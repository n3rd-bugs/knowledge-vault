"""Package C — the curation loop: quiet expiry, report visibility, judged-pair
memory (migration 017), and auto-resolver scheduling.

Real DB in kb_test (conftest ``kb``); Ollama is never called — the embedder is
mocked by conftest and every 14B entry point used here (llm.items_contradict,
evidence.gather_evidence) is monkeypatched. Four defects, one file:

1. quiet expiry must not close a row carrying an UNAPPLIED reject_* verdict;
2. the nightly report must carry the four curation counts;
3. contradiction pairs are judged ONCE per embed model, under a per-night cap;
4. a near-decided contradiction (streak >= 1) gets a reserved slice of the
   evidence cap instead of starving behind never-enriched rows.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from kbcore import consolidation, db, evidence, llm, review_auto, review_expire, review_triage
from kbcore.config import settings
from kbcore.ingest import ingest
from kbcore.review import queue_review
from kbworker import consolidate

# --- helpers -----------------------------------------------------------------

# Each near-pair lives on its OWN two dimensions, so pairs are mutually
# orthogonal: without that, every a_i/b_j combination also lands in the
# contradiction band and the pair counts below stop meaning anything.
_AXIS = itertools.count()


def _lit(values: list[float]) -> str:
    return "[" + ",".join(repr(x) for x in values) + "]"


def _force_lead(item_id: int, vec: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE chunks SET embedding = %s::vector WHERE item_id = %s AND ord = 0",
            (vec, item_id),
        )


def _near_pair(project: str, tag: str) -> tuple[int, int]:
    """Two items in ``project`` whose lead-chunk cosine is 0.9 — inside the
    contradiction band [CONTRA_LOW, CONTRA_HIGH) and orthogonal to every other
    pair this module makes."""
    axis = next(_AXIS) % (settings.kb_embed_dim // 2)
    a_vec = [0.0] * settings.kb_embed_dim
    b_vec = [0.0] * settings.kb_embed_dim
    a_vec[2 * axis] = 1.0
    b_vec[2 * axis] = 0.9
    b_vec[2 * axis + 1] = 0.4358899  # |b| = 1, cos(a, b) = 0.9
    a = ingest(project, "note", f"a {tag}", f"body a {tag}", uri=f"{project}-a-{tag}")
    b = ingest(project, "note", f"b {tag}", f"body b {tag}", uri=f"{project}-b-{tag}")
    _force_lead(a, _lit(a_vec))
    _force_lead(b, _lit(b_vec))
    return a, b


def _unverified(project: str, tag: str) -> tuple[int, int]:
    """One unverified item + its open unverified_extraction review row."""
    item_id = ingest(project, "note", f"note {tag}", f"body {tag}", trust="unverified", uri=tag)
    return item_id, queue_review(item_id, "unverified_extraction", {"rejected_count": 0})


def _age_row(queue_id: int, days: int) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE review_queue SET created_at = now() - make_interval(days => %s) WHERE id = %s",
            (days, queue_id),
        )


def _resolved(queue_id: int) -> bool:
    with db.connect() as conn:
        return conn.execute(
            "SELECT resolved FROM review_queue WHERE id = %s", (queue_id,)
        ).fetchone()[0]


def _judgements() -> list[tuple[int, int, str, bool]]:
    with db.connect() as conn:
        return [
            (r[0], r[1], r[2], r[3])
            for r in conn.execute(
                "SELECT a_id, b_id, embed_model, verdict FROM contradiction_judgements "
                "ORDER BY a_id, b_id, embed_model"
            ).fetchall()
        ]


@pytest.fixture()
def judge_calls(monkeypatch):
    """llm.items_contradict, mocked, counting the pairs it was asked about."""
    calls: list[tuple[str, str]] = []

    def _judge(a_title, a_body, b_title, b_body):
        calls.append((a_title, b_title))
        return False

    monkeypatch.setattr(llm, "items_contradict", _judge)
    return calls


# --- DEFECT 1: quiet expiry must not close unapplied reject verdicts ----------


def test_reject_suggested_row_survives_quiet_expiry(kb):
    """A known-bad item's evidence must not quietly expire while it stays live."""
    item_id, qid = _unverified("curation", "rejectme")
    review_triage.store_triage(qid, {"action": "reject_noise", "dup_of": None, "rationale": "r"})
    _age_row(qid, 400)  # far past ANY expiry age, and never recalled

    assert review_expire.expire_quiet_rows(30)["expired"] == 0
    assert _resolved(qid) is False, "an unapplied reject_* verdict was quietly closed"
    with db.connect() as conn:
        status = conn.execute("SELECT status FROM items WHERE id = %s", (item_id,)).fetchone()[0]
    assert status == "active", "expiry must not touch the item either"


def test_reject_duplicate_is_exempt_too(kb):
    """Both reject_* actions are exempt — the exemption tracks llm.TRIAGE_ACTIONS."""
    assert set(review_auto.REJECT_TRIAGE_ACTIONS) == {"reject_noise", "reject_duplicate"}
    _item, qid = _unverified("curation", "dupme")
    review_triage.store_triage(
        qid, {"action": "reject_duplicate", "dup_of": None, "rationale": "r"}
    )
    _age_row(qid, 400)

    assert review_expire.expire_quiet_rows(30)["expired"] == 0
    assert _resolved(qid) is False


def test_keep_suggested_expires_but_an_untriaged_row_is_held(kb):
    """Applying a 'keep' lands on the SAME end state as expiry, so it may expire.

    The untriaged row no longer does (owner amendment 2026-08-08, amending KB
    957's "an untriaged row expires as before"): closing it would file an
    unexamined item away for good, so it stays open as visible queue depth.
    """
    _i1, keep_qid = _unverified("curation", "keepme")
    review_triage.store_triage(keep_qid, {"action": "keep", "dup_of": None, "rationale": "r"})
    _i2, plain_qid = _unverified("curation", "plain")
    for qid in (keep_qid, plain_qid):
        _age_row(qid, 400)

    assert review_expire.expire_quiet_rows(30)["expired"] == 1
    assert _resolved(keep_qid) is True
    assert _resolved(plain_qid) is False


def test_quiet_expiry_kill_switch_and_age_floor_still_hold(kb):
    """The exemption must not change the two bounds the sweep already had."""
    _item, qid = _unverified("curation", "young")
    review_triage.store_triage(qid, {"action": "keep", "dup_of": None, "rationale": "r"})
    assert review_expire.expire_quiet_rows(0)["expired"] == 0, "0 is still the kill switch"
    assert review_expire.expire_quiet_rows(30)["expired"] == 0, "a fresh row is still too young"
    _age_row(qid, 400)
    assert review_expire.expire_quiet_rows(30)["expired"] == 1


# --- DEFECT 2: the report must carry the curation counts ---------------------


def test_curation_counts_report_the_backlog(kb):
    _i1, q_reject = _unverified("curation", "c1")
    review_triage.store_triage(
        q_reject, {"action": "reject_noise", "dup_of": None, "rationale": ""}
    )
    _i2, q_keep = _unverified("curation", "c2")
    review_triage.store_triage(q_keep, {"action": "keep", "dup_of": None, "rationale": ""})
    _i3, q_raw = _unverified("curation", "c3")  # never triaged
    _age_row(q_raw, 10)

    a, b = _near_pair("curation", "sugg")
    qid = queue_review(None, "contradiction", {"a_id": a, "b_id": b})
    evidence.update_review_detail(
        qid,
        {"suggested_resolution": {"action": "supersede", "keep": a, "retire": b, "rule": "r"}},
    )
    queue_review(None, "contradiction", {"a_id": a, "b_id": b})  # no suggestion yet

    counts = review_auto.curation_counts(30)
    assert counts["triage_awaiting_apply"] == 2
    assert counts["reject_suggestions_pending"] == 1
    assert counts["contradiction_suggestions_pending"] == 1
    assert counts["never_triaged_open"] == 1, "q_raw is the whole examined-nothing backlog"
    # q_keep is the oldest row that can ACTUALLY expire: the reject-suggested row
    # is exempt, and q_raw — older — is held open by the untriaged gate.
    with db.connect() as conn:
        expected = conn.execute(
            "SELECT (created_at + make_interval(days => 30))::date::text "
            "FROM review_queue WHERE id = %s",
            (q_keep,),
        ).fetchone()[0]
    assert counts["first_expiry_date"] == expected


def test_first_expiry_date_is_none_when_expiry_is_off_or_nothing_is_eligible(kb):
    _item, qid = _unverified("curation", "off")
    assert review_auto.curation_counts(0)["first_expiry_date"] is None, "kill switch → no date"
    review_triage.store_triage(qid, {"action": "reject_duplicate", "dup_of": None, "rationale": ""})
    assert review_auto.curation_counts(30)["first_expiry_date"] is None, "only exempt rows left"


def test_the_nightly_report_carries_the_curation_keys(kb, monkeypatch, tmp_path):
    """The keys must ride the run report (and hence the persisted result)."""
    monkeypatch.setattr(settings, "kb_raw", tmp_path / "kbraw")
    monkeypatch.setattr(settings, "kb_eval_in_consolidation", False)
    monkeypatch.setattr(llm, "items_contradict", lambda *a, **k: False)
    _i, qid = _unverified("curation", "reported")
    review_triage.store_triage(qid, {"action": "reject_noise", "dup_of": None, "rationale": ""})

    counts = consolidate.main()

    assert counts["triage_awaiting_apply"] == 1
    assert counts["reject_suggestions_pending"] == 1
    assert counts["contradiction_suggestions_pending"] == 0
    assert counts["first_expiry_date"] is None, "the only open row is exempt from expiry"
    assert counts["contradiction_pairs_judged"] == 0
    # Existing keys are never renamed — the report is read night over night.
    for key in ("review_expired", "review_queue_open", "evidence_refreshed", "archived"):
        assert key in counts


# --- DEFECT 3: judged-pair memory (migration 017) ----------------------------


def test_a_judged_pair_is_never_re_judged(kb, judge_calls):
    a, b = _near_pair("contra", "x")

    assert consolidate._contradictions("contra") == 0
    assert len(judge_calls) == 1, "the pair should be judged once"
    assert _judgements() == [(a, b, settings.kb_embed_model, False)]

    assert consolidate._contradictions("contra") == 0
    assert len(judge_calls) == 1, "the pair was re-judged from scratch"
    assert consolidation.contradiction_pairs("contra") == [], "a judged pair is not re-offered"


def test_a_positive_verdict_is_remembered_too(kb, monkeypatch):
    a, b = _near_pair("contra", "pos")
    monkeypatch.setattr(llm, "items_contradict", lambda *a, **k: True)

    assert consolidate._contradictions("contra") == 1
    assert _judgements() == [(a, b, settings.kb_embed_model, True)]
    with db.connect() as conn:
        (rows,) = conn.execute(
            "SELECT count(*) FROM review_queue WHERE reason = 'contradiction'"
        ).fetchone()
    assert rows == 1, "the contradiction is still queued exactly once"


def test_re_embedding_with_a_new_model_reopens_the_pair(kb, judge_calls, monkeypatch):
    """The embed_model column IS the invalidation mechanism (migration 017)."""
    a, b = _near_pair("contra", "reembed")
    old_model = settings.kb_embed_model
    consolidate._contradictions("contra")
    assert len(judge_calls) == 1

    monkeypatch.setattr(settings, "kb_embed_model", "some-newer-embedder:1b")
    with db.transaction() as conn:  # what reembed does: chunks carry the new model
        conn.execute("UPDATE chunks SET embed_model = %s", (settings.kb_embed_model,))

    assert consolidate._contradictions("contra") == 0
    assert len(judge_calls) == 2, "a new embed model must re-open the pair"
    judged = _judgements()
    assert len(judged) == 2, "the old verdict is kept, never updated (INSERT-only)"
    assert {m for _a, _b, m, _v in judged} == {old_model, settings.kb_embed_model}
    assert {(x, y) for x, y, _m, _v in judged} == {(a, b)}


def test_a_verdict_survives_a_kill_mid_step(kb, monkeypatch):
    """A power cut loses the pair in flight, not the whole step's work."""
    a1, b1 = _near_pair("contra", "first")
    _near_pair("contra", "second")
    seen: list[int] = []

    def _judge(a_title, a_body, b_title, b_body):
        seen.append(1)
        if len(seen) > 1:
            raise RuntimeError("power cut")
        return False

    monkeypatch.setattr(llm, "items_contradict", _judge)
    with pytest.raises(RuntimeError):
        consolidate._contradictions("contra")

    assert _judgements() == [(a1, b1, settings.kb_embed_model, False)], (
        "the completed pair's verdict was not durable"
    )


def test_the_per_night_pair_cap_bounds_the_step(kb, judge_calls):
    for tag in ("p1", "p2", "p3"):
        _near_pair("contra", tag)

    consolidate._contradictions("contra", None, 2)
    assert len(judge_calls) == 2, "the cap must bound the pairs judged"
    assert len(_judgements()) == 2
    # The unreached pair is simply offered again — nothing was lost.
    assert len(consolidation.contradiction_pairs("contra")) == 1


def test_the_pair_cap_is_shared_across_projects(kb, judge_calls, monkeypatch):
    _near_pair("proj_a", "p1")
    _near_pair("proj_a", "p2")
    _near_pair("proj_b", "p3")
    monkeypatch.setattr(settings, "kb_contradiction_pair_cap", 2)

    consolidate._contradictions_all(["proj_a", "proj_b"])
    assert len(judge_calls) == 2, "the budget is per NIGHT, not per project"
    assert consolidate._CONTRA_STATS["judged"] == 2

    monkeypatch.setattr(settings, "kb_contradiction_pair_cap", 0)
    consolidate._contradictions_all(["proj_a", "proj_b"])
    assert len(judge_calls) == 2, "0 is the kill switch"


def test_dedupe_pairs_are_unaffected_by_the_judged_memory(kb):
    """Only the LLM-bound step skips judged pairs; dedupe SQL is cheap to redo."""
    a = ingest("dup", "note", "a", "body a", uri="dup-a")
    b = ingest("dup", "note", "b", "body b", uri="dup-b")
    dim = settings.kb_embed_dim
    _force_lead(a, _lit([1.0] + [0.0] * (dim - 1)))
    _force_lead(b, _lit([0.999, 0.0447102] + [0.0] * (dim - 2)))
    consolidation.record_pair_judgement(a, b, False)
    assert consolidation.duplicate_pairs("dup", 0.97) != []


# --- DEFECT 4: the auto-resolver must not starve near-decided rows -----------


def _row(qid: int, streak: int = 0) -> tuple[int, int, int, dict[str, Any]]:
    detail: dict[str, Any] = {}
    if streak:
        detail = {
            "suggested_resolution": {"keep": 1, "retire": 2},
            "suggestion_streak": streak,
        }
    return (qid, 1, 2, detail)


def test_streak_rows_get_a_reserved_slice_of_the_cap(monkeypatch):
    monkeypatch.setattr(consolidate, "EVIDENCE_PAIR_CAP", 4)  # reserve = 2
    fresh = [_row(i) for i in range(10, 20)]  # never-enriched: the query puts these first
    hot = [_row(90, 1), _row(91, 1), _row(92, 1)]

    batch = consolidate._evidence_batch(fresh + hot)

    assert len(batch) == 4
    assert [r[0] for r in batch[:2]] == [90, 91], "streak rows must lead the batch"
    assert [r[0] for r in batch[2:]] == [10, 11], "the rest keeps never-enriched-first order"


def test_a_row_is_never_selected_twice(monkeypatch):
    monkeypatch.setattr(consolidate, "EVIDENCE_PAIR_CAP", 4)
    assert [r[0] for r in consolidate._evidence_batch([_row(90, 1), _row(91)])] == [90, 91]


def test_the_reserve_is_a_floor_not_a_ceiling(monkeypatch):
    """Half the cap is GUARANTEED to streak rows; the rest follows the query's
    order, so no slot is ever left idle — with only one never-enriched row to
    offer, the leftover capacity goes back to streak rows rather than shrinking
    the batch."""
    monkeypatch.setattr(consolidate, "EVIDENCE_PAIR_CAP", 4)  # reserve = 2
    hot = [_row(90 + i, 1) for i in range(10)]
    batch = consolidate._evidence_batch([_row(10), *hot])
    assert [r[0] for r in batch] == [90, 91, 10, 92]


def test_a_stuck_suggestion_reaches_auto_resolution(kb, monkeypatch):
    """End to end: a streak=1 row behind a full window of fresh rows resolves."""
    a, b = _near_pair("starve", "decided")
    stuck = queue_review(None, "contradiction", {"a_id": a, "b_id": b})
    sugg = {"action": "supersede", "keep": a, "retire": b, "rule": "evidence: 2 supporters"}
    evidence.update_review_detail(
        stuck,
        {
            "suggested_resolution": sugg,
            "suggestion_streak": 1,
            "evidence_updated": "2026-07-31T00:00:00+00:00",
        },
    )
    # …queued BEHIND two never-enriched rows, which the query orders first.
    fresh_ids = []
    for tag in ("fresh1", "fresh2"):
        c, d = _near_pair("starve", tag)
        fresh_ids.append(queue_review(None, "contradiction", {"a_id": c, "b_id": d}))

    monkeypatch.setattr(consolidate, "EVIDENCE_PAIR_CAP", 2)  # reserve = 1
    monkeypatch.setattr(
        evidence,
        "gather_evidence",
        lambda x, y: {
            "a_id": x,
            "b_id": y,
            "evidence_updated": "2026-08-01T00:00:00+00:00",
            "suggested_resolution": sugg if (x, y) == (a, b) else None,
        },
    )
    refreshed, auto_resolved, _moot = consolidate._evidence()

    assert (refreshed, auto_resolved) == (2, 1)
    assert _resolved(stuck) is True, "the streak-2 suggestion was never executed"
    assert _resolved(fresh_ids[0]) is False
    with db.connect() as conn:
        status, superseded_by = conn.execute(
            "SELECT status, superseded_by FROM items WHERE id = %s", (b,)
        ).fetchone()
    assert (status, superseded_by) == ("superseded", a)

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
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kbcore import (
    consolidation,
    db,
    evidence,
    judge_fit,
    llm,
    review_auto,
    review_expire,
    review_triage,
)
from kbcore.config import settings
from kbcore.errors import ExtractorUnavailable
from kbcore.ingest import ingest
from kbcore.review import queue_review
from kbworker import consolidate, contradiction_blanks

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
    # KB 55589: the prompt-budget keys ride the report too (added, never renamed).
    assert counts["contradiction_pairs_trimmed"] == 0
    assert counts["contradiction_pairs_no_verdict"] == 0
    assert counts["judge_prompts_trimmed"] == {"contradict": 0, "evidence": 0, "same_fact": 0}
    assert counts["contradiction_no_verdict_pairs"] == []
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


def _age_blanks(days: int) -> None:
    """Move every no-verdict stamp ``days`` into the past (the rest is RETRY_DAYS)."""
    stamps = contradiction_blanks.load()
    when = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    for entry in stamps.values():
        entry["at"] = when
    contradiction_blanks._save(stamps)


def _pin_caps(monkeypatch, cap: int = 500) -> None:
    """The breaker tests need room: never read the calendar's (or .env's) pair cap."""
    monkeypatch.setattr(settings, "kb_contradiction_pair_cap", cap)
    monkeypatch.setattr(settings, "kb_contradiction_pair_cap_weekday", cap)


def _stats(**want: int) -> dict[str, int]:
    base = {"judged": 0, "no_verdict": 0, "resting": 0, "fresh_judged": 0, "fresh_blank": 0}
    return {**base, **want}


def test_a_reply_with_no_verdict_is_never_remembered(kb, monkeypatch):
    """KB 55589: None (empty, unparseable) is not a "no" — counted, warned, never memorized;
    the pair rests RETRY_DAYS (it would blank again tomorrow: same request, same seeds),
    then is asked again, and a real answer clears the stamp."""
    a, b = _near_pair("contra", "blank")
    answers = iter([None, False])
    monkeypatch.setattr(llm, "items_contradict", lambda *a_, **k: next(answers))

    assert consolidate._contradictions_all(["contra"]) == 0
    assert _judgements() == [], "a reply without a verdict must never become 'no conflict'"
    assert consolidate._CONTRA_STATS == _stats(
        judged=1, no_verdict=1, fresh_judged=1, fresh_blank=1
    )
    assert consolidate._CONTRA_BLANK == [[a, b]], "the report names the pair"
    assert [(x, y) for x, y, _s in consolidation.contradiction_pairs("contra")] == [(a, b)]
    assert contradiction_blanks.known(contradiction_blanks.load(), a, b)

    assert consolidate._contradictions_all(["contra"]) == 0  # tomorrow: it rests
    assert consolidate._CONTRA_STATS == _stats(resting=1), "a resting pair is not judged"

    _age_blanks(contradiction_blanks.RETRY_DAYS + 1)
    assert consolidate._contradictions_all(["contra"]) == 0  # after its rest: a real "no"
    assert _judgements() == [(a, b, settings.kb_embed_model, False)]
    assert consolidate._CONTRA_STATS == _stats(judged=1), "a retry is not a fresh pair"
    assert consolidate._CONTRA_BLANK == []
    assert contradiction_blanks.load() == {}, "an answer clears the stamp"


def test_resting_blanks_do_not_take_the_cap(kb, monkeypatch):
    """Blank pairs sort to the head (sim DESC) night after night; resting, they are skipped
    and the cap goes to the pairs behind them."""
    _pin_caps(monkeypatch)
    heads = [_near_pair("rest", f"h{i}") for i in range(2)]
    monkeypatch.setattr(llm, "items_contradict", lambda *_a: None)
    consolidate._contradictions_all(["rest"])
    assert {tuple(p) for p in consolidate._CONTRA_BLANK} == set(heads)
    tails = [_near_pair("rest", f"t{i}") for i in range(2)]
    asked: list[str] = []
    monkeypatch.setattr(llm, "items_contradict", lambda at, *_r: asked.append(at) or False)
    monkeypatch.setattr(settings, "kb_contradiction_pair_cap", 2)
    monkeypatch.setattr(settings, "kb_contradiction_pair_cap_weekday", 2)

    consolidate._contradictions_all(["rest"])
    assert sorted(asked) == sorted(f"a t{i}" for i in range(2)), "the cap went to the tails"
    assert {(x, y) for x, y, _m, _v in _judgements()} == set(tails)


def test_a_new_seat_or_embedder_frees_a_resting_pair(kb, monkeypatch):
    a, b = _near_pair("rest2", "x")
    monkeypatch.setattr(llm, "items_contradict", lambda *_a: None)
    consolidate._contradictions_all(["rest2"])
    stamps = contradiction_blanks.load()
    assert contradiction_blanks.resting(stamps, a, b)
    monkeypatch.setattr(settings, "kb_contradiction_judge_model", "another-seat:9b")
    assert not contradiction_blanks.resting(stamps, a, b), "a new seat starts fresh"


def test_a_seat_that_cannot_answer_stands_the_step_down(kb, monkeypatch):
    """KB 55589's breaker: never-memorized blanks must not spend the whole cap every night.
    From the 6th FRESH pair judged, half or more blank stands the step down at the next
    stop check (every STOP_CHECK_EVERY pairs) — here after 10 of 14, and the next project
    is not judged at all."""
    _pin_caps(monkeypatch)
    for tag in range(12):
        _near_pair("blank1", f"p{tag}")
    _near_pair("blank2", "q0")
    _near_pair("blank2", "q1")
    calls: list[str] = []
    monkeypatch.setattr(llm, "items_contradict", lambda at, *_r: calls.append(at) or None)
    consolidate._STOP_LOG.clear()

    assert consolidate._contradictions_all(["blank1", "blank2"]) == 0
    assert len(calls) == 10, calls
    assert consolidate._CONTRA_STATS == _stats(
        judged=10, no_verdict=10, fresh_judged=10, fresh_blank=10
    )
    assert _judgements() == []
    assert consolidate._STOP_LOG["contradictions_queued"] == {"reason": "no verdict", "deferred": 4}


def test_a_block_of_retried_blanks_neither_trips_the_breaker_nor_stalls_the_step(kb, monkeypatch):
    """Round-2 review: persistent blanks at the head of the queue must not stand the step
    down every night. Retried ones (known blanks past their rest) do not count."""
    _pin_caps(monkeypatch)
    poison = [_near_pair("blk", f"z{i}") for i in range(6)]
    monkeypatch.setattr(llm, "items_contradict", lambda *_a: None)
    consolidate._contradictions_all(["blk"])  # night 1: six fresh blanks (under the floor)
    good = [_near_pair("blk", f"g{i}") for i in range(6)]
    _age_blanks(contradiction_blanks.RETRY_DAYS + 1)  # the six are due for a retry
    poisoned = {p for pair in poison for p in pair}
    monkeypatch.setattr(
        llm,
        "items_contradict",
        lambda at, ab, bt, bb: None if at.removeprefix("a ").startswith("z") else False,
    )
    consolidate._STOP_LOG.clear()

    consolidate._contradictions_all(["blk"])
    assert "contradictions_queued" not in consolidate._STOP_LOG, "retries never trip it"
    assert consolidate._CONTRA_STATS["fresh_blank"] == 0
    assert {(x, y) for x, y, _m, _v in _judgements()} == set(good)
    assert not poisoned & {x for x, _y, _m, _v in _judgements()}
    retried = consolidate._CONTRA_STATS["judged"] - len(good)
    assert retried == contradiction_blanks.RETRIES_PER_RUN, "a few retries per run, no more"
    stamps = contradiction_blanks.load()
    again = [p for p in poison if contradiction_blanks.resting(stamps, *p)]
    assert again == poison[:5], "each retried pair is re-stamped and rests again ..."
    assert not contradiction_blanks.resting(stamps, *poison[5]), "... the sixth waits its turn"
    assert sorted(stamps[f"{a}\t{b}"]["nights"] for a, b in poison) == [1, 2, 2, 2, 2, 2]


def test_a_seat_broken_for_weeks_still_stands_down_and_retries_stay_bounded(kb, monkeypatch):
    """Round-3 review: due retries go AFTER every fresh pair (so the breaker sees fresh
    replies first) and at most RETRIES_PER_RUN of them run — a seat that stays broken
    under the same name must not grow its nightly bill week by week."""
    _pin_caps(monkeypatch)
    for i in range(30):
        _near_pair("dead", f"d{i}")
    monkeypatch.setattr(llm, "items_contradict", lambda *_a: None)
    for night in range(3):
        consolidate._STOP_LOG.clear()
        consolidate._contradictions_all(["dead"])
        assert consolidate._STOP_LOG["contradictions_queued"]["reason"] == "no verdict"
        assert consolidate._CONTRA_STATS["fresh_judged"] == 10, f"night {night}"
        assert consolidate._CONTRA_STATS["judged"] == 10, "the breaker trips before retries"
        _age_blanks(contradiction_blanks.RETRY_DAYS + 1)


def test_an_occasional_blank_never_trips_the_breaker(kb, monkeypatch):
    _pin_caps(monkeypatch)
    for tag in range(12):
        _near_pair("blank3", f"r{tag}")
    answers = itertools.cycle([None, False, False])  # a third blank: under half
    monkeypatch.setattr(llm, "items_contradict", lambda *_a: next(answers))
    consolidate._STOP_LOG.clear()

    assert consolidate._contradictions_all(["blank3"]) == 0
    assert consolidate._CONTRA_STATS == _stats(
        judged=12, no_verdict=4, fresh_judged=12, fresh_blank=4
    )
    assert "contradictions_queued" not in consolidate._STOP_LOG
    assert len(_judgements()) == 8, "every real answer is remembered"


def test_the_nightly_report_counts_trimmed_and_blank_pairs(kb, monkeypatch, tmp_path):
    """The report keys (and the digest clause) through main(), not just module state: one
    oversized pair fitted and answered, one small pair whose replies carry no verdict."""
    monkeypatch.setattr(settings, "kb_raw", tmp_path / "kbraw")
    monkeypatch.setattr(settings, "kb_eval_in_consolidation", False)
    monkeypatch.setattr(settings, "kb_llm_num_ctx", 2048)  # judge ceiling floor: 1024
    big = ingest("rpt", "spec", "big a", " ".join(f"a{i}" for i in range(1500)), uri="rpt-a")
    big_b = ingest("rpt", "spec", "big b", " ".join(f"b{i}" for i in range(1500)), uri="rpt-b")
    small = (ingest("rpt", "qa", "small a", "x is 5", uri="rs-a"),)
    small += (ingest("rpt", "qa", "small b", "x is 7", uri="rs-b"),)
    pairs = [(big, big_b, 0.9), (*small, 0.88)]
    monkeypatch.setattr(
        consolidation,
        "contradiction_pairs",
        lambda project, *a_, **k: pairs if project == "rpt" else [],
    )

    def chat(messages, **_k):
        user = messages[-1]["content"]
        if "Item A: big a" in user:
            return '{"contradict": false}'
        return "" if "Item A: small a" in user else "{}"

    monkeypatch.setattr(llm, "_chat", chat)
    judge_fit.FIT_STATS.update(contradict=5)  # a stale count must not leak into the report

    counts = consolidate.main()

    assert counts["contradiction_pairs_judged"] == 2
    assert counts["contradiction_pairs_trimmed"] == 1
    assert counts["contradiction_pairs_no_verdict"] == 1
    assert counts["contradiction_no_verdict_pairs"] == [list(small)]
    assert counts["judge_prompts_trimmed"] == {"contradict": 1, "evidence": 0, "same_fact": 0}
    from kbworker import digest

    assert "judged pairs: 2 (1 trimmed, 1 no verdict)" in digest.digest_lines(counts)


def test_a_step_that_fails_midway_still_reports_what_it_spent(kb, monkeypatch, tmp_path):
    _pin_caps(monkeypatch)
    monkeypatch.setattr(settings, "kb_raw", tmp_path / "kbraw")
    monkeypatch.setattr(settings, "kb_eval_in_consolidation", False)
    first = _near_pair("mid", "first")
    _near_pair("mid", "second")

    def chat(messages, **_k):
        if "Item A: a first" in messages[-1]["content"]:
            return ""  # no verdict on either draw
        raise ExtractorUnavailable("ollama went away")

    monkeypatch.setattr(llm, "_chat", chat)

    counts = consolidate.main()

    assert counts["contradictions_queued"] == 0  # the step failed: _run_step's default
    assert counts["contradiction_pairs_judged"] == 1
    assert counts["contradiction_pairs_no_verdict"] == 1
    assert counts["contradiction_no_verdict_pairs"] == [list(first)]


def test_an_oversized_pair_is_judged_fitted_and_its_answer_remembered(kb, monkeypatch):
    """KB 55589 end to end through the step: the real items_contradict fits the prompt
    (counted in FIT_STATS, which the report reads per step) and a real "no" is memorized."""
    monkeypatch.setattr(settings, "kb_llm_num_ctx", 2048)  # judge ceiling floor: 1024
    a = ingest("contra", "spec", "spec a", " ".join(f"a{i}" for i in range(1500)), uri="ts-a")
    b = ingest("contra", "spec", "spec b", " ".join(f"b{i}" for i in range(1500)), uri="ts-b")
    monkeypatch.setattr(consolidation, "contradiction_pairs", lambda *a_, **k: [(a, b, 0.9)])
    sent: list[str] = []
    monkeypatch.setattr(
        llm, "_chat", lambda m, **k: sent.append(m[1]["content"]) or '{"contradict": false}'
    )
    judge_fit.FIT_STATS.update(contradict=0)
    assert consolidate._contradictions_all(["contra"]) == 0
    assert judge_fit.FIT_STATS["contradict"] == 1
    assert "tokens omitted ...]" in sent[0]
    assert _judgements() == [(a, b, settings.kb_embed_model, False)]


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

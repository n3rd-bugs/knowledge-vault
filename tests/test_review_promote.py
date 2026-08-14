"""WAVE B — the PROMOTE PROGRAM (review #3 strategic P1, KB 19663).

Trust has been one-way in practice: promote throughput ~0 while the top-5
unverified share rose 53.5% -> 64%. ``kbcore.review_promote`` is the surface
that makes the HUMAN fast — V4 IS LAW, so everything mutating here is pinned to
run through the ONE existing owner chokepoint (``kbcore.review.promote``) and
only on explicit, confirmed invocation:

* candidate enumeration is READ-ONLY, recall-proven, ordered, paginated, and
  never proposes consolidation output (summary/report);
* ``promote_batch`` dry-run writes NOTHING (table-snapshot proof) and its apply
  path matches the singular ``review.promote`` end state byte-for-byte;
* the CLI defaults to a dry run and a DECLINED confirmation writes nothing;
* the nightly only COUNTS candidates (``curation_counts.promote_candidates`` —
  an ADDED key; the digest line pin lives in test_obs.py).

Real DB (kb_test via the ``kb`` fixture); Ollama is never called.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from kbcore import db, review_auto, review_promote
from kbcore import review as review_mod
from kbcore.config import settings
from kbcore.ingest import ingest
from kbcore.items import log_access
from kbcore.review import queue_review

runner = CliRunner()


# --- helpers -----------------------------------------------------------------


def _unverified(tag: str, project: str = "pp", type_: str = "note") -> tuple[int, int]:
    """One unverified item + its open unverified_extraction row (the WP-1 shape)."""
    item_id = ingest(project, type_, f"t {tag}", f"b {tag}", trust="unverified", uri=tag)
    return item_id, queue_review(item_id, "unverified_extraction", {"rejected_count": 0})


def _recall(item_id: int, n: int) -> None:
    for _ in range(n):
        log_access(item_id, "kb_search")


def _age_item(item_id: int, days: int) -> None:
    """Backdate an item (kb_test fixture data) so age-ordering is testable."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE items SET created_at = now() - make_interval(days => %s) WHERE id = %s",
            (days, item_id),
        )


def _item_state(item_id: int) -> tuple[str, str]:
    with db.connect() as conn:
        r = conn.execute(
            "SELECT i.status, COALESCE(s.trust, 'verified') FROM items i "
            "LEFT JOIN sources s ON s.id = i.source_id WHERE i.id = %s",
            (item_id,),
        ).fetchone()
    return r[0], r[1]


def _row(qid: int) -> tuple[bool, dict]:
    with db.connect() as conn:
        r = conn.execute(
            "SELECT resolved, detail FROM review_queue WHERE id = %s", (qid,)
        ).fetchone()
    return bool(r[0]), (r[1] if isinstance(r[1], dict) else {})


def _snapshot() -> tuple:
    """Full table snapshot of everything a promote could touch — dry-run proof."""
    with db.connect() as conn:
        items = conn.execute("SELECT id, status, updated_at FROM items ORDER BY id").fetchall()
        sources = conn.execute("SELECT id, trust FROM sources ORDER BY id").fetchall()
        queue = conn.execute("SELECT id, resolved, detail FROM review_queue ORDER BY id").fetchall()
    return items, sources, queue


@pytest.fixture(autouse=True)
def _promote_defaults(monkeypatch):
    """Pin the knob so a live .env cannot decide what these tests measure."""
    monkeypatch.setattr(settings, "kb_promote_candidate_min_recalls", 3)


# --- 1. candidate enumeration: threshold / ordering / exclusions --------------


def test_candidates_are_recall_proven_ordered_and_exclude_consolidation_output(kb):
    a, qa = _unverified("a")  # 5 recalls, 40d old -> rank 1 (ties break oldest-first)
    _recall(a, 5)
    _age_item(a, 40)
    b, qb = _unverified("b")  # 5 recalls, fresh   -> rank 2
    _recall(b, 5)
    i, qi = _unverified("i")  # 4 recalls, row already closed -> still a candidate
    _recall(i, 4)
    review_mod.resolve_review(qi)
    c, qc = _unverified("c")  # 3 recalls, at the floor -> rank 4
    _recall(c, 3)
    d, _qd = _unverified("d")  # 2 recalls: below the floor
    _recall(d, 2)
    e, _qe = _unverified("e", type_="summary")  # consolidation output, recalled
    _recall(e, 5)
    f, _qf = _unverified("f", type_="report")
    _recall(f, 5)
    g = ingest("pp", "note", "t g", "b g", uri="g")  # verified: nothing to promote
    _recall(g, 5)
    h, _qh = _unverified("h")  # unverified but retired
    _recall(h, 5)
    with db.transaction() as conn:
        conn.execute("UPDATE items SET status = 'archived' WHERE id = %s", (h,))

    rows = review_promote.list_promote_candidates()

    assert [r["item_id"] for r in rows] == [a, b, i, c], "recalls DESC, then oldest first"
    assert [r["queue_id"] for r in rows] == [qa, qb, None, qc], "open row id, or None"
    top = rows[0]
    assert (top["project"], top["type"], top["title"]) == ("pp", "note", "t a")
    assert top["recalls"] == 5 and top["age_days"] == 40


def test_candidate_threshold_defaults_to_the_knob_and_zero_is_a_floor(kb, monkeypatch):
    lo, _q1 = _unverified("lo")  # 1 recall
    _recall(lo, 1)
    hi, _q2 = _unverified("hi")  # 4 recalls
    _recall(hi, 4)
    never, _q3 = _unverified("never")  # 0 recalls

    assert [r["item_id"] for r in review_promote.list_promote_candidates()] == [hi]
    monkeypatch.setattr(settings, "kb_promote_candidate_min_recalls", 1)
    assert [r["item_id"] for r in review_promote.list_promote_candidates()] == [hi, lo]
    # explicit param overrides the knob; 0 is a floor, not a kill switch
    assert [r["item_id"] for r in review_promote.list_promote_candidates(5)] == []
    assert never in [r["item_id"] for r in review_promote.list_promote_candidates(0)]


def test_candidate_filters_and_pagination(kb):
    a, _ = _unverified("fa", project="p1", type_="gotcha")
    _recall(a, 5)
    b, _ = _unverified("fb", project="p1")
    _recall(b, 4)
    c, _ = _unverified("fc", project="p2")
    _recall(c, 3)

    assert [r["item_id"] for r in review_promote.list_promote_candidates(project="p1")] == [a, b]
    assert [r["item_id"] for r in review_promote.list_promote_candidates(item_type="gotcha")] == [a]
    assert [r["item_id"] for r in review_promote.list_promote_candidates(limit=1)] == [a]
    assert [r["item_id"] for r in review_promote.list_promote_candidates(limit=2, offset=1)] == [
        b,
        c,
    ], "offset pages through the same stable order"


def test_count_matches_the_list_at_the_default_threshold(kb):
    for n, tag in enumerate(("ca", "cb", "cc")):
        item, _q = _unverified(tag)
        _recall(item, 3 + n)
    below, _q = _unverified("cd")
    _recall(below, 2)
    assert review_promote.count_promote_candidates() == 3
    assert review_promote.count_promote_candidates() == len(
        review_promote.list_promote_candidates()
    )


# --- 2. promote_batch: dry run writes nothing (table-snapshot proof) ----------


def test_batch_dry_run_writes_nothing_and_reports_what_would_promote(kb):
    a, qa = _unverified("dry-a")
    _recall(a, 3)
    b, qb = _unverified("dry-b")
    _recall(b, 3)
    review_mod.resolve_review(qb)  # b has no OPEN row any more
    before = _snapshot()

    outcomes = review_promote.promote_batch([a, b], dry_run=True)

    assert outcomes == [
        (a, f"would promote (queue {qa} resolves)"),
        (b, "would promote (audit row minted + resolved)"),
    ]
    assert _snapshot() == before, "a dry run must not touch items, sources or review_queue"


# --- 3. apply parity with the singular review.promote -------------------------


def test_apply_promotes_and_closes_the_row_exactly_as_the_singular_promote(kb):
    """Twin items, one through review.promote(qid), one through promote_batch:
    the end states must be indistinguishable (trust flip + row resolution)."""
    manual, q_manual = _unverified("twin-manual")
    _recall(manual, 3)
    batched, q_batched = _unverified("twin-batched")
    _recall(batched, 3)

    assert review_mod.promote(q_manual) == manual
    outcomes = review_promote.promote_batch([batched])
    assert outcomes == [(batched, f"promoted (queue {q_batched} resolved)")]

    assert _item_state(manual) == _item_state(batched) == ("active", "verified")
    m_resolved, m_detail = _row(q_manual)
    b_resolved, b_detail = _row(q_batched)
    assert m_resolved and b_resolved, "both paths resolve the open row"
    assert m_detail == b_detail == {"rejected_count": 0}, "neither path rewrites detail"


def test_apply_without_an_open_row_mints_an_audit_row_through_the_chokepoint(kb):
    item, qid = _unverified("rowless")
    _recall(item, 3)
    review_mod.resolve_review(qid)  # e.g. quiet-expired / dismissed earlier

    (outcome,) = review_promote.promote_batch([item])
    assert outcome[0] == item and outcome[1].startswith("promoted (audit row ")

    assert _item_state(item) == ("active", "verified")
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, resolved, detail FROM review_queue WHERE item_id = %s AND id <> %s",
            (item, qid),
        ).fetchall()
    assert len(rows) == 1, "exactly one audit row was minted"
    minted_qid, resolved, detail = rows[0]
    assert resolved is True and detail["promote_program"]["minted"] is True
    assert f"audit row {minted_qid} minted + resolved" in outcome[1]


def test_batch_per_item_isolation_and_eligibility_walls(kb):
    good, q_good = _unverified("iso-good")
    _recall(good, 3)
    verified = ingest("pp", "note", "t iso-v", "b iso-v", uri="iso-v")
    retired, _q = _unverified("iso-retired")
    with db.transaction() as conn:
        conn.execute("UPDATE items SET status = 'archived' WHERE id = %s", (retired,))

    outcomes = dict(review_promote.promote_batch([good, verified, retired, 999999, good]))

    assert outcomes[good] == f"promoted (queue {q_good} resolved)", "one bad id never aborts"
    assert outcomes[verified] == f"error: item {verified} is not unverified (nothing to promote)"
    assert outcomes[retired] == f"error: item {retired} is not active (status=archived)"
    assert outcomes[999999] == "error: item 999999 does not exist"
    assert len(outcomes) == 4, "a duplicated id is reported once"
    assert _item_state(good) == ("active", "verified")
    assert _item_state(verified) == ("active", "verified"), "already verified: untouched"
    assert _item_state(retired) == ("archived", "unverified"), "retired knowledge: untouched"


# --- 4. the CLI: dry-run default, confirm gate, read-only table ---------------


def test_cli_promote_candidates_prints_the_table_and_writes_nothing(kb):
    from kbcli.main import app

    item, _q = _unverified("cli-table", type_="gotcha")
    _recall(item, 4)
    before = _snapshot()

    res = runner.invoke(app, ["review", "promote-candidates"])

    assert res.exit_code == 0, res.output
    assert "rank" in res.stdout and "recalls" in res.stdout
    assert str(item) in res.stdout and "t cli-table" in res.stdout and "gotcha" in res.stdout
    assert _snapshot() == before, "the table is read-only"

    res = runner.invoke(app, ["review", "promote-candidates", "--min-recalls", "9"])
    assert res.exit_code == 0 and "no promote candidates (recall floor 9)" in res.stdout


def test_cli_promote_batch_defaults_to_a_dry_run(kb):
    from kbcli.main import app

    item, qid = _unverified("cli-dry")
    _recall(item, 3)
    before = _snapshot()

    res = runner.invoke(app, ["review", "promote-batch"])

    assert res.exit_code == 0, res.output
    assert f"[{item}] would promote (queue {qid} resolves)" in res.stdout
    assert "dry run: 1 item(s) would promote" in res.stdout
    assert _snapshot() == before, "the default path must not write"


def test_cli_promote_batch_confirm_declined_writes_nothing(kb):
    from kbcli.main import app

    item, _qid = _unverified("cli-decline")
    _recall(item, 3)
    before = _snapshot()

    res = runner.invoke(app, ["review", "promote-batch", "--apply"], input="n\n")

    assert res.exit_code == 1, "abort=True exits nonzero on a declined confirm"
    assert _snapshot() == before, "declining the prompt must write ZERO rows"
    assert _item_state(item) == ("active", "unverified")


def test_cli_promote_batch_apply_confirmed_promotes_and_supports_ids(kb):
    from kbcli.main import app

    a, qa = _unverified("cli-a")
    _recall(a, 3)
    b, _qb = _unverified("cli-b")
    _recall(b, 3)

    res = runner.invoke(app, ["review", "promote-batch", "--ids", str(a), "--apply"], input="y\n")

    assert res.exit_code == 0, res.output
    assert f"[{a}] promoted (queue {qa} resolved)" in res.stdout
    assert "promote-batch: 1 promoted, 0 error(s), 1 item(s)" in res.stdout
    assert _item_state(a) == ("active", "verified")
    assert _item_state(b) == ("active", "unverified"), "--ids selects EXACTLY those items"

    res = runner.invoke(app, ["review", "promote-batch", "--ids", "1,x"])
    assert res.exit_code == 1, "malformed --ids is refused before any DB work"


def test_cli_promote_batch_with_nothing_eligible_never_prompts(kb):
    from kbcli.main import app

    res = runner.invoke(app, ["review", "promote-batch", "--apply"])
    assert res.exit_code == 0, res.output
    assert "nothing to promote" in res.stdout


# --- 5. the nightly only COUNTS (curation_counts.promote_candidates) ----------


def test_curation_counts_gains_promote_candidates_as_an_added_key(kb):
    a, _qa = _unverified("cc-a")
    _recall(a, 3)
    b, _qb = _unverified("cc-b")
    _recall(b, 5)
    below, _qc = _unverified("cc-below")
    _recall(below, 1)

    counts = review_auto.curation_counts(30)

    assert counts["promote_candidates"] == 2, "counted at the DEFAULT threshold"
    # ADDED, never renamed: the existing telemetry keys all still ride along.
    for key in (
        "triage_awaiting_apply",
        "reject_suggestions_pending",
        "never_triaged_open",
        "first_expiry_date",
        "contradiction_suggestions_pending",
        "untriaged_expiring_7d",
        "untriaged_expiring_30d",
        "eligible_entered_1d",
        "eligible_left_1d",
        "eligible_flow_net_1d",
    ):
        assert key in counts, f"existing key {key} must survive the promote program"

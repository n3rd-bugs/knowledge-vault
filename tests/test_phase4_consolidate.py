"""Phase 4 — nightly consolidation: dedupe, summaries, contradictions, staleness,
raw immutability, idempotency, advisory lock (PLAN.md §7.3 / §11 Phase 4).

Real DB in kb_test; the embedder is mocked by conftest's fake_embeddings and the
14B extractor (llm.summarize_items / items_contradict) is mocked here — the real
Ollama extractor is never called (Invariant 8, coordinator rule).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kbcore import consolidation, db, llm
from kbcore.config import settings
from kbcore.ingest import ingest
from kbcore.items import get_item, log_access
from kbworker import consolidate


@pytest.fixture(autouse=True)
def _mock_extractor(monkeypatch, tmp_path):
    """Mock the 14B extractor and keep raw/disk reads off the real ~/kb."""
    monkeypatch.setattr(
        llm, "summarize_items", lambda topic, items: f"Summary of {topic} ({len(items)} items)."
    )
    monkeypatch.setattr(llm, "items_contradict", lambda *a, **k: False)
    monkeypatch.setattr(settings, "kb_raw", tmp_path / "kbraw")


# --- helpers -----------------------------------------------------------------


def _vec(*head: float) -> str:
    """A pgvector literal: the given leading components, zero-padded to the dim."""
    v = [float(x) for x in head] + [0.0] * (settings.kb_embed_dim - len(head))
    return "[" + ",".join(repr(x) for x in v) + "]"


def _force_lead(item_id: int, vec: str) -> None:
    """Overwrite an item's lead-chunk embedding (test-only, deterministic cosines)."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE chunks SET embedding = %s::vector WHERE item_id = %s AND ord = 0",
            (vec, item_id),
        )


def _status(item_id: int) -> str:
    with db.connect() as conn:
        return conn.execute("SELECT status FROM items WHERE id = %s", (item_id,)).fetchone()[0]


def _has_relation(src: int, rel: str, dst: int) -> bool:
    with db.connect() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM relations "
                "WHERE src_id=%s AND rel=%s AND dst_id=%s AND src_kind='item'",
                (src, rel, dst),
            ).fetchone()
            is not None
        )


def _age(item_id: int, days: int) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE items SET created_at = now() - make_interval(days => %s), "
            "updated_at = now() - make_interval(days => %s) WHERE id = %s",
            (days, days, item_id),
        )


# --- step 1: dedupe ----------------------------------------------------------


def test_dedupe_collapses_cluster_to_oldest_verified(kb):
    # A is oldest but UNVERIFIED; B,C are verified → canonical is the oldest verified (B).
    a = ingest("p", "note", "alpha", "distinct body alpha", uri="a", trust="unverified")
    b = ingest("p", "note", "bravo", "distinct body bravo", uri="b", trust="verified")
    c = ingest("p", "note", "charlie", "distinct body charlie", uri="c", trust="verified")
    for i in (a, b, c):
        _force_lead(i, _vec(1.0))  # identical vectors → cosine 1.0 >= threshold

    result = consolidate.main()

    assert result["duplicates_marked"] == 2
    assert _status(b) == "active", "oldest verified is canonical"
    assert _status(a) == "duplicate" and _status(c) == "duplicate"
    assert _has_relation(a, "derived_from", b)
    assert _has_relation(c, "derived_from", b)


def test_dedupe_never_collapses_reports_or_summaries(kb):
    # Consolidation OUTPUT must be exempt from dedup/contradiction: nightly
    # reports are ~0.98-cosine boilerplate and would otherwise demote each other,
    # collapsing the audit trail (review finding). Two active reports + two active
    # summaries with IDENTICAL lead vectors must all stay active.
    r1 = ingest("kb-system", "report", "Consolidation report 2026-07-01", "counts A", uri="r1")
    r2 = ingest("kb-system", "report", "Consolidation report 2026-07-02", "counts B", uri="r2")
    s1 = ingest("kb-system", "summary", "Summary — lang:python", "python summary", uri="s1")
    s2 = ingest("kb-system", "summary", "Summary — lang:python", "python summary", uri="s2")
    for i in (r1, r2, s1, s2):
        _force_lead(i, _vec(1.0))  # cosine 1.0 across all four

    consolidate.main()

    assert consolidation.duplicate_pairs("kb-system", 0.5) == [], "output types excluded from dedup"
    assert all(_status(i) == "active" for i in (r1, r2, s1, s2)), "no report/summary demoted"


def test_second_run_marks_no_new_duplicates(kb):
    a = ingest("p", "note", "one", "body one", uri="a")
    b = ingest("p", "note", "two", "body two", uri="b")
    for i in (a, b):
        _force_lead(i, _vec(1.0))

    first = consolidate.main()
    assert first["duplicates_marked"] == 1
    active_after = [i for i in (a, b) if _status(i) == "active"]
    assert len(active_after) == 1

    second = consolidate.main()
    assert second["duplicates_marked"] == 0, "already-deduped cluster does no new work"
    assert [i for i in (a, b) if _status(i) == "active"] == active_after


# --- step 2: summaries -------------------------------------------------------


def _summary_ids(project: str, tag: str, status: str = "active") -> list[int]:
    with db.connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT id FROM items WHERE project=%s AND type='summary' AND status=%s "
                "AND meta->>'topic'=%s ORDER BY id",
                (project, status, tag),
            ).fetchall()
        ]


def test_summaries_link_all_sources_and_supersede_on_refresh(kb):
    tag = "hw:tps55288"
    first_batch = [
        ingest(
            "p", "decision", f"d{i}", f"decision body number {i} about the buck-boost", tags=[tag]
        )
        for i in range(3)
    ]

    consolidate.main()
    summaries = _summary_ids("p", tag)
    assert len(summaries) == 1, "one summary for a tag with >=3 items"
    s1 = summaries[0]
    for src in first_batch:
        assert _has_relation(s1, "derived_from", src), "derived_from links every source"

    # Three MORE items for the same tag → the second run refreshes (supersedes).
    second_batch = [
        ingest("p", "decision", f"e{i}", f"extra decision body {i} on tps55288 rails", tags=[tag])
        for i in range(3)
    ]
    consolidate.main()

    active = _summary_ids("p", tag, "active")
    assert len(active) == 1, "refresh supersedes, never duplicates, the summary"
    s2 = active[0]
    assert s2 != s1
    assert get_item(s1).status == "superseded"
    assert get_item(s1).superseded_by == s2
    for src in first_batch + second_batch:
        assert _has_relation(s2, "derived_from", src), "refreshed summary links all sources"


# --- step 3: contradictions --------------------------------------------------


def test_contradiction_pair_is_flagged_and_queued(kb, monkeypatch):
    monkeypatch.setattr(llm, "items_contradict", lambda *a, **k: True)
    a = ingest("p", "decision", "use mode 0", "The bus runs in SPI mode 0.", uri="a")
    b = ingest("p", "decision", "use mode 1", "The bus runs in SPI mode 1.", uri="b")
    lo, hi = min(a, b), max(a, b)
    _force_lead(a, _vec(1.0))
    _force_lead(b, _vec(0.88, 0.4749736))  # cosine 0.88 ∈ [0.80, 0.95)

    result = consolidate.main()

    assert result["contradictions_queued"] == 1
    assert _has_relation(lo, "contradicts", hi)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT item_id, detail FROM review_queue "
            "WHERE reason='contradiction' AND resolved=false"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == lo
    assert {rows[0][1]["a_id"], rows[0][1]["b_id"]} == {lo, hi}
    assert _status(lo) == "active" and _status(hi) == "active", "never auto-superseded"


# --- step 4: staleness -------------------------------------------------------


def test_archive_stale_respects_access_and_exempts_summaries(kb):
    before = datetime.now(UTC) - timedelta(days=settings.kb_archive_after_days)
    old_unused = ingest("p", "note", "old", "an old untouched note", uri="o")
    old_accessed = ingest("p", "note", "seen", "an old but recently read note", uri="s")
    fresh = ingest("p", "note", "new", "a brand new note", uri="n")
    old_summary = ingest("p", "summary", "sum", "a derived summary body", uri="sum")
    for i in (old_unused, old_accessed, old_summary):
        _age(i, 200)
    log_access(old_accessed, "kb_get")  # recent access → keeps it active

    archived = consolidation.archive_stale(before)

    assert archived == 1
    assert _status(old_unused) == "archived"
    assert _status(old_accessed) == "active", "recent access keeps it active"
    assert _status(fresh) == "active"
    assert _status(old_summary) == "active", "summaries are exempt from staleness"


# --- invariants: raw immutability + report ----------------------------------


def _all_bodies() -> dict[int, str]:
    with db.connect() as conn:
        return dict(conn.execute("SELECT id, body FROM items").fetchall())


def test_raw_bodies_are_byte_identical_after_consolidation(kb):
    tag = "protocol:spi"
    dup_a = ingest("p", "note", "dupA", "same-vector body A", uri="a")
    dup_b = ingest("p", "note", "dupB", "same-vector body B", uri="b")
    _force_lead(dup_a, _vec(1.0))
    _force_lead(dup_b, _vec(1.0))
    for i in range(3):
        ingest("p", "gotcha", f"g{i}", f"gotcha body {i} about spi framing", tags=[tag])

    before = _all_bodies()
    consolidate.main()
    after = _all_bodies()

    for item_id, body in before.items():
        assert after[item_id] == body, f"item {item_id} body must be immutable (Invariant 1)"


def test_report_item_written_with_counts(kb):
    a = ingest("p", "note", "one", "body one", uri="a")
    b = ingest("p", "note", "two", "body two", uri="b")
    for i in (a, b):
        _force_lead(i, _vec(1.0))

    result = consolidate.main()

    assert result["skipped"] is False
    report = get_item(result["report_item_id"])
    assert report.type == "report" and report.project == "kb-system"
    assert report.meta["duplicates_marked"] == 1
    assert "review_queue_open" in report.meta and "disk_pct" in report.meta


# --- advisory lock -----------------------------------------------------------


def test_advisory_lock_makes_concurrent_run_skip(kb):
    ingest("p", "note", "x", "some body", uri="x")
    with db.advisory_lock(consolidate.ADVISORY_KEY) as acquired:
        assert acquired, "test holds the lock"
        result = consolidate.main()  # main() opens its own connection → cannot acquire
    assert result == {"skipped": True}
    with db.connect() as conn:
        reports = conn.execute("SELECT count(*) FROM items WHERE type='report'").fetchone()[0]
    assert reports == 0, "skipped run does no work (no report written)"


def test_every_step_is_timed_in_the_run_report(kb, monkeypatch):
    """A pass ran over an hour producing nothing and there was no way to say
    which step consumed it. Durations now ride the report, which is persisted in
    consolidation_requests.result — so the question is answerable from data on
    the NEXT occurrence instead of by instrumenting after the fact."""
    from kbworker import consolidate

    counts: dict = {}
    consolidate._run_step(counts, "fast_step", lambda: 7)
    assert counts["fast_step"] == 7
    assert "fast_step" in counts["step_secs"]
    assert counts["step_secs"]["fast_step"] >= 0


def test_a_failing_step_is_still_timed(kb):
    """A step that blows up is exactly the one whose duration matters — timing
    it in a finally means a crash cannot hide how long it burned first."""
    from kbworker import consolidate

    counts: dict = {}

    def boom():
        raise RuntimeError("step exploded")

    consolidate._run_step(counts, "bad_step", boom)
    assert "bad_step" not in counts, "a failed step must not report a count"
    assert "bad_step" in counts["step_secs"], "a failed step was not timed"

"""last_consolidation_at must ignore non-consolidation reports (KB item 942 §2).

Deep-search (and manual kb_save) also writes ``type='report'`` items into
kb-system WITHOUT ``meta.consolidation``; matching on type alone let such a
report poison the worker's ``since`` mark so the next nightly summarize step
missed new items. The query now requires ``meta->>'consolidation' = 'true'``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from kbcore import consolidation, db
from kbcore.ingest import ingest


def _set_created(item_id: int, ts: datetime) -> None:
    with db.transaction() as conn:  # deterministic timestamps, kb_test-owned tweak
        conn.execute("UPDATE items SET created_at = %s WHERE id = %s", (ts, item_id))


def test_last_consolidation_at_ignores_foreign_reports(kb):
    cons_at = datetime(2026, 7, 1, 3, 0, tzinfo=UTC)
    # A real nightly report — same meta shape as kbworker.consolidate._write_report.
    cons = ingest(
        "kb-system",
        "report",
        "Consolidation report 2026-07-01",
        "Nightly consolidation report\n\n- duplicates_marked: 2",
        kind="chat",
        uri="report:2026-07-01",
        meta={"consolidation": True, "duplicates_marked": 2},
    )
    _set_created(cons, cons_at)
    # A NEWER deep-search-style report: type='report' but NO consolidation key.
    ds = ingest(
        "kb-system",
        "report",
        "Deep search: pgvector index ops",
        "deep-search findings",
        kind="chat",
        uri="deepsearch:2026-07-10",
        trust="unverified",
        meta={"deep_search": "pgvector index ops", "sources": []},
    )
    _set_created(ds, datetime(2026, 7, 10, 12, 0, tzinfo=UTC))
    # And an even newer report carrying consolidation=false explicitly.
    off = ingest(
        "kb-system",
        "report",
        "Some other report",
        "not a consolidation run",
        uri="report:other",
        meta={"consolidation": False},
    )
    _set_created(off, datetime(2026, 7, 15, 12, 0, tzinfo=UTC))

    # `since` = the older CONSOLIDATION report — the newer foreign reports don't poison it.
    assert consolidation.last_consolidation_at() == cons_at


def test_last_consolidation_at_none_without_consolidation_reports(kb):
    assert consolidation.last_consolidation_at() is None  # no reports at all (first run)
    ingest(
        "kb-system",
        "report",
        "Deep search: only foreign reports exist",
        "deep-search body",
        uri="deepsearch:only",
        trust="unverified",
        meta={"deep_search": "q"},
    )
    # Foreign reports alone still mean "never consolidated" (summarize floors to epoch).
    assert consolidation.last_consolidation_at() is None

"""kbcore.stats — corpus counters for kb_stats (PLAN.md §6 Phase 5).

Read-only aggregate over the knowledge base: totals by type/project/trust/status,
entity+relation counts, open review queue, chunk count, embed model, and the last
consolidation report date. All SQL here (Invariant 2).
"""

from __future__ import annotations

from typing import Any

from kbcore import scope
from kbcore.config import settings
from kbcore.db import connect

_SUB = "(SELECT name FROM projects WHERE public IS TRUE)"


def stats() -> dict[str, Any]:
    """Snapshot of corpus health/size for kb_stats.

    Under a public-only instance every counter is scoped to public projects, so
    private/NDA volume never leaks through aggregate numbers.
    """
    po = scope.public_only()
    # Constant SQL fragments (no user input); empty in full-access mode.
    iw = f"WHERE project IN {_SUB}" if po else ""  # items / entities / relations
    jw = f"WHERE i.project IN {_SUB}" if po else ""  # joined items alias `i`
    ja = f"AND i.project IN {_SUB}" if po else ""  # append to an existing WHERE
    pa = f"AND project IN {_SUB}" if po else ""  # append (bare project column)
    with connect() as conn:
        by_type = dict(
            conn.execute(f"SELECT type, count(*) FROM items {iw} GROUP BY type").fetchall()  # noqa: S608
        )
        by_project = dict(
            conn.execute(
                f"SELECT project, count(*) FROM items {iw} GROUP BY project ORDER BY count(*) DESC"  # noqa: S608
            ).fetchall()
        )
        by_status = dict(
            conn.execute(f"SELECT status, count(*) FROM items {iw} GROUP BY status").fetchall()  # noqa: S608
        )
        by_trust = dict(
            conn.execute(
                "SELECT COALESCE(s.trust, 'verified'), count(*) "
                f"FROM items i LEFT JOIN sources s ON s.id = i.source_id {jw} GROUP BY 1"  # noqa: S608
            ).fetchall()
        )
        items_total = conn.execute(f"SELECT count(*) FROM items {iw}").fetchone()[0]  # noqa: S608
        chunks = conn.execute(
            f"SELECT count(*) FROM chunks c JOIN items i ON i.id = c.item_id {jw}"  # noqa: S608
        ).fetchone()[0]
        entities = conn.execute(f"SELECT count(*) FROM entities {iw}").fetchone()[0]  # noqa: S608
        relations = conn.execute(f"SELECT count(*) FROM relations {iw}").fetchone()[0]  # noqa: S608
        review_open = conn.execute(
            "SELECT count(*) FROM review_queue rq LEFT JOIN items i ON i.id = rq.item_id "
            f"WHERE rq.resolved = false {ja}"  # noqa: S608
        ).fetchone()[0]
        last_report = conn.execute(
            "SELECT max(created_at) FROM items "
            f"WHERE project = 'kb-system' AND type = 'report' {pa}"  # noqa: S608
        ).fetchone()[0]
        # Guarded in a separate statement so kb_stats survives migration 002 not
        # yet being applied (a missing table would fail the whole query at parse).
        queue_exists = conn.execute("SELECT to_regclass('ingest_queue')").fetchone()[0]
        queue_pending = (
            conn.execute(
                f"SELECT count(*) FROM ingest_queue WHERE status = 'pending' {pa}"  # noqa: S608
            ).fetchone()[0]
            if queue_exists
            else 0
        )
    return {
        "items_total": items_total,
        "chunks": chunks,
        "entities": entities,
        "relations": relations,
        "review_queue_open": review_open,
        "ingest_queue_pending": queue_pending,
        "embed_model": settings.kb_embed_model,
        "last_consolidation": last_report.isoformat() if last_report else None,
        "by_type": by_type,
        "by_status": by_status,
        "by_trust": by_trust,
        "by_project": by_project,
    }

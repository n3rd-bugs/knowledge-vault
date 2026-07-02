"""kbcore.stats — corpus counters for kb_stats (PLAN.md §6 Phase 5).

Read-only aggregate over the knowledge base: totals by type/project/trust/status,
entity+relation counts, open review queue, chunk count, embed model, and the last
consolidation report date. All SQL here (Invariant 2).
"""

from __future__ import annotations

from typing import Any

from kbcore.config import settings
from kbcore.db import connect


def stats() -> dict[str, Any]:
    """Snapshot of corpus health/size for kb_stats."""
    with connect() as conn:
        by_type = dict(conn.execute("SELECT type, count(*) FROM items GROUP BY type").fetchall())
        by_project = dict(
            conn.execute(
                "SELECT project, count(*) FROM items GROUP BY project ORDER BY count(*) DESC"
            ).fetchall()
        )
        by_status = dict(
            conn.execute("SELECT status, count(*) FROM items GROUP BY status").fetchall()
        )
        by_trust = dict(
            conn.execute(
                "SELECT COALESCE(s.trust, 'verified'), count(*) "
                "FROM items i LEFT JOIN sources s ON s.id = i.source_id GROUP BY 1"
            ).fetchall()
        )
        scalar = conn.execute(
            """
            SELECT (SELECT count(*) FROM items),
                   (SELECT count(*) FROM chunks),
                   (SELECT count(*) FROM entities),
                   (SELECT count(*) FROM relations),
                   (SELECT count(*) FROM review_queue WHERE resolved = false),
                   (SELECT max(created_at) FROM items
                    WHERE project = 'kb-system' AND type = 'report')
            """
        ).fetchone()
        # Guarded in a separate statement so kb_stats survives migration 002 not
        # yet being applied (a missing table would fail the whole query at parse).
        queue_exists = conn.execute("SELECT to_regclass('ingest_queue')").fetchone()[0]
        queue_pending = (
            conn.execute("SELECT count(*) FROM ingest_queue WHERE status = 'pending'").fetchone()[0]
            if queue_exists
            else 0
        )
    items_total, chunks, entities, relations, review_open, last_report = scalar
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

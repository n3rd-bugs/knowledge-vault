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


def stats() -> dict[str, Any]:
    """Snapshot of corpus health/size for kb_stats.

    Every counter is scoped to the projects the request may read (public-only
    instance AND/OR grantee grants), so private/NDA volume never leaks through
    aggregate numbers. Fragments are constant; the grant project list rides params.
    """
    proj_frag, proj_params = scope.project_predicate("project")  # bare project column
    iproj_frag, iproj_params = scope.project_predicate("i.project")  # joined items alias
    iw = f"WHERE {proj_frag}" if proj_frag else ""  # items / entities / relations
    jw = f"WHERE {iproj_frag}" if iproj_frag else ""  # joined items alias `i`
    ja = f"AND {iproj_frag}" if iproj_frag else ""  # append to an existing WHERE
    pa = f"AND {proj_frag}" if proj_frag else ""  # append (bare project column)
    with connect() as conn:
        by_type = dict(
            conn.execute(
                f"SELECT type, count(*) FROM items {iw} GROUP BY type",
                proj_params,  # noqa: S608
            ).fetchall()
        )
        by_project = dict(
            conn.execute(
                f"SELECT project, count(*) FROM items {iw} GROUP BY project ORDER BY count(*) DESC",  # noqa: S608
                proj_params,
            ).fetchall()
        )
        by_status = dict(
            conn.execute(
                f"SELECT status, count(*) FROM items {iw} GROUP BY status",
                proj_params,  # noqa: S608
            ).fetchall()
        )
        by_trust = dict(
            conn.execute(
                "SELECT COALESCE(s.trust, 'verified'), count(*) "
                f"FROM items i LEFT JOIN sources s ON s.id = i.source_id {jw} GROUP BY 1",  # noqa: S608
                iproj_params,
            ).fetchall()
        )
        items_total = conn.execute(f"SELECT count(*) FROM items {iw}", proj_params).fetchone()[0]  # noqa: S608
        chunks = conn.execute(
            f"SELECT count(*) FROM chunks c JOIN items i ON i.id = c.item_id {jw}",  # noqa: S608
            iproj_params,
        ).fetchone()[0]
        entities = conn.execute(f"SELECT count(*) FROM entities {iw}", proj_params).fetchone()[0]  # noqa: S608
        relations = conn.execute(f"SELECT count(*) FROM relations {iw}", proj_params).fetchone()[0]  # noqa: S608
        review_open = conn.execute(
            "SELECT count(*) FROM review_queue rq LEFT JOIN items i ON i.id = rq.item_id "
            f"WHERE rq.resolved = false {ja}",  # noqa: S608
            iproj_params,
        ).fetchone()[0]
        last_report = conn.execute(
            "SELECT max(created_at) FROM items "
            f"WHERE project = 'kb-system' AND type = 'report' {pa}",  # noqa: S608
            proj_params,
        ).fetchone()[0]
        # Guarded in a separate statement so kb_stats survives migration 002 not
        # yet being applied (a missing table would fail the whole query at parse).
        queue_exists = conn.execute("SELECT to_regclass('ingest_queue')").fetchone()[0]
        queue_pending = (
            conn.execute(
                f"SELECT count(*) FROM ingest_queue WHERE status = 'pending' {pa}",  # noqa: S608
                proj_params,
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

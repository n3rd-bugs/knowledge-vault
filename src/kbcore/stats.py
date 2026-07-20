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
        # --- Phase-3 aggregates (WEBUI_TIERED_ACCESS_PLAN §5 P3) -------------
        # Every new series composes the SAME project predicate as the counters
        # above, so a grantee's trend/tag numbers can never include withheld
        # projects (AC: a one-project grantee's items_per_week sums equal its
        # by_project count — no unscoped residual). Deliberately unwindowed:
        # the whole-series sum must equal items_total for that invariant.
        items_per_week = [
            {"week": r[0].date().isoformat(), "count": int(r[1])}
            for r in conn.execute(
                "SELECT date_trunc('week', created_at) AS wk, count(*) "
                f"FROM items {iw} GROUP BY wk ORDER BY wk",  # noqa: S608
                proj_params,
            ).fetchall()
        ]
        top_tags = dict(
            conn.execute(
                "SELECT tag, count(*) FROM items i, LATERAL unnest(i.tags) AS tag "
                f"{jw} GROUP BY tag ORDER BY count(*) DESC, tag LIMIT 20",  # noqa: S608
                iproj_params,
            ).fetchall()
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
        "items_per_week": items_per_week,
        "top_tags": top_tags,
    }


def usage_stats(weeks: int = 12) -> dict[str, Any]:
    """Access-log-derived analytics — OWNER-ONLY (plan §5 Phase 3).

    Recall patterns reveal which items/queries OTHER principals touch (the
    ``principal`` audit column, migration 011), so this never ships to grantees:
    ``require_owner`` is the wall, the same one the review tools use. All SQL
    parameterized; ``weeks`` bounds the trend window (clamped).
    """
    scope.require_owner("usage analytics")
    wk = max(1, min(int(weeks), 104))
    with connect() as conn:
        total = conn.execute("SELECT count(*) FROM access_log").fetchone()[0]
        per_week = [
            {"week": r[0].date().isoformat(), "count": int(r[1])}
            for r in conn.execute(
                "SELECT date_trunc('week', ts) AS wk, count(*) FROM access_log "
                "WHERE ts >= now() - make_interval(weeks => %s) GROUP BY wk ORDER BY wk",
                (wk,),
            ).fetchall()
        ]
        by_tool = dict(
            conn.execute("SELECT tool, count(*) FROM access_log GROUP BY tool").fetchall()
        )
        by_principal = dict(
            conn.execute(
                "SELECT COALESCE(principal, '(owner)'), count(*) "
                "FROM access_log GROUP BY 1 ORDER BY count(*) DESC"
            ).fetchall()
        )
        top_items = [
            {"item_id": r[0], "title": r[1], "count": int(r[2])}
            for r in conn.execute(
                "SELECT al.item_id, i.title, count(*) FROM access_log al "
                "JOIN items i ON i.id = al.item_id "
                "GROUP BY al.item_id, i.title ORDER BY count(*) DESC, al.item_id LIMIT 10"
            ).fetchall()
        ]
    return {
        "events_total": int(total),
        "events_per_week": per_week,
        "by_tool": by_tool,
        "by_principal": by_principal,
        "top_items": top_items,
    }

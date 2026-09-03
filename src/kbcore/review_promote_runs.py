"""kbcore.review_promote_runs — the promote-run LISTING (review #8 H13c).

Uptake of the promote program was zero on both surfaces for a week, because the
owner had to KNOW a run key before ``kb review promote-run <key>`` could show
anything. This module answers "which session runs of the last N days still hold
promotable items?", largest first, so the next step costs one paste.

READ-ONLY and OWNER-ONLY, like :func:`kbcore.review_promote.promote_run` (a
listing of ``sources.uri`` values names projects across the whole KB, so it is
not for grantees). Nothing here sets ``trust`` — V4 IS LAW; the promotion itself
still runs through ``kbcore.review.promote`` via the CLI's pinned preview. Lives
beside :mod:`kbcore.review_promote` rather than in it because that file sits at
its 300-line cap; it imports the candidate-type exclusion from there so the two
surfaces can never disagree about what counts as promotable.
"""

from __future__ import annotations

from typing import Any

from kbcore import scope
from kbcore.db import connect
from kbcore.review_promote import CANDIDATE_EXCLUDED_TYPES

# One grouped scan of the last ``days`` days of session sources. "candidates" is
# exactly promote_run's predicate (active + unverified + not consolidation output);
# a run with none is not listed. ``ingested_at`` is when the run's items were
# minted (kbworker.sessions._ingest_and_queue), so it IS the run's night. Constant
# SQL text, every value bound (Invariant 2).
_RUNS_SQL = """
    SELECT s.uri,
           count(*) FILTER (WHERE i.status = 'active' AND s.trust = 'unverified'
                              AND i.type <> ALL(%(excluded)s)) AS candidates,
           count(*) AS items,
           max(s.ingested_at) AS last_seen,
           array_agg(DISTINCT i.project ORDER BY i.project) AS projects
      FROM sources s
      JOIN items i ON i.source_id = s.id
     WHERE s.kind = 'session'
       AND s.ingested_at >= now() - make_interval(days => %(days)s)
     GROUP BY s.uri
    HAVING count(*) FILTER (WHERE i.status = 'active' AND s.trust = 'unverified'
                              AND i.type <> ALL(%(excluded)s)) > 0
     ORDER BY 2 DESC, 4 DESC, s.uri
     LIMIT %(limit)s
"""


def list_promote_runs(days: int = 7, *, limit: int = 20) -> list[dict[str, Any]]:
    """Session runs of the last ``days`` days that still hold promotable items.

    Each row is ``{run_key, candidates, items, last_seen, projects}`` — ``run_key``
    is the stored ``sources.uri`` (what ``kb review promote-run`` takes verbatim),
    ``candidates`` the active+unverified non-consolidation items promote_run would
    list, ``items`` every item the run minted, ``last_seen`` a UTC timestamp and
    ``projects`` the sorted distinct projects it wrote into. Largest run first,
    then most recent. READ-ONLY; bounded (``limit`` floor 1, ``days`` floor 1).
    """
    scope.require_owner("promote-runs")
    params = {
        "excluded": list(CANDIDATE_EXCLUDED_TYPES),
        "days": max(1, int(days)),
        "limit": max(1, int(limit)),
    }
    with connect() as conn:
        rows = conn.execute(_RUNS_SQL, params).fetchall()
    return [
        {
            "run_key": r[0],
            "candidates": int(r[1]),
            "items": int(r[2]),
            "last_seen": r[3],
            "projects": list(r[4] or []),
        }
        for r in rows
    ]


def promote_runs_summary(days: int = 1) -> dict[str, Any]:
    """The digest's one-line view: ``{runs, items, largest_key, largest_items}``.

    ``runs`` / ``items`` are the runs of the last ``days`` days with promotable
    items and their total; ``largest_key`` names the biggest so the digest line
    'promote-run ready: N runs / M items' can carry a paste-ready key (H13c —
    the render itself is the digest's, this is the number). READ-ONLY.
    """
    rows = list_promote_runs(days, limit=1000)
    return {
        "runs": len(rows),
        "items": sum(r["candidates"] for r in rows),
        "largest_key": rows[0]["run_key"] if rows else None,
        "largest_items": rows[0]["candidates"] if rows else 0,
    }

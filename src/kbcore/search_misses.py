"""kbcore.search_misses — zero-hit search telemetry (review #4 Wave E, KB 21332).

The KB could not see its own gaps: a search that returned NOTHING left no
trace, so the demand side — what agents ASK for and never get — was invisible
while the supply side (access_log) was fully instrumented. kbcore.search
records every zero-hit query here (migration 018) and the surfaces read it
back: the nightly digest counts the last day's misses ('search misses: N',
positive-only — kbworker.digest) and the weekly trend report tables the week's
top misses by count (kbworker.weekly) — the cheapest possible signal for what
knowledge is worth capturing next.

Rules of the road:

* :func:`record` is BEST-EFFORT and never raises — telemetry must never fail
  the search that produced it (same posture as kbcore.notify).
* The EVAL HARNESS never records: it replays golden queries with ``tool=None``,
  the same exemption the access log applies, so replayed misses cannot
  masquerade as demand. Callers of :func:`record` therefore always pass a
  non-None tool (kbcore.search's zero-hit hook sits inside its
  ``tool is not None`` gate).
* Query text is bounded to :data:`MAX_QUERY_CHARS` — a runaway caller must not
  turn a telemetry row into a blob store.
* RETENTION (the carve-out documented in migration 018, in the spirit of the
  enrolment-code sweep): these rows are telemetry, not knowledge, so
  :func:`sweep_old` MAY delete rows older than :data:`RETENTION_DAYS`. The
  weekly pass calls it; never extend the carve-out to knowledge tables.
"""

from __future__ import annotations

import logging

from kbcore.db import connect, transaction

logger = logging.getLogger("kbcore.search_misses")

MAX_QUERY_CHARS = 500
RETENTION_DAYS = 90


def record(query: str, project: str | None, tool: str | None) -> bool:
    """Record one zero-hit search. Best-effort: returns False, NEVER raises.

    ``project`` is the search's project-scope filter (None = all projects);
    ``tool`` names the calling surface (``mcp.kb_search``, ``rest.search``, …).
    An empty/whitespace query or a None tool (the eval-harness marker) records
    nothing. The query is stripped and bounded to :data:`MAX_QUERY_CHARS`.
    """
    q = (query or "").strip()[:MAX_QUERY_CHARS]
    if not q or not tool:
        return False
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO search_misses (query, project_scope, tool) VALUES (%s, %s, %s)",
                (q, project, tool),
            )
    except Exception:  # noqa: BLE001 — telemetry must never fail the search
        logger.warning("search-miss telemetry write failed", exc_info=True)
        return False
    return True


def count_since(days: float = 1.0) -> int:
    """Misses recorded in the last ``days`` (fractional ok; the digest uses 1)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT count(*) FROM search_misses WHERE at >= now() - make_interval(secs => %s)",
            (float(days) * 86400.0,),
        ).fetchone()
    return int(row[0])


def stats(days: float = 7.0) -> dict[str, int]:
    """``{total, distinct}`` misses over the window (the weekly's headline pair)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT count(*), count(DISTINCT query) FROM search_misses "
            "WHERE at >= now() - make_interval(secs => %s)",
            (float(days) * 86400.0,),
        ).fetchone()
    return {"total": int(row[0]), "distinct": int(row[1])}


def top_misses(days: float = 7.0, limit: int = 10) -> list[tuple[str, int]]:
    """The window's most-missed queries as ``[(query, count)]``, most-missed first.

    Ties break oldest-demand-first then lexically, so the table is stable run
    over run. Grouping is by EXACT query text — near-duplicate phrasings are a
    judgement for the reader, not for SQL.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT query, count(*) AS n FROM search_misses "
            "WHERE at >= now() - make_interval(secs => %s) "
            "GROUP BY query ORDER BY n DESC, min(at), query LIMIT %s",
            (float(days) * 86400.0, int(limit)),
        ).fetchall()
    return [(r[0], int(r[1])) for r in rows]


def sweep_old(days: int = RETENTION_DAYS) -> int:
    """Prune telemetry rows older than ``days``. Returns rows deleted.

    THE DOCUMENTED CARVE-OUT (migration 018 header; CLAUDE.md rule 5): this is
    telemetry, not knowledge — no item, source, chunk, relation or review row is
    ever touched. ``days <= 0`` is refused (a misconfigured sweep must degrade
    to 'prune nothing', never to 'prune everything'). Called from the weekly
    pass (kbworker.weekly), which reports the count as ``search_misses_swept``.
    """
    if days <= 0:
        return 0
    with transaction() as conn:
        rows = conn.execute(
            "DELETE FROM search_misses WHERE at < now() - make_interval(days => %s) RETURNING id",
            (int(days),),
        ).fetchall()
    if rows:
        logger.info("search-miss retention: pruned %d row(s) older than %dd", len(rows), days)
    return len(rows)

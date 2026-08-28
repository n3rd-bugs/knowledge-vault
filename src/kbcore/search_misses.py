"""kbcore.search_misses — search-miss telemetry (Wave E KB 21332; review #5 Wave F).

The KB could not see its own gaps: a search that returned NOTHING left no
trace, so the demand side — what agents ASK for and never get — was invisible
while the supply side (access_log) was fully instrumented. kbcore.search
records misses here (migrations 018 + 019) and the surfaces read them back:
the nightly digest counts the last day's empty-result searches (positive-only,
kbworker.digest) and the weekly trend report tables the week's top misses
(kbworker.weekly) — the cheapest possible signal for what knowledge is worth
capturing next. Three row subtypes (review #5 val-1):

* ``empty`` — the Wave-E zero-hit miss (catches filter-emptied searches).
* ``thin`` — results came back but the top answer was judged irrelevant: the
  rerank sidecar scored it below ``kb_miss_relevance_floor``, or (sidecar
  absent) the top fused candidate had no keyword/identifier-leg contribution.
  Recorded only while the floor dial is ON. NEVER a fused-score floor — a
  vector-only top-1 always scores >= 1/(RRF_K+1), so that condition is
  unreachable by construction (review #5 guard).
* ``calibration`` — NOT a miss; excluded from every default count. Two ways a
  row is minted: floor dial OFF (the calibration-week posture) records the
  would-be top relevance score on EVERY sidecar-scored search so a week of real
  data sizes the floor; floor dial ON (Wave G, review #6) keeps the score
  histogram alive with a DETERMINISTIC 1-in-:data:`CALIBRATION_SAMPLE_EVERY`
  sample of sidecar-scored searches — the 183-row floor-off distribution is
  frozen and ages out under the 90d retention, and without a live sample every
  future floor re-tune would cost another floor-off week. Sub-floor scores are
  all captured anyway (every 'thin' row carries its score), so thin rows plus
  the sample reconstruct the distribution. Consumers of THIN rows should
  require ``top_score IS NOT NULL`` — fallback-mode thins (sidecar absent) are
  provenance-heuristic only and carry no score.

Rules of the road:

* :func:`record` is BEST-EFFORT and never raises — telemetry must never fail
  the search that produced it (same posture as kbcore.notify).
* The EVAL HARNESS never records: it replays golden queries with ``tool=None``,
  the same exemption the access log applies, so replayed misses cannot
  masquerade as demand (kbcore.search's hook sits inside its
  ``tool is not None`` gate).
* NO RAW SECRET, ONE LINE (review #5 sec-1/sec-2, mirroring ingest_queue's
  'the durable queue must hold no raw secret' posture): :func:`record` scrubs
  BEFORE truncation — so truncation can never split a secret past a pattern —
  and collapses all whitespace, so a crafted multi-line query cannot forge
  lines in the weekly report's miss table. kbcore.search scrubs once upstream
  and feeds both sinks; this is the belt-and-suspenders at THIS sink.
* Query text is bounded to :data:`MAX_QUERY_CHARS` — a runaway caller must not
  turn a telemetry row into a blob store.
* RETENTION (the carve-out documented in migration 018, in the spirit of the
  enrolment-code sweep): these rows are telemetry, not knowledge, so
  :func:`sweep_old` MAY delete rows older than :data:`RETENTION_DAYS`. The
  weekly pass calls it; never extend the carve-out to knowledge tables.
"""

from __future__ import annotations

import logging

from kbcore import rerank
from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.scrub import scrub

logger = logging.getLogger("kbcore.search_misses")

MAX_QUERY_CHARS = 500
RETENTION_DAYS = 90

# One leg's (item_id, text) rows, exactly as kbcore.search_legs returns them.
Rows = list[tuple[int, str]]
# The subtypes that ARE misses; 'calibration' is deliberately outside this set.
_MISS_SUBTYPES = ("empty", "thin")

# Deterministic floor-ON calibration sampler (Wave G): a plain modulo on a
# monotonically increasing per-process counter — NEVER random.random(), so tests
# pin it exactly (reset ``_calibration_seen``). The counter ticks once per
# sidecar-scored search while the floor dial is ON; restarts only re-phase the
# sample, which is all a histogram needs.
CALIBRATION_SAMPLE_EVERY = 20
_calibration_seen = 0


def _calibration_due() -> bool:
    """Tick the sampler; True exactly once per :data:`CALIBRATION_SAMPLE_EVERY` ticks."""
    global _calibration_seen
    _calibration_seen += 1
    return _calibration_seen % CALIBRATION_SAMPLE_EVERY == 0


def record(
    query: str,
    project: str | None,
    tool: str | None,
    subtype: str = "empty",
    top_score: float | None = None,
    top_legs: str | None = None,
) -> bool:
    """Record one search-miss telemetry row. Best-effort: returns False, NEVER raises.

    ``project`` is the search's project-scope filter (None = all projects);
    ``tool`` names the calling surface (``mcp.kb_search``, ``rest.search``, …).
    An empty/whitespace query or a None tool (the eval-harness marker) records
    nothing. The query is whitespace-flattened to one line, defensively
    scrubbed BEFORE truncation (module docstring: no raw secret, no forged
    report lines), then bounded to :data:`MAX_QUERY_CHARS`. ``subtype`` /
    ``top_score`` / ``top_legs`` are the migration-019 columns; plain zero-hit
    callers need only the original three arguments.
    """
    q = " ".join((query or "").split())
    q, _ = scrub(q)
    q = q[:MAX_QUERY_CHARS]
    if not q or not tool:
        return False
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO search_misses (query, project_scope, tool, subtype, top_score, "
                "top_legs) VALUES (%s, %s, %s, %s, %s, %s)",
                (q, project, tool, subtype, top_score, top_legs),
            )
    except Exception:  # noqa: BLE001 — telemetry must never fail the search
        logger.warning("search-miss telemetry write failed", exc_info=True)
        return False
    return True


def observe(
    query: str,
    project: str | None,
    tool: str | None,
    top_id: int | None = None,
    legs: tuple[Rows, Rows, Rows] | None = None,
) -> bool:
    """kbcore.search's single telemetry hook: classify and record one search.

    ``top_id`` is the top hit's item id (None = zero hits → an 'empty' row,
    the proven Wave-E path). ``legs`` are the (vector, keyword, identifier)
    candidate rows for leg provenance. With hits present:

    * rerank sidecar 'active' + floor ON → 'thin' when its top-1 relevance is
      below the floor; otherwise a deterministic 1-in-CALIBRATION_SAMPLE_EVERY
      sample records a 'calibration' row with the score (the sampler ticks on
      EVERY sidecar-scored search here; a due tick that is also a thin miss
      records only the thin row, which carries the same score);
    * sidecar 'active' + floor OFF → a 'calibration' row carrying the would-be
      score (cheap: one small best-effort insert per sidecar-scored search);
    * sidecar absent ('off'/'fallback') + floor ON → 'thin' when the top
      candidate has no keyword/identifier-leg contribution (no sidecar score:
      ``top_score`` stays NULL — provenance-heuristic only).

    ``query`` arrives already scrubbed (search scrubs ONCE for both sinks);
    :func:`record` re-scrubs defensively. Returns whether a row was written.
    """
    if tool is None:
        return False
    if top_id is None:
        return record(query, project, tool)
    names = ("vector", "keyword", "identifier")
    hit = [
        n
        for n, rows in zip(names, legs or ((), (), ()), strict=False)
        if any(r[0] == top_id for r in rows)
    ]
    top_legs = "+".join(hit) or "none"
    floor = settings.kb_miss_relevance_floor
    if rerank.last_outcome() == "active":
        score = rerank.last_top_relevance()
        if floor > 0:
            due = _calibration_due()
            if score is not None and score < floor:
                return record(query, project, tool, "thin", score, top_legs)
            if due:
                return record(query, project, tool, "calibration", score, top_legs)
            return False
        return record(query, project, tool, "calibration", score, top_legs)
    if floor > 0 and "keyword" not in hit and "identifier" not in hit:
        return record(query, project, tool, "thin", None, top_legs)
    return False


def count_since(days: float = 1.0, subtype: str = "empty") -> int:
    """Misses of one ``subtype`` in the last ``days`` (fractional ok).

    Default 'empty' keeps the digest's ``search_misses_24h`` semantics exactly
    as Wave E shipped them (every pre-019 row backfilled to 'empty'); the thin
    counter reads ``count_since(days=1, subtype='thin')``.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT count(*) FROM search_misses "
            "WHERE at >= now() - make_interval(secs => %s) AND subtype = %s",
            (float(days) * 86400.0, subtype),
        ).fetchone()
    return int(row[0])


def stats(days: float = 7.0) -> dict[str, int]:
    """Window aggregates for the weekly report.

    ``total``/``distinct`` keep their Wave-E meaning — real MISSES (empty +
    thin; calibration rows never count). The per-subtype keys (``empty``,
    ``thin``, ``calibration``) are the review #5 ADDITIVE breakdown — new keys
    only, so every existing consumer of the headline pair is untouched.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT count(*) FILTER (WHERE subtype = ANY(%(miss)s)),"
            "       count(DISTINCT query) FILTER (WHERE subtype = ANY(%(miss)s)),"
            "       count(*) FILTER (WHERE subtype = 'empty'),"
            "       count(*) FILTER (WHERE subtype = 'thin'),"
            "       count(*) FILTER (WHERE subtype = 'calibration') "
            "FROM search_misses WHERE at >= now() - make_interval(secs => %(secs)s)",
            {"miss": list(_MISS_SUBTYPES), "secs": float(days) * 86400.0},
        ).fetchone()
    keys = ("total", "distinct", "empty", "thin", "calibration")
    return {k: int(v) for k, v in zip(keys, row, strict=True)}


def top_misses(
    days: float = 7.0, limit: int = 10, subtype: str | None = None
) -> list[tuple[str, int]]:
    """The window's most-missed queries as ``[(query, count)]``, most-missed first.

    Ties break oldest-demand-first then lexically, so the table is stable run
    over run. Grouping is by EXACT query text — near-duplicate phrasings are a
    judgement for the reader, not for SQL. ``subtype`` None (the default, and
    the pre-019 semantics) tables real misses (empty + thin — calibration rows
    never enter); naming a subtype narrows to it.
    """
    subtypes = list(_MISS_SUBTYPES) if subtype is None else [subtype]
    with connect() as conn:
        rows = conn.execute(
            "SELECT query, count(*) AS n FROM search_misses "
            "WHERE at >= now() - make_interval(secs => %s) AND subtype = ANY(%s) "
            "GROUP BY query ORDER BY n DESC, min(at), query LIMIT %s",
            (float(days) * 86400.0, subtypes, int(limit)),
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

"""kbcore.queue_groups — the drain's READ side: choosing and grouping drainable rows.

Split from ``kbcore.ingest_queue`` (mechanical move, 2026-08-21, review #5): that
file sat at 568 lines against its 543+25 ratchet ceiling. THIRD application of
the documented seam (queue_checkpoint 2026-08-10, queue_repair with it): every
name here is re-imported by ``ingest_queue``, so callers, CLI and tests keep
reaching them as ``ingest_queue.<name>``. Import direction stays acyclic —
``ingest_queue`` is imported only LAZILY, inside functions, for ``MAX_ATTEMPTS``
and ``drain_day_start`` (the queue_repair.near_deferral_cap_groups pattern); the
day boundary resolves THROUGH ingest_queue at call time because tests patch
``ingest_queue.drain_day_start`` to compress "tomorrow night" into one test.
Same logger name as the parent on purpose: these lines have always appeared in
journald under ``kbcore.ingest_queue`` and grep recipes depend on that.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from kbcore.db import connect

logger = logging.getLogger("kbcore.ingest_queue")

# How long a session's chunks must be QUIET before the drain treats them as a
# complete unit. The hook sends a large delta as many sequential rows across
# several firings, so coalescing eagerly would extract a half-session and then
# extract the rest separately.
COALESCE_QUIET_MINUTES = 30

# The drainable set both readers below share: still pending, or failed but under
# the retry budget AND not content-failed TODAY — attempts are budgeted per DAY,
# not per run, since a per-RUN exclusion let the 08-15 boot catch-up double-burn
# a group failed at 01:00 (review #4; %(day0)s = queue_repair.drain_day_start(),
# local midnight). Owner requeues and the re-arm NULL processed_at, so a
# deliberate retry is eligible today; deferrals leave rows 'pending' with
# attempts untouched, so outages are exempt. Named once so the paths cannot
# drift apart.
_DRAINABLE = (
    "(status = 'pending' OR (status = 'failed' AND attempts < %(maxa)s"
    " AND (processed_at IS NULL OR processed_at < %(day0)s)))"
)

# The SQL twin of :func:`normalise_source_uri`, applied to rows ALREADY STORED.
# The enqueue guard protects new submissions; this protects the 137 rows that
# were written before it existed, so requeueing them for a supervised recovery
# cannot re-create the cross-project coalescing. Keep the two in step.
_DEGENERATE_URI_SQL = """
    btrim(coalesce(meta->>'source_uri', '')) = ''
 OR btrim(coalesce(meta->>'source_uri', '')) ~ '^[^:]*:[/[:space:]]*$'
"""


def normalise_source_uri(value: str | None) -> str | None:
    """Return ``value`` unless it is a DEGENERATE session identity, then ``None``.

    A ``source_uri`` is a GROUPING KEY: :func:`pending_groups` concatenates every
    row that shares one into a single extraction. That makes a valueless value
    actively dangerous — ``'session:'`` is not an identity, it is a wildcard that
    matches every session that ever sent one.

    It is not hypothetical. The flush hook computed the session id in the parent
    shell and forgot to export it, so the detached child interpolated an empty
    string and every machine sent ``session_uri='session:'``. 137 rows from SIX
    unrelated projects coalesced into cross-project mega-extractions stamped with
    the first row's project — a confidentiality near-miss on NDA material.

    Refusing the value HERE is the half of the fix that does not depend on a
    client being correct: a degenerate URI never reaches the grouping key at all,
    so each row falls back to its own ``queue:<kind>:<id>``. Degenerate means
    empty/whitespace, or a bare scheme with nothing after it (``session:``,
    ``session://``, ``file:/``).
    """
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    _scheme, sep, tail = v.partition(":")
    if sep and not tail.strip("/ \t"):
        return None
    return v


def group_key(project: str, uri: str) -> str:
    """The coalescing key: a session identity is only meaningful WITHIN a project.

    Downstream (``kbworker.consolidate``) this string is the cohort key, so it
    must be unique per group as well as stable — two groups sharing a key would
    hand one group's pre-extraction to the other.
    """
    return f"{project}::{uri}"


def pending(limit: int = 50) -> list[dict[str, Any]]:
    """TEST/INSPECTION helper: up to ``limit`` drainable rows, deferred-last.

    ZERO production callers — the drain reads through :func:`pending_groups`.
    Corrected 2026-08-21 (review #5): the ``FOR UPDATE SKIP LOCKED`` here is
    NOT a concurrency guarantee — the row locks die at ``connect()``'s context
    exit, so they never outlive the call; advisory lock 42
    (``kbworker.consolidate.ADVISORY_KEY``) is the real single-drainer
    guarantee. Drainable is :data:`_DRAINABLE` — still 'pending', or
    retryable-'failed' and not content-failed TODAY (the per-DAY attempt
    budget, review #4). Ordering: fresh (never-deferred) pending rows first,
    THEN already-deferred pending rows, then retryable failed rows, so a stuck
    row drifts to the back as it accumulates deferrals (review finding).
    """
    from kbcore.ingest_queue import MAX_ATTEMPTS, drain_day_start

    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, project, kind, content, meta
            FROM ingest_queue
            WHERE {_DRAINABLE}
            ORDER BY (status = 'failed'), deferrals, id
            LIMIT %(limit)s
            FOR UPDATE SKIP LOCKED
            """,  # noqa: S608 — _DRAINABLE is a constant fragment
            {"maxa": MAX_ATTEMPTS, "day0": drain_day_start(), "limit": limit},
        ).fetchall()
    return [
        {"id": r[0], "project": r[1], "kind": r[2], "content": r[3], "meta": r[4] or {}}
        for r in rows
    ]


def pending_groups(
    limit: int = 200,
    quiet_minutes: int = COALESCE_QUIET_MINUTES,
    max_groups: int | None = None,
    exclude: Iterable[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Drainable rows grouped by the SESSION they belong to, WITHIN A PROJECT.

    The network path windows RAW JSONL at 900 KB, while the local path assembles
    to text first — measured on a real transcript, 114 MB of JSONL is 2.7 MB of
    conversation, so the network path produces ~42x the rows for the same
    session (350 vs 9 for 300 MB). Each row is its own 14B extraction, against a
    200-row nightly budget, so one large session could starve every project for
    days.

    Grouping by (project, source_uri) and ordering by id restores the local
    path's behaviour: the caller concatenates, assembles ONCE, and re-windows the
    assembled text. Row order WITHIN a group is the hook's byte order, because it
    submits sequential offsets.

    OLDEST-FIRST ACROSS GROUPS (2026-08-01). Groups used to come back in
    whatever order the group query produced — effectively (project, uri), i.e.
    ALPHABETICAL. With a bounded nightly budget that is a starvation bug, not a
    cosmetic one: on a busy night the drain never reached the tail of the
    alphabet, so a submission from ``zeta-project`` could sit pending for as long
    as ``alpha-project`` kept submitting. Groups are now ordered by the age of
    their OLDEST row (``min(created_at)`` ascending), so waiting is what earns a
    group its turn and nothing can be starved by a lexically-luckier URI.

    THE CAPS ARE APPLIED IN SQL, and they are per CALL:

    * ``limit`` bounds the ROWS this call materialises — the memory bound. The
      grouping pass deliberately does NOT select ``content``; only the rows of
      the chosen groups are fetched, so a 5,000-row backlog is no longer read
      into the client to hand back 30 rows of it. A single group larger than
      ``limit`` is still returned ALONE (skipping it would wedge the queue).
    * ``max_groups`` bounds the GROUPS returned. The drain sizes it to one cohort
      and calls again (the refill loop in ``kbworker.consolidate``), which is why
      neither cap is a per-RUN budget any more.
    * ``exclude`` drops ``(project, group_uri)`` pairs the caller has already
      processed. A drain that DEFERS a group leaves its rows 'pending' (an outage
      must not burn the retry budget), and a content failure leaves them
      retryable-'failed' — both stay drainable, so without this the very next
      refill would re-fetch the group and retry it inside the same run, burning
      the attempt/deferral budget a night at a time instead of a run at a time.

    THE PROJECT IS PART OF THE KEY, and that is not a nicety. The consumer
    concatenates a group's rows into ONE extraction and stamps the result with
    ``rows[0]['project']``, so a key shared across projects does not merely mix
    them — it files everyone's content under whichever project happened to sort
    first. When the flush hook shipped an empty session id, 137 rows from six
    projects met under ``'session:'`` and became cross-project mega-extractions
    on NDA material. A session identity is only ever meaningful inside the
    project that produced it, so it can never be the whole key.

    A group is returned only when its newest row has been quiet for
    ``quiet_minutes`` — a session still being written would otherwise be
    extracted in halves. Rows with no (or a degenerate) source_uri fall back to
    their own ``queue:<kind>:<id>`` and come back as groups of one, which is what
    they already were.

    Each returned group carries the unique ``uri`` key (see :func:`group_key`),
    the ``project``, the raw ``source_uri`` it was derived from, and
    ``group_uri`` — the grouping value the key was built from, which is what
    ``exclude`` matches on (the composite key can, pathologically, collide).
    """
    from kbcore.ingest_queue import MAX_ATTEMPTS, drain_day_start

    # Materialised ONCE: the two arrays are positionally paired, so they must not
    # be built from two separate walks of a caller-supplied iterator/set.
    excluded = list(exclude or ())
    ex_projects = [p for p, _ in excluded]
    ex_uris = [u for _, u in excluded]
    with connect() as conn:
        rows = conn.execute(
            f"""
            WITH d AS (
                -- NO content here: this pass exists to CHOOSE groups, and
                -- selecting the payload would materialise the whole backlog to
                -- hand back one cohort of it (the unbounded-fetch finding).
                SELECT id, project, created_at,
                       CASE WHEN {_DEGENERATE_URI_SQL}
                            THEN NULL
                            ELSE btrim(meta->>'source_uri') END AS suri,
                       CASE WHEN {_DEGENERATE_URI_SQL}
                            THEN 'queue:' || kind || ':' || id::text
                            ELSE btrim(meta->>'source_uri') END AS uri
                  FROM ingest_queue
                 WHERE {_DRAINABLE}
            ),
            g AS (
                -- The wait exists ONLY to avoid splitting a session that is
                -- still being submitted. A group of ONE row cannot be split, so
                -- it drains immediately — which also keeps every single-shot
                -- submission (an ad-hoc ingest, a small session) as responsive
                -- as it was before coalescing existed.
                SELECT project, uri, min(created_at) AS oldest, count(*) AS n
                  FROM d GROUP BY project, uri
                 HAVING count(*) = 1
                     OR max(created_at) < now() - make_interval(mins => %(quiet)s)
            ),
            elig AS (
                -- Oldest-first, with a running row total so the ROW cap can be
                -- applied to a PREFIX of that order instead of to whatever the
                -- planner happened to emit first.
                SELECT project, uri, oldest, n,
                       sum(n) OVER (ORDER BY oldest, project, uri
                                    ROWS UNBOUNDED PRECEDING) AS cum
                  FROM g
                 WHERE NOT EXISTS (
                     SELECT 1
                       FROM unnest(%(ex_projects)s::text[], %(ex_uris)s::text[]) AS ex(p, u)
                      WHERE ex.p = g.project AND ex.u = g.uri
                 )
            ),
            chosen AS (
                -- cum = n is the FIRST group in the order: it is taken even when
                -- it alone exceeds the row cap, because skipping it would return
                -- nothing and one oversized session would wedge the queue.
                SELECT project, uri, oldest FROM elig
                 WHERE cum <= %(rows)s OR cum = n
                 ORDER BY oldest, project, uri
                 LIMIT %(groups)s::bigint  -- NULL = no group cap
            )
            SELECT q.id, q.project, q.kind, q.content, q.meta, d.uri, d.suri
              FROM chosen c
              JOIN d ON d.project = c.project AND d.uri = c.uri
              JOIN ingest_queue q ON q.id = d.id
             ORDER BY c.oldest, c.project, c.uri, q.id
            """,  # noqa: S608 — _DRAINABLE/_DEGENERATE_URI_SQL are constant fragments
            {
                "maxa": MAX_ATTEMPTS,
                "day0": drain_day_start(),
                "quiet": int(quiet_minutes),
                "rows": max(1, int(limit)),
                "groups": None if max_groups is None else max(1, int(max_groups)),
                "ex_projects": ex_projects,
                "ex_uris": ex_uris,
            },
        ).fetchall()

    # Keyed by the TUPLE, so no delimiter convention can ever merge two projects.
    # Insertion order is the SQL order, so the groups come back oldest-first.
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    source_uris: dict[tuple[str, str], str | None] = {}
    for rid, project, kind, content, meta, uri, suri in rows:
        groups.setdefault((project, uri), []).append(
            {"id": rid, "project": project, "kind": kind, "content": content, "meta": meta or {}}
        )
        source_uris[(project, uri)] = suri
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for (project, uri), members in groups.items():
        key = group_key(project, uri)
        # Pathological only (a project name containing the delimiter), but the
        # cohort key MUST be unique or one group's prefetch feeds another's.
        n = 2
        while key in seen:
            key = f"{group_key(project, uri)}#{n}"
            n += 1
        seen.add(key)
        out.append(
            {
                "uri": key,
                "project": project,
                "group_uri": uri,
                "source_uri": source_uris[(project, uri)],
                "rows": members,
            }
        )
    return out

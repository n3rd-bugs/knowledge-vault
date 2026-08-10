"""kbcore.ingest_queue — durable queue for remote submissions (Invariant 8).

Remote agents submit conversation transcripts or memory dumps over the public
MCP/REST endpoint. We SCRUB and store immediately (fast, no GPU); the grounded
14B extraction runs later in the nightly consolidation drain. The raw-with-secrets
text is never persisted — only the scrubbed content is stored. All SQL lives here.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from typing import Any, NamedTuple

from psycopg.types.json import Jsonb

from kbcore import scope
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation

# THE ESTABLISHED SEAM. Both modules below were split OUT of this one (the
# per-window checkpoint 2026-08-10, the owner requeue repairs with it) and are
# re-imported HERE because this module owns these rows: every caller, CLI and
# test reaches them as ``ingest_queue.<name>``, and the split must not be visible
# to any of them. Same pattern as kbworker.consolidate's re-exports.
from kbcore.queue_checkpoint import (  # noqa: F401
    CHECKPOINT_KEY,
    resume_windows,
    save_window_checkpoint,
    window_checkpoint,
    window_fingerprint,
)
from kbcore.queue_repair import requeue_extracted, requeue_failed  # noqa: F401
from kbcore.scrub import scrub

logger = logging.getLogger("kbcore.ingest_queue")

QUEUE_KINDS = ("session", "memory")
MAX_CONTENT_BYTES = 1_000_000  # ~1 MB cap on a single submission
MAX_PENDING = 5_000  # aggregate cap: refuse new submissions when the backlog is this deep
# A failed row is re-drained until it has been tried this many times, then left
# permanently 'failed'. Self-heals a transient DB/OOM failure without any
# operator requeue tool; a genuinely-bad row stops after a bounded few attempts.
MAX_ATTEMPTS = 3
# A row DEFERRED for a "transient" extractor/embedder outage keeps attempts
# untouched (an outage must not burn the content-failure budget). But a
# payload-DETERMINISTIC 5xx / mid-response crash is misclassified as transient and
# would re-drain every night forever. After this many consecutive deferrals — set
# WELL ABOVE MAX_ATTEMPTS so a genuine multi-night outage still recovers — the row
# is escalated to 'failed' so the attempts cap can retire it (review finding).
DEFERRAL_CAP = 10
# Times a TERMINALLY-failed row may be re-armed to 'pending' by an identical
# resubmission before it stays permanently failed. Bounds a recurring poison
# payload that would otherwise reset attempts=0 forever (review finding).
MAX_REQUEUES = 3
# Projects a public submission may never target (system/curated). Review findings:
# a public caller must not inject items into kb-system or burn the nightly run on it.
RESERVED_PROJECTS = frozenset({"kb-system"})


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


class EnqueueResult(NamedTuple):
    """Outcome of an enqueue: ``id`` is the drainable row (None ⇒ live duplicate);
    ``requeued`` is True when a TERMINALLY-FAILED row was reset to 'pending' so
    callers can distinguish "re-queued after failure" from "already queued";
    ``permanently_failed`` is True when the row is terminally failed AND has
    exhausted its re-arm budget (:data:`MAX_REQUEUES`) — the id is returned but the
    row was NOT re-opened, so callers report it as needing manual review."""

    id: int | None
    requeued: bool
    permanently_failed: bool = False


def enqueue(
    project: str,
    content: str,
    kind: str = "session",
    repo: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
    source_uri: str | None = None,
    allow_reserved: bool = False,
) -> EnqueueResult:
    """Scrub and enqueue a submission. Returns an :class:`EnqueueResult`.

    Rejects unknown kinds, empty content, and oversized content (domain errors).
    The content is SCRUBBED before storage and the dedupe hash is over the
    scrubbed text, so re-submitting identical content is a no-op. ``allow_reserved``
    is set only by trusted LOCAL callers (the CLI session flush) so a PreCompact
    hook may file into kb-system; the public REST/MCP path always leaves it false.

    Resubmitting content whose only existing row is TERMINALLY FAILED (status
    'failed', attempts ≥ MAX_ATTEMPTS) resets that row to 'pending'/attempts=0 and
    returns its id with ``requeued=True`` — a status flip (Invariant 5), so a bug
    fixed after the cap was hit no longer strands the content behind the dedupe
    UNIQUE. A row that is still pending/in-flight/extracted/under-cap stays a no-op
    (id=None) so a live submission is never disturbed. Re-arming is BOUNDED at
    :data:`MAX_REQUEUES`: past that a recurring poison payload is left permanently
    failed and its id is returned with ``permanently_failed=True`` (finding).
    """
    if kind not in QUEUE_KINDS:
        raise InvalidOperation(f"unknown ingest kind {kind!r}; one of {', '.join(QUEUE_KINDS)}")
    if not project or not project.strip():
        raise InvalidOperation("project is required")
    if not allow_reserved and project.strip() in RESERVED_PROJECTS:
        raise InvalidOperation(f"project {project!r} is reserved and cannot be targeted remotely")
    # Public-only instance: only public projects accept submissions.
    scope.assert_writable(project)
    if not content or not content.strip():
        raise InvalidOperation("content is empty")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise InvalidOperation(f"content exceeds the {MAX_CONTENT_BYTES}-byte limit")
    if queue_depth() >= MAX_PENDING:
        raise InvalidOperation("ingest queue is full; try again after the nightly drain")

    clean, redactions = scrub(content)
    content_hash = hashlib.sha256(clean.encode("utf-8")).hexdigest()
    meta: dict[str, Any] = {}
    # A DEGENERATE session id is dropped at the door rather than stored: it is a
    # grouping key, and an empty one groups everything (see normalise_source_uri).
    source_uri = normalise_source_uri(source_uri)
    # Scrub the freeform meta strings too — the durable queue must hold no
    # raw secret, and these values flow into item metadata (review finding).
    for key, value in (
        ("repo", repo),
        ("branch", branch),
        ("commit", commit),
        ("source_uri", source_uri),
    ):
        if value:
            meta[key] = scrub(value)[0]
    if redactions:
        meta["redactions"] = redactions

    with transaction() as conn:
        # ON CONFLICT DO UPDATE fires for ANY terminally-failed row; other statuses
        # fail the WHERE so RETURNING yields no row (id=None → live duplicate). For a
        # terminal row we RE-OPEN it (status→'pending', attempts→0) only while it is
        # still under the re-arm budget; once requeue_count hits MAX_REQUEUES we bump
        # nothing and leave it 'failed', so a recurring poison payload can't reset
        # attempts forever. xmax is 0 on a fresh INSERT and non-zero on an UPDATE, and
        # the returned status tells a re-open apart from a permanent-failure touch.
        row = conn.execute(
            """
            INSERT INTO ingest_queue (project, kind, content, content_hash, meta)
            VALUES (%(project)s, %(kind)s, %(content)s, %(hash)s, %(meta)s)
            ON CONFLICT (kind, project, content_hash) DO UPDATE
              SET requeue_count = CASE WHEN ingest_queue.requeue_count < %(cap)s
                                       THEN ingest_queue.requeue_count + 1
                                       ELSE ingest_queue.requeue_count END,
                  status = CASE WHEN ingest_queue.requeue_count < %(cap)s
                                THEN 'pending' ELSE 'failed' END,
                  attempts = CASE WHEN ingest_queue.requeue_count < %(cap)s
                                  THEN 0 ELSE ingest_queue.attempts END,
                  error = CASE WHEN ingest_queue.requeue_count < %(cap)s
                               THEN NULL ELSE ingest_queue.error END,
                  deferrals = CASE WHEN ingest_queue.requeue_count < %(cap)s
                                   THEN 0 ELSE ingest_queue.deferrals END,
                  meta = EXCLUDED.meta,
                  processed_at = CASE WHEN ingest_queue.requeue_count < %(cap)s
                                      THEN NULL ELSE ingest_queue.processed_at END
              WHERE ingest_queue.status = 'failed' AND ingest_queue.attempts >= %(maxa)s
            RETURNING id, (xmax <> 0), (status = 'pending')
            """,
            {
                "project": project,
                "kind": kind,
                "content": clean,
                "hash": content_hash,
                "meta": Jsonb(meta),
                "cap": MAX_REQUEUES,
                "maxa": MAX_ATTEMPTS,
            },
        ).fetchone()
    if row is None:
        return EnqueueResult(None, requeued=False)
    id_, was_update, reopened = int(row[0]), bool(row[1]), bool(row[2])
    if was_update and not reopened:
        # A terminal row over the re-arm budget: id returned, but NOT re-opened.
        return EnqueueResult(id_, requeued=False, permanently_failed=True)
    return EnqueueResult(id_, requeued=(was_update and reopened))


def pending(limit: int = 50) -> list[dict[str, Any]]:
    """Claim up to ``limit`` drainable rows (FOR UPDATE SKIP LOCKED).

    Drainable = still 'pending', OR 'failed' but under :data:`MAX_ATTEMPTS` (the
    self-heal set). Ordering: fresh (never-deferred) pending rows first, THEN
    already-deferred pending rows, then retryable failed rows. Sorting by
    ``deferrals`` before ``id`` means a stuck low-id row that keeps hitting a
    "transient" 5xx can no longer sit at the HEAD of every nightly batch and starve
    new content — it drifts to the back as it accumulates deferrals (review finding).
    SKIP LOCKED means a concurrent/crashed drainer can never wedge the queue.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, project, kind, content, meta
            FROM ingest_queue
            WHERE status = 'pending'
               OR (status = 'failed' AND attempts < %s)
            ORDER BY (status = 'failed'), deferrals, id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (MAX_ATTEMPTS, limit),
        ).fetchall()
    return [
        {"id": r[0], "project": r[1], "kind": r[2], "content": r[3], "meta": r[4] or {}}
        for r in rows
    ]


def mark_extracted(queue_id: int, saved: int, rejected: int) -> None:
    """Record a successful drain — terminal, whether it took a retry or not."""
    with transaction() as conn:
        conn.execute(
            """
            UPDATE ingest_queue
            SET status = 'extracted', saved_count = %s, rejected_count = %s, processed_at = now()
            WHERE id = %s
            """,
            (saved, rejected, queue_id),
        )


def mark_failed(queue_id: int, error: str) -> None:
    """Record a failed drain (kept 'failed', not deleted — inspectable + retried).

    Increments ``attempts``; once it reaches :data:`MAX_ATTEMPTS` the row is no
    longer selected by :func:`pending`, so a genuinely-bad row stops looping.
    """
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET status = 'failed', attempts = attempts + 1, "
            "error = %s, processed_at = now() WHERE id = %s",
            (error[:2000], queue_id),
        )


def mark_deferred(queue_id: int, error: str) -> bool:
    """Record a TRANSIENT defer (extractor/embedder unavailable): bump ``deferrals``,
    leaving the row 'pending' with ``attempts`` UNCHANGED so an outage never burns
    the content-failure budget. Once deferrals reach :data:`DEFERRAL_CAP` a
    "transient" that never heals is treated as content-deterministic in practice and
    ESCALATED to 'failed' (attempts pinned at the cap) so the attempts logic retires
    it — a payload-deterministic 5xx / mid-response crash can no longer re-drain
    every night forever (review finding). Returns True when the row was escalated.
    """
    with transaction() as conn:
        row = conn.execute(
            "UPDATE ingest_queue SET deferrals = deferrals + 1, error = %s "
            "WHERE id = %s RETURNING deferrals",
            (error[:2000], queue_id),
        ).fetchone()
        deferrals = int(row[0]) if row else 0
        if deferrals >= DEFERRAL_CAP:
            conn.execute(
                "UPDATE ingest_queue SET status = 'failed', attempts = %s, "
                "processed_at = now() WHERE id = %s",
                (MAX_ATTEMPTS, queue_id),
            )
            return True
    return False


def queue_depth() -> int:
    """Count of pending submissions. Resilient to migration 002 not yet applied."""
    with connect() as conn:
        exists = conn.execute("SELECT to_regclass('ingest_queue')").fetchone()[0]
        if exists is None:
            return 0
        row = conn.execute("SELECT count(*) FROM ingest_queue WHERE status = 'pending'").fetchone()
    return int(row[0])


# How long a session's chunks must be QUIET before the drain treats them as a
# complete unit. The hook sends a large delta as many sequential rows across
# several firings, so coalescing eagerly would extract a half-session and then
# extract the rest separately.
COALESCE_QUIET_MINUTES = 30

# The same drainable set pending() uses: still pending, or failed but under the
# retry budget. Named once so the two paths cannot drift apart.
_DRAINABLE = "(status = 'pending' OR (status = 'failed' AND attempts < %(maxa)s))"

# The SQL twin of :func:`normalise_source_uri`, applied to rows ALREADY STORED.
# The enqueue guard protects new submissions; this protects the 137 rows that
# were written before it existed, so requeueing them for a supervised recovery
# cannot re-create the cross-project coalescing. Keep the two in step.
_DEGENERATE_URI_SQL = """
    btrim(coalesce(meta->>'source_uri', '')) = ''
 OR btrim(coalesce(meta->>'source_uri', '')) ~ '^[^:]*:[/[:space:]]*$'
"""


def group_key(project: str, uri: str) -> str:
    """The coalescing key: a session identity is only meaningful WITHIN a project.

    Downstream (``kbworker.consolidate``) this string is the cohort key, so it
    must be unique per group as well as stable — two groups sharing a key would
    hand one group's pre-extraction to the other.
    """
    return f"{project}::{uri}"


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


def mark_group_extracted(ids: list[int], saved: int, rejected: int) -> None:
    """Flip a whole coalesced group to 'extracted' in ONE transaction, booking the
    group's totals on ONE row. All-or-nothing: every id flips together, so a group
    extracted once cannot leave rows pending for a partial re-extraction.

    THE TOTALS BELONG TO THE GROUP, NOT EACH ROW. Writing ``saved`` / ``rejected``
    to EVERY row multiplied ``sum(saved_count)`` by the coalesce factor (a 648-item
    session reported 26,972 across ~42 rows). They are booked on the FIRST id and 0
    on the rest, so the column sums to the real total; that zero on a productive
    group's other rows is told apart from a genuinely zero-yield row by
    :func:`requeue_extracted`, whose predicate is group-aware for exactly this.

    THE PER-WINDOW CHECKPOINT DIES WITH THE GROUP IT WAS RESUMING (2026-08-10),
    in this same transaction as the flip: a completed group leaves no residue, so
    the key can never be read back against a re-opened row (and a stale one could
    only ever be read against a matching fingerprint anyway). See
    :mod:`kbcore.queue_checkpoint`.
    """
    if not ids:
        return
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET status='extracted', "
            "saved_count = CASE WHEN id = %(first)s THEN %(saved)s ELSE 0 END, "
            "rejected_count = CASE WHEN id = %(first)s THEN %(rejected)s ELSE 0 END, "
            "meta = coalesce(meta, '{}'::jsonb) - %(cpkey)s::text, "
            "processed_at = now() WHERE id = ANY(%(ids)s)",
            {
                "first": ids[0],
                "saved": saved,
                "rejected": rejected,
                "ids": list(ids),
                "cpkey": CHECKPOINT_KEY,
            },
        )

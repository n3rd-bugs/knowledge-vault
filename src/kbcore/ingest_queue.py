"""kbcore.ingest_queue — durable queue for remote submissions (Invariant 8).

Remote agents submit conversation transcripts or memory dumps over the public
MCP/REST endpoint. We SCRUB and store immediately (fast, no GPU); the grounded
14B extraction runs later in the nightly consolidation drain. The raw-with-secrets
text is never persisted — only the scrubbed content is stored. All SQL lives here.
"""

from __future__ import annotations

import hashlib
from typing import Any, NamedTuple

from psycopg.types.json import Jsonb

from kbcore import scope
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation
from kbcore.scrub import scrub

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
_DRAINABLE = "(status = 'pending' OR (status = 'failed' AND attempts < %s))"


def pending_groups(limit: int = 200, quiet_minutes: int = COALESCE_QUIET_MINUTES):
    """Drainable rows grouped by the SESSION they belong to.

    The network path windows RAW JSONL at 900 KB, while the local path assembles
    to text first — measured on a real transcript, 114 MB of JSONL is 2.7 MB of
    conversation, so the network path produces ~42x the rows for the same
    session (350 vs 9 for 300 MB). Each row is its own 14B extraction, against a
    200-row nightly budget, so one large session could starve every project for
    days.

    Grouping by source_uri and ordering by id restores the local path's
    behaviour: the caller concatenates, assembles ONCE, and re-windows the
    assembled text. Row order is the hook's byte order, because it submits
    sequential offsets.

    A group is returned only when its newest row has been quiet for
    ``quiet_minutes`` — a session still being written would otherwise be
    extracted in halves. Rows with no source_uri (single ad-hoc submissions)
    come back as groups of one, which is what they already were.
    """
    with connect() as conn:
        rows = conn.execute(
            f"""
            WITH d AS (
                SELECT id, project, kind, content, meta, created_at,
                       coalesce(meta->>'source_uri', 'queue:' || id::text) AS grp
                  FROM ingest_queue
                 WHERE {_DRAINABLE}
            ),
            quiet AS (
                -- The wait exists ONLY to avoid splitting a session that is
                -- still being submitted. A group of ONE row cannot be split, so
                -- it drains immediately — which also keeps every single-shot
                -- submission (an ad-hoc ingest, a small session) as responsive
                -- as it was before coalescing existed.
                SELECT grp FROM d GROUP BY grp
                 HAVING count(*) = 1
                     OR max(created_at) < now() - make_interval(mins => %s)
            )
            SELECT d.id, d.project, d.kind, d.content, d.meta, d.grp
              FROM d JOIN quiet USING (grp)
             ORDER BY d.grp, d.id
            """,  # noqa: S608 — _DRAINABLE is a constant fragment
            (MAX_ATTEMPTS, int(quiet_minutes)),
        ).fetchall()

    groups: dict[str, list[dict[str, Any]]] = {}
    for rid, project, kind, content, meta, grp in rows:
        groups.setdefault(grp, []).append(
            {"id": rid, "project": project, "kind": kind, "content": content, "meta": meta or {}}
        )
    # Cap on ROWS, not groups, so the nightly budget still bounds the work.
    out, taken = [], 0
    for grp, members in groups.items():
        if taken + len(members) > limit and out:
            break
        out.append({"uri": grp, "rows": members})
        taken += len(members)
    return out


def mark_group_extracted(ids: list[int], saved: int, rejected: int) -> None:
    """Flip a whole coalesced group in ONE transaction.

    All-or-nothing matters: a group extracted once must not leave some rows
    pending for a second, partial extraction of the same session.
    """
    if not ids:
        return
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET status='extracted', saved_count=%s, "
            "rejected_count=%s, processed_at=now() WHERE id = ANY(%s)",
            (saved, rejected, list(ids)),
        )

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
# Projects a public submission may never target (system/curated). Review findings:
# a public caller must not inject items into kb-system or burn the nightly run on it.
RESERVED_PROJECTS = frozenset({"kb-system"})


class EnqueueResult(NamedTuple):
    """Outcome of an enqueue: ``id`` is the drainable row (None ⇒ live duplicate);
    ``requeued`` is True when a TERMINALLY-FAILED row was reset to 'pending' so
    callers can distinguish "re-queued after failure" from "already queued"."""

    id: int | None
    requeued: bool


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
    (id=None) so a live submission is never disturbed.
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
        # ON CONFLICT re-opens ONLY a terminally-failed row; other statuses fail the
        # DO UPDATE ... WHERE, so RETURNING yields no row (id=None). The system
        # column xmax is 0 on a fresh INSERT and non-zero on an UPDATE, which tells
        # a re-queue apart from a brand-new enqueue.
        row = conn.execute(
            """
            INSERT INTO ingest_queue (project, kind, content, content_hash, meta)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (kind, project, content_hash) DO UPDATE
              SET status = 'pending', attempts = 0, error = NULL,
                  meta = EXCLUDED.meta, processed_at = NULL
              WHERE ingest_queue.status = 'failed' AND ingest_queue.attempts >= %s
            RETURNING id, (xmax <> 0)
            """,
            (project, kind, clean, content_hash, Jsonb(meta), MAX_ATTEMPTS),
        ).fetchone()
    if row is None:
        return EnqueueResult(None, requeued=False)
    return EnqueueResult(int(row[0]), requeued=bool(row[1]))


def pending(limit: int = 50) -> list[dict[str, Any]]:
    """Claim up to ``limit`` drainable rows (FOR UPDATE SKIP LOCKED).

    Drainable = still 'pending', OR 'failed' but under :data:`MAX_ATTEMPTS` (the
    self-heal set). Fresh pending rows sort first so a backlog of retryable
    failures can never starve new content under the batch limit. SKIP LOCKED
    means a concurrent/crashed drainer can never wedge the queue.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, project, kind, content, meta
            FROM ingest_queue
            WHERE status = 'pending'
               OR (status = 'failed' AND attempts < %s)
            ORDER BY (status = 'failed'), id
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


def queue_depth() -> int:
    """Count of pending submissions. Resilient to migration 002 not yet applied."""
    with connect() as conn:
        exists = conn.execute("SELECT to_regclass('ingest_queue')").fetchone()[0]
        if exists is None:
            return 0
        row = conn.execute("SELECT count(*) FROM ingest_queue WHERE status = 'pending'").fetchone()
    return int(row[0])

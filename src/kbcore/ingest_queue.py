"""kbcore.ingest_queue — durable queue for remote submissions (Invariant 8).

Remote agents submit conversation transcripts or memory dumps over the public
MCP/REST endpoint. We SCRUB and store immediately (fast, no GPU); the grounded
14B extraction runs later in the nightly consolidation drain. The raw-with-secrets
text is never persisted — only the scrubbed content is stored. All SQL lives here.
"""

from __future__ import annotations

import hashlib
from typing import Any

from psycopg.types.json import Jsonb

from kbcore import scope
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation
from kbcore.scrub import scrub

QUEUE_KINDS = ("session", "memory")
MAX_CONTENT_BYTES = 1_000_000  # ~1 MB cap on a single submission
MAX_PENDING = 5_000  # aggregate cap: refuse new submissions when the backlog is this deep
# Projects a public submission may never target (system/curated). Review findings:
# a public caller must not inject items into kb-system or burn the nightly run on it.
RESERVED_PROJECTS = frozenset({"kb-system"})


def enqueue(
    project: str,
    content: str,
    kind: str = "session",
    repo: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
    source_uri: str | None = None,
) -> int | None:
    """Scrub and enqueue a submission. Returns the row id, or None if a duplicate.

    Rejects unknown kinds, empty content, and oversized content (domain errors).
    The content is SCRUBBED before storage and the dedupe hash is over the
    scrubbed text, so re-submitting identical content is a no-op.
    """
    if kind not in QUEUE_KINDS:
        raise InvalidOperation(f"unknown ingest kind {kind!r}; one of {', '.join(QUEUE_KINDS)}")
    if not project or not project.strip():
        raise InvalidOperation("project is required")
    if project.strip() in RESERVED_PROJECTS:
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
        row = conn.execute(
            """
            INSERT INTO ingest_queue (project, kind, content, content_hash, meta)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (kind, project, content_hash) DO NOTHING
            RETURNING id
            """,
            (project, kind, clean, content_hash, Jsonb(meta)),
        ).fetchone()
    return int(row[0]) if row else None


def pending(limit: int = 50) -> list[dict[str, Any]]:
    """Claim up to ``limit`` pending rows for the drain (FOR UPDATE SKIP LOCKED).

    SKIP LOCKED means a concurrent/crashed drainer can never wedge the queue —
    rows it holds are simply skipped; unprocessed rows stay 'pending' for retry.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, project, kind, content, meta
            FROM ingest_queue
            WHERE status = 'pending'
            ORDER BY id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (limit,),
        ).fetchall()
    return [
        {"id": r[0], "project": r[1], "kind": r[2], "content": r[3], "meta": r[4] or {}}
        for r in rows
    ]


def mark_extracted(queue_id: int, saved: int, rejected: int) -> None:
    """Record a successful drain of a row."""
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
    """Record a failed drain (kept 'failed', not deleted — inspectable)."""
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET status = 'failed', error = %s, processed_at = now() "
            "WHERE id = %s",
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

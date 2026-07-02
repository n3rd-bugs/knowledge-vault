"""kbcore.review — the review queue for unverified/automatic knowledge (§7.3/§7.4).

Automatic extractions land in ``review_queue`` so a human can promote or reject
them in batches (Verdict V4). Phase 3 only enqueues; the interactive ``kb
review`` workflow arrives in Phase 4. All SQL lives here (Invariant 2).
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation, NotFound
from kbcore.models import REVIEW_REASONS, ReviewEntry
from kbcore.supersede import supersede


def queue_review_in(
    conn: psycopg.Connection,
    item_id: int | None,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> int:
    """Enqueue a review-queue entry inside the caller's transaction.

    Composition point so an extraction can insert the item AND its review-queue
    row in one transaction — a crash between them must not leave an unverified
    item that is never surfaced for review (review finding).
    """
    if reason not in REVIEW_REASONS:
        raise InvalidOperation(
            f"unknown review reason {reason!r}; one of {', '.join(REVIEW_REASONS)}"
        )
    row = conn.execute(
        """
        INSERT INTO review_queue (item_id, reason, detail)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (item_id, reason, Jsonb(detail or {})),
    ).fetchone()
    return int(row[0])


def queue_review(
    item_id: int | None,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> int:
    """Enqueue a review-queue entry in its own transaction. Returns the row id."""
    with transaction() as conn:
        return queue_review_in(conn, item_id, reason, detail)


def list_open_review() -> list[ReviewEntry]:
    """Unresolved review-queue rows joined to their item (kb review list, §7.4)."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT rq.id, rq.reason, rq.item_id, i.title, i.status, s.trust, rq.detail
            FROM review_queue rq
            LEFT JOIN items i ON i.id = rq.item_id
            LEFT JOIN sources s ON s.id = i.source_id
            WHERE rq.resolved = false
            ORDER BY rq.id
            """
        ).fetchall()
    return [
        ReviewEntry(
            queue_id=r[0],
            reason=r[1],
            item_id=r[2],
            title=r[3],
            status=r[4],
            trust=r[5],
            detail=r[6] if isinstance(r[6], dict) else {},
        )
        for r in rows
    ]


def _queued_item_id(conn: psycopg.Connection, queue_id: int) -> int:
    row = conn.execute("SELECT item_id FROM review_queue WHERE id = %s", (queue_id,)).fetchone()
    if row is None:
        raise NotFound(f"review queue row {queue_id} does not exist")
    if row[0] is None:
        raise InvalidOperation(f"review queue row {queue_id} has no associated item")
    return row[0]


def promote(queue_id: int) -> int:
    """Promote the queued item to trust='verified' (on its source) + resolve the row."""
    with transaction() as conn:
        item_id = _queued_item_id(conn, queue_id)
        conn.execute(
            "UPDATE sources SET trust = 'verified' "
            "WHERE id = (SELECT source_id FROM items WHERE id = %s)",
            (item_id,),
        )
        conn.execute("UPDATE review_queue SET resolved = true WHERE id = %s", (queue_id,))
    return item_id


def reject(queue_id: int) -> int:
    """Archive the queued item (Invariant 5: status flip, never delete) + resolve the row."""
    with transaction() as conn:
        item_id = _queued_item_id(conn, queue_id)
        conn.execute(
            "UPDATE items SET status = 'archived', updated_at = now() WHERE id = %s",
            (item_id,),
        )
        conn.execute("UPDATE review_queue SET resolved = true WHERE id = %s", (queue_id,))
    return item_id


def resolve_via_supersede(queue_id: int, new_item_id: int) -> int:
    """Supersede the flagged item with an existing item, then resolve the row.

    For contradictions: the reviewer keeps ``new_item_id`` and retires the older,
    conflicting one via kbcore.supersede (immutable-safe). Returns the flagged id.
    """
    with connect() as conn:
        item_id = _queued_item_id(conn, queue_id)
    supersede(item_id, new_item_id=new_item_id, reason="review: superseded via contradiction")
    with transaction() as conn:
        conn.execute("UPDATE review_queue SET resolved = true WHERE id = %s", (queue_id,))
    return item_id

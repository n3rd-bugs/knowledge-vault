"""kbcore.review — the review queue for unverified/automatic knowledge (§7.3/§7.4).

Automatic extractions land in ``review_queue`` so a human can promote or reject
them in batches (Verdict V4). Phase 3 only enqueues; the interactive ``kb
review`` workflow arrives in Phase 4. All SQL lives here (Invariant 2).
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from kbcore import scope
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


def open_review_by_reason(reason: str) -> tuple[int, dict[str, Any]] | None:
    """The oldest open review row ``(id, detail)`` for a reason, or None.

    Dedup + self-heal helper for the search-quality sentinel: it enqueues at most
    one open ``search_regression`` row and auto-resolves it once quality recovers.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT id, detail FROM review_queue "
            "WHERE resolved = false AND reason = %s ORDER BY id LIMIT 1",
            (reason,),
        ).fetchone()
    if row is None:
        return None
    return int(row[0]), (row[1] if isinstance(row[1], dict) else {})


def resolve_review(queue_id: int) -> None:
    """Mark a review row resolved (status flip, Invariant 5 — no item mutation).

    A generic close for item-less rows (the search-quality sentinel's regression
    flag) that promote/reject/supersede — all of which require an item — cannot.
    """
    with transaction() as conn:
        conn.execute("UPDATE review_queue SET resolved = true WHERE id = %s", (queue_id,))


def list_open_review() -> list[ReviewEntry]:
    """Unresolved review-queue rows joined to their item (kb review list, §7.4)."""
    # Public-only instance: never list rows for items in non-public projects
    # (they leak private ids/titles/quotes). A NULL-item row → NULL IN (...) →
    # not TRUE → dropped, matching stats.review_open (review finding).
    gate = (
        "AND i.project IN (SELECT name FROM projects WHERE public IS TRUE)"
        if scope.public_only()
        else ""
    )
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT rq.id, rq.reason, rq.item_id, i.title, i.status, s.trust, rq.detail
            FROM review_queue rq
            LEFT JOIN items i ON i.id = rq.item_id
            LEFT JOIN sources s ON s.id = i.source_id
            WHERE rq.resolved = false {gate}
            ORDER BY rq.id
            """  # noqa: S608 — gate is a constant fragment, no user data
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
    row = conn.execute(
        """
        SELECT rq.item_id, i.project
        FROM review_queue rq LEFT JOIN items i ON i.id = rq.item_id
        WHERE rq.id = %s
        """,
        (queue_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"review queue row {queue_id} does not exist")
    if row[0] is None:
        raise InvalidOperation(f"review queue row {queue_id} has no associated item")
    # Public-only instance: cannot resolve a review for a non-public item.
    scope.assert_writable(row[1])
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


def close_contradiction_rows_for(a_id: int, b_id: int) -> int:
    """Close open contradiction rows for a pair that auto-resolution settled.

    Matches the {a_id, b_id} detail in either order. Closing (resolved=true) is
    a status flip, not a delete (Invariant 5); the underlying items already
    carry the supersession trail.
    """
    with transaction() as conn:
        rows = conn.execute(
            """
            UPDATE review_queue SET resolved = true
            WHERE resolved = false AND reason = 'contradiction'
              AND ((detail->>'a_id')::bigint, (detail->>'b_id')::bigint) IN ((%s, %s), (%s, %s))
            RETURNING id
            """,
            (a_id, b_id, b_id, a_id),
        ).fetchall()
    return len(rows)


def auto_resolve_contradiction(queue_id: int, keep: int, retire: int) -> int:
    """Auto-resolve a contradiction whose evidence held for the stability streak.

    Owner V4 refinement (2026-07): supersede the evidence-losing side with the
    winner and close the row. Invoked ONLY by the nightly worker after the same
    suggestion persisted for AUTO_RESOLVE_STREAK consecutive refreshes; fully
    audited (supersede reason + relation + report count + log line) and
    reversible (supersession is a status flip, never a delete).
    """
    supersede(
        retire,
        new_item_id=keep,
        reason="auto: evidence margin stable across nightly refreshes",
    )
    with transaction() as conn:
        conn.execute("UPDATE review_queue SET resolved = true WHERE id = %s", (queue_id,))
    return retire


def apply_suggestion(queue_id: int) -> int:
    """Execute an open row's suggested resolution NOW (human-invoked early apply).

    Returns the retired item id. Raises when the row is missing, already
    resolved, or carries no suggestion.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT detail, resolved FROM review_queue WHERE id = %s", (queue_id,)
        ).fetchone()
    if row is None:
        raise NotFound(f"review row {queue_id} does not exist")
    detail, resolved = row
    if resolved:
        raise InvalidOperation(f"review row {queue_id} is already resolved")
    sugg = (detail or {}).get("suggested_resolution")
    if not sugg:
        raise InvalidOperation(f"review row {queue_id} has no suggested resolution")
    supersede(
        sugg["retire"], new_item_id=sugg["keep"], reason="review: applied evidence suggestion"
    )
    with transaction() as conn:
        conn.execute("UPDATE review_queue SET resolved = true WHERE id = %s", (queue_id,))
    return sugg["retire"]

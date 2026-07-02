"""kbcore.review — the review queue for unverified/automatic knowledge (§7.3/§7.4).

Automatic extractions land in ``review_queue`` so a human can promote or reject
them in batches (Verdict V4). Phase 3 only enqueues; the interactive ``kb
review`` workflow arrives in Phase 4. All SQL lives here (Invariant 2).
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from kbcore.db import transaction
from kbcore.errors import InvalidOperation
from kbcore.models import REVIEW_REASONS


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

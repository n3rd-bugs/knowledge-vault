"""kbcore.review_auto — automated review-queue hygiene (owner decision 2026-07-18).

The queue accumulated hundreds of open ``unverified_extraction`` rows against a
one-at-a-time manual drain. The owner approved a bounded V4 refinement in the
spirit of ``kb_auto_resolve_evidence``: automation may CLOSE review rows, and
nothing else. Everything here is a status flip (Invariant 5) — ``resolved``
flips to true and an ``auto_resolution`` audit object is merged into the
existing ``detail`` JSONB (no new schema, no migration). No row is ever
deleted, no item body is edited, and no trust level changes automatically; the
only trust/status changes ride the existing human-invoked primitives
promote/reject, which :func:`bulk_resolve` merely batches for an owner.
"""

from __future__ import annotations

from datetime import UTC, datetime

from kbcore import review
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation

BULK_ACTIONS = ("promote", "reject", "dismiss")


def close_moot_rows() -> int:
    """Close open ``unverified_extraction`` rows whose item is no longer active.

    Whatever retired the item — supersede, the nightly dedupe (``duplicate``),
    ``archive_stale``, a manual reject — already disposed of the knowledge, so
    the review question is moot; keeping the row open only buries real work.
    One parameterized UPDATE in one transaction, idempotent (a second run finds
    nothing open). Each closed row's ``detail`` is stamped via jsonb merge with
    ``auto_resolution = {kind: 'item_retired', item_status, at}`` for audit.
    Returns the number of rows closed.
    """
    at = datetime.now(UTC).isoformat()
    with transaction() as conn:
        rows = conn.execute(
            """
            UPDATE review_queue rq
            SET resolved = true,
                detail = COALESCE(rq.detail, '{}'::jsonb) || jsonb_build_object(
                    'auto_resolution', jsonb_build_object(
                        'kind', 'item_retired',
                        'item_status', i.status,
                        'at', %s::text))
            FROM items i
            WHERE i.id = rq.item_id
              AND rq.resolved = false
              AND rq.reason = 'unverified_extraction'
              AND i.status <> 'active'
            RETURNING rq.id
            """,
            (at,),
        ).fetchall()
    return len(rows)


def expire_quiet_rows(older_than_days: int) -> int:
    """Close old ``unverified_extraction`` rows for items that were NEVER recalled.

    Quiet expiry (owner decision 2026-07-18): a row older than
    ``older_than_days`` whose item is still active but has ZERO access_log rows
    was never going to be reviewed — nobody ever pulled the item. Only the
    review ROW closes; the item stays active, unverified, and searchable ("it
    remains available"). Stamps ``auto_resolution = {kind: 'quiet_expiry',
    age_days, at}`` via jsonb merge. ``older_than_days <= 0`` is the kill
    switch: return 0 without touching the DB. Returns the rows closed.
    """
    if older_than_days <= 0:
        return 0
    at = datetime.now(UTC).isoformat()
    with transaction() as conn:
        rows = conn.execute(
            """
            UPDATE review_queue rq
            SET resolved = true,
                detail = COALESCE(rq.detail, '{}'::jsonb) || jsonb_build_object(
                    'auto_resolution', jsonb_build_object(
                        'kind', 'quiet_expiry',
                        'age_days', %(days)s::int,
                        'at', %(at)s::text))
            FROM items i
            WHERE i.id = rq.item_id
              AND rq.resolved = false
              AND rq.reason = 'unverified_extraction'
              AND i.status = 'active'
              AND rq.created_at < now() - make_interval(days => %(days)s)
              AND NOT EXISTS (
                  SELECT 1 FROM access_log al WHERE al.item_id = rq.item_id
              )
            RETURNING rq.id
            """,
            {"days": older_than_days, "at": at},
        ).fetchall()
    return len(rows)


def bulk_resolve(
    action: str,
    queue_ids: list[int] | None = None,
    project: str | None = None,
) -> list[tuple[int, str]]:
    """Batch-drain open review rows through the existing single-row primitives.

    Owner batch tool (2026-07-18): applies one ``action`` — ``promote``,
    ``reject``, or ``dismiss`` — to rows selected by explicit ``queue_ids``
    and/or by the flagged item's ``project``. At least one selector is required
    (both ``None`` raises :class:`InvalidOperation` — never blanket the whole
    queue). Each row goes through review.promote / review.reject /
    review.dismiss with PER-ROW isolation: one failure is recorded as
    ``'error: <msg>'`` and the batch continues. Explicitly requested ids that
    are missing or already resolved surface as per-row errors too; the project
    selector sweeps only that project's OPEN rows. Returns ``[(queue_id,
    outcome)]`` sorted by queue id.
    """
    if action not in BULK_ACTIONS:
        raise InvalidOperation(f"unknown bulk action {action!r}; one of {', '.join(BULK_ACTIONS)}")
    if not queue_ids and not project:
        raise InvalidOperation(
            "refusing to bulk-resolve without a selector; pass queue_ids and/or project"
        )
    outcomes: dict[int, str] = {}
    targets: set[int] = set()
    with connect() as conn:
        if queue_ids:
            found = dict(
                conn.execute(
                    "SELECT id, resolved FROM review_queue WHERE id = ANY(%s)",
                    (list(queue_ids),),
                ).fetchall()
            )
            for qid in dict.fromkeys(queue_ids):
                if qid not in found:
                    outcomes[qid] = f"error: review queue row {qid} does not exist"
                elif found[qid]:
                    outcomes[qid] = f"error: review queue row {qid} is already resolved"
                else:
                    targets.add(qid)
        if project:
            rows = conn.execute(
                """
                SELECT rq.id FROM review_queue rq
                JOIN items i ON i.id = rq.item_id
                WHERE rq.resolved = false AND i.project = %s
                """,
                (project,),
            ).fetchall()
            targets.update(r[0] for r in rows)
    for qid in sorted(targets):
        try:
            if action == "promote":
                outcomes[qid] = f"promoted item {review.promote(qid)}"
            elif action == "reject":
                outcomes[qid] = f"rejected item {review.reject(qid)} (archived)"
            else:
                review.dismiss(qid)
                outcomes[qid] = "dismissed"
        except Exception as e:  # noqa: BLE001 — per-row isolation: one bad row must not abort the batch
            outcomes[qid] = f"error: {e}"
    return sorted(outcomes.items())

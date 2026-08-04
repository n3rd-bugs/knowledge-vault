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
from typing import Any

from kbcore import llm, review
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation

BULK_ACTIONS = ("promote", "reject", "dismiss")

# Triage suggestions that ask for the ITEM to be retired. Derived from
# llm.TRIAGE_ACTIONS rather than hand-listed so a future reject_* action is
# exempt from quiet expiry the day it ships, not the day someone remembers.
REJECT_TRIAGE_ACTIONS = tuple(a for a in llm.TRIAGE_ACTIONS if a.startswith("reject"))

# The quiet-expiry predicate, shared by the sweep and the "when does the first
# row expire" report count so the two can never drift. Constant SQL text with
# named placeholders — no value is ever formatted in (Invariant 2). ``i`` is the
# joined items row; ``%(reject)s`` is the REJECT_TRIAGE_ACTIONS array.
_QUIET_ELIGIBLE = """
              rq.resolved = false
              AND rq.reason = 'unverified_extraction'
              AND i.status = 'active'
              -- HARD DEADLINE FIX: a row carrying a reject_* triage suggestion is
              -- a KNOWN-BAD item waiting on a human to apply it. Expiring it
              -- closes the evidence and leaves the item live, unverified and
              -- searchable forever — the exact opposite of what the suggestion
              -- asked for. Those rows therefore never expire; they stay open
              -- until an owner applies or dismisses them.
              -- A 'keep'-suggested row DOES still expire: applying a keep closes
              -- the row and leaves the item active + unverified, which is
              -- byte-for-byte the end state quiet expiry produces, so holding it
              -- open buys nothing. An untriaged row expires as before.
              AND NOT (COALESCE(rq.detail->'triage'->>'action', '') = ANY(%(reject)s))
              AND NOT EXISTS (
                  SELECT 1 FROM access_log al WHERE al.item_id = rq.item_id
              )
"""


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

    EXEMPT: rows whose ``detail.triage`` carries a reject_* suggestion — see
    :data:`_QUIET_ELIGIBLE` for why those must never quietly close, and why a
    'keep'-suggested row may.
    """
    if older_than_days <= 0:
        return 0
    at = datetime.now(UTC).isoformat()
    with transaction() as conn:
        rows = conn.execute(
            f"""
            UPDATE review_queue rq
            SET resolved = true,
                detail = COALESCE(rq.detail, '{{}}'::jsonb) || jsonb_build_object(
                    'auto_resolution', jsonb_build_object(
                        'kind', 'quiet_expiry',
                        'age_days', %(days)s::int,
                        'at', %(at)s::text))
            FROM items i
            WHERE i.id = rq.item_id
              AND {_QUIET_ELIGIBLE}
              AND rq.created_at < now() - make_interval(days => %(days)s)
            RETURNING rq.id
            """,  # noqa: S608 — _QUIET_ELIGIBLE is constant SQL text, values are bound
            {"days": older_than_days, "at": at, "reject": list(REJECT_TRIAGE_ACTIONS)},
        ).fetchall()
    return len(rows)


def curation_counts(expire_days: int) -> dict[str, Any]:
    """The four numbers that say whether the CURATION LOOP is actually moving.

    The nightly report counted what automation DID (rows triaged, evidence
    refreshed) but never what it left WAITING, so a backlog of known-bad items
    with unapplied verdicts, and a wave of rows about to age out, were both
    invisible until someone went looking in SQL. Read-only; returns

    * ``triage_awaiting_apply`` — open unverified_extraction rows that already
      carry a stored triage suggestion. Every one is a human keystroke away
      from being resolved (``kb review apply-triage``).
    * ``reject_suggestions_pending`` — the subset asking for the item to be
      RETIRED. These are known-bad items still live in retrieval.
    * ``first_expiry_date`` — the date the oldest quiet-expiry-eligible row
      reaches ``expire_days`` (ISO date, possibly in the past = "next run").
      None when expiry is off (``expire_days <= 0``) or nothing is eligible.
    * ``contradiction_suggestions_pending`` — open contradiction rows carrying a
      decisive ``suggested_resolution`` that has not been executed yet.

    EXAMINED-BEFORE-EXPIRY (Package O). The 08-19..09-01 expiry cliff had to be
    reconstructed by hand, so the pool quiet-expiry silently drains is now
    first-class (read-only; nothing here changes any expiry behavior):

    * ``untriaged_expiring_7d`` / ``untriaged_expiring_30d`` — quiet-expiry-eligible
      rows NOT yet triaged whose item reaches ``expire_days`` within 7 / 30 days:
      the ones at risk of aging out UNEXAMINED into a live, unverified, searchable
      item nobody looked at. Both 0 when expiry is off (``expire_days <= 0``).
    * ``eligible_entered_1d`` / ``eligible_left_1d`` / ``eligible_flow_net_1d`` —
      the net daily flow of that pool: rows minted in the last day, versus rows
      that LEFT it by being triaged or having their item recalled. ``net`` > 0
      means the backlog grows faster than it is examined.
    """
    reject = list(REJECT_TRIAGE_ACTIONS)
    with connect() as conn:
        triaged, rejecting = conn.execute(
            """
            SELECT count(*) FILTER (WHERE COALESCE(rq.detail, '{}'::jsonb) ? 'triage'),
                   count(*) FILTER (WHERE rq.detail->'triage'->>'action' = ANY(%(reject)s))
            FROM review_queue rq
            WHERE rq.resolved = false AND rq.reason = 'unverified_extraction'
            """,
            {"reject": reject},
        ).fetchone()
        first_expiry = None
        expiring_7d = expiring_30d = 0
        if expire_days > 0:
            # first_expiry counts EVERY eligible row (a keep-triaged row still
            # expires); the two horizon counts narrow to UNTRIAGED-and-eligible
            # rows — the ones at risk of aging out never having been examined.
            untriaged = "NOT (COALESCE(rq.detail, '{}'::jsonb) ? 'triage')"
            expiry = "rq.created_at + make_interval(days => %(days)s)"
            row = conn.execute(
                f"""
                SELECT
                    min(rq.created_at) + make_interval(days => %(days)s),
                    count(*) FILTER (
                        WHERE {untriaged} AND {expiry} <= now() + make_interval(days => 7)),
                    count(*) FILTER (
                        WHERE {untriaged} AND {expiry} <= now() + make_interval(days => 30))
                FROM review_queue rq JOIN items i ON i.id = rq.item_id
                WHERE {_QUIET_ELIGIBLE}
                """,  # noqa: S608 — every interpolated fragment is constant SQL text, values bound
                {"days": expire_days, "reject": reject},
            ).fetchone()
            first_expiry = row[0].date().isoformat() if row and row[0] else None
            expiring_7d, expiring_30d = int(row[1]), int(row[2])
        (contra,) = conn.execute(
            "SELECT count(*) FROM review_queue "
            "WHERE resolved = false AND reason = 'contradiction' "
            "  AND jsonb_typeof(detail->'suggested_resolution') = 'object'"
        ).fetchone()
        # Net daily flow of the untriaged-eligible pool. ENTERED = rows minted in
        # the last day; LEFT = rows that were triaged or whose item was recalled in
        # the last day (either removes a row from the at-risk-of-silent-expiry set).
        (entered,) = conn.execute(
            "SELECT count(*) FROM review_queue "
            "WHERE reason = 'unverified_extraction' "
            "  AND created_at >= now() - make_interval(days => 1)"
        ).fetchone()
        (left,) = conn.execute(
            """
            SELECT count(DISTINCT rq.id)
            FROM review_queue rq
            WHERE rq.reason = 'unverified_extraction'
              AND (
                  (rq.detail->'triage'->>'at')::timestamptz >= now() - make_interval(days => 1)
                  OR EXISTS (
                      SELECT 1 FROM access_log al
                      WHERE al.item_id = rq.item_id
                        AND al.ts >= now() - make_interval(days => 1)
                  )
              )
            """
        ).fetchone()
    return {
        "triage_awaiting_apply": int(triaged),
        "reject_suggestions_pending": int(rejecting),
        "first_expiry_date": first_expiry,
        "contradiction_suggestions_pending": int(contra),
        # Examined-before-expiry (Package O): the pool quiet-expiry drains.
        "untriaged_expiring_7d": expiring_7d,
        "untriaged_expiring_30d": expiring_30d,
        "eligible_entered_1d": int(entered),
        "eligible_left_1d": int(left),
        "eligible_flow_net_1d": int(entered) - int(left),
    }


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

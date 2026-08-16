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

QUIET EXPIRY LIVES IN :mod:`kbcore.review_expire` (split out 2026-08-08 when it
grew its cap/dry-run/undo rails). This module still owns the predicate's other
consumer — :func:`curation_counts` — and imports the shared SQL from there so
the sweep and the report can never drift apart.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kbcore import review, review_promote
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation
from kbcore.review_expire import EXPIRY_GATE, QUIET_ELIGIBLE, REJECT_TRIAGE_ACTIONS
from kbcore.triage_sql import (
    TRIAGE_KEY,
    TRIAGE_PRIOR_KEY,
    sql_has_triage,
    sql_not_triaged,
    sql_triage_field,
)

BULK_ACTIONS = ("promote", "reject", "dismiss")
#: A row carrying EITHER key has been examined by the judge. ``triage_prior`` holds
#: the verdict auto-apply's dead-target re-queue moved aside, and a re-queued row
#: is waiting for a SECOND look, not a first one. Key spellings come from their
#: single owner (kbcore.review_triage, KB 21332).
EXAMINED_DETAIL_KEYS = (TRIAGE_KEY, TRIAGE_PRIOR_KEY)


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
    * ``never_triaged_open`` — open unverified_extraction rows carrying NO triage
      verdict at all AND no ``triage_prior``. Since the 2026-08-08 untriaged gate
      these can never quiet-expire, so this is the TRUE backlog gauge: work that
      will sit in the queue until triage (or a human) reaches it, however old it
      gets. The ``triage_prior`` exclusion is what keeps the gauge honest: a row
      auto-apply INVALIDATED (a dead dup target re-queued for a fresh verdict)
      has its verdict moved to ``triage_prior``, and counting it as never-triaged
      would report an examined row as unexamined — inflating the one number the
      digest uses to say whether the backlog is being worked.
    * ``first_expiry_date`` — the date the oldest row that can ACTUALLY expire
      (eligible AND past :data:`kbcore.review_expire.EXPIRY_GATE`) reaches
      ``expire_days`` (ISO date, possibly in the past = "next run"). None when
      expiry is off (``expire_days <= 0``) or nothing can expire.
    * ``contradiction_suggestions_pending`` — open contradiction rows carrying a
      decisive ``suggested_resolution`` that has not been executed yet.

    EXAMINED-BEFORE-EXPIRY (Package O). The 08-19..09-01 expiry cliff had to be
    reconstructed by hand, so the pool quiet-expiry drains is first-class
    (read-only; nothing here changes any expiry behavior):

    * ``untriaged_expiring_7d`` / ``untriaged_expiring_30d`` — otherwise-eligible
      rows NOT yet triaged that reach ``expire_days`` within 7 / 30 days. Under
      the untriaged gate these no longer age out unexamined — they are HELD OPEN
      instead, so the pair now measures how fast unexamined rows pile up against
      the deadline rather than how many are about to vanish. Both 0 when expiry
      is off (``expire_days <= 0``).
    * ``eligible_entered_1d`` / ``eligible_left_1d`` / ``eligible_flow_net_1d`` —
      the net daily flow of that pool: rows minted in the last day, versus rows
      that LEFT it by being triaged or having their item recalled. ``net`` > 0
      means the backlog grows faster than it is examined.
    * ``promote_candidates`` — recall-proven unverified items waiting on an
      owner promote, at the default threshold (the PROMOTE PROGRAM, review #3
      P1: the owner sees the queue exists without asking). ADDED key; nothing
      renamed.
    """
    reject = list(REJECT_TRIAGE_ACTIONS)
    with connect() as conn:
        triaged, rejecting, never_triaged = conn.execute(
            f"""
            SELECT count(*) FILTER (WHERE {sql_has_triage()}),
                   count(*) FILTER (WHERE {sql_triage_field("action")} = ANY(%(reject)s)),
                   count(*) FILTER (WHERE NOT (COALESCE(rq.detail, '{{}}'::jsonb) ?| %(examined)s))
            FROM review_queue rq
            WHERE rq.resolved = false AND rq.reason = 'unverified_extraction'
            """,  # noqa: S608 — constant triage-SQL fragments; values ride parameters
            {"reject": reject, "examined": list(EXAMINED_DETAIL_KEYS)},
        ).fetchone()
        first_expiry = None
        expiring_7d = expiring_30d = 0
        if expire_days > 0:
            # first_expiry narrows to rows that can ACTUALLY close (EXPIRY_GATE:
            # triaged, not undo-vetoed); the two horizon counts narrow the other
            # way, to UNTRIAGED rows the gate now holds open. One scan, three
            # FILTERs, so the report and the sweep read the same predicate.
            untriaged = sql_not_triaged()
            expiry = "rq.created_at + make_interval(days => %(days)s)"
            row = conn.execute(
                f"""
                SELECT
                    min(rq.created_at) FILTER (WHERE {EXPIRY_GATE})
                        + make_interval(days => %(days)s),
                    count(*) FILTER (
                        WHERE {untriaged} AND {expiry} <= now() + make_interval(days => 7)),
                    count(*) FILTER (
                        WHERE {untriaged} AND {expiry} <= now() + make_interval(days => 30))
                FROM review_queue rq JOIN items i ON i.id = rq.item_id
                WHERE {QUIET_ELIGIBLE}
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
            f"""
            SELECT count(DISTINCT rq.id)
            FROM review_queue rq
            WHERE rq.reason = 'unverified_extraction'
              AND (
                  ({sql_triage_field("at")})::timestamptz >= now() - make_interval(days => 1)
                  OR EXISTS (
                      SELECT 1 FROM access_log al
                      WHERE al.item_id = rq.item_id
                        AND al.ts >= now() - make_interval(days => 1)
                  )
              )
            """  # noqa: S608 — constant triage-SQL fragment; no value interpolated
        ).fetchone()
    return {
        "triage_awaiting_apply": int(triaged),
        "reject_suggestions_pending": int(rejecting),
        # The gauge the untriaged gate makes meaningful: rows that will NEVER
        # close by themselves, so the number only falls when work gets done.
        "never_triaged_open": int(never_triaged),
        "first_expiry_date": first_expiry,
        "contradiction_suggestions_pending": int(contra),
        # Examined-before-expiry (Package O): the pool quiet-expiry drains.
        "untriaged_expiring_7d": expiring_7d,
        "untriaged_expiring_30d": expiring_30d,
        "eligible_entered_1d": int(entered),
        "eligible_left_1d": int(left),
        "eligible_flow_net_1d": int(entered) - int(left),
        # The PROMOTE PROGRAM's queue depth (kbcore.review_promote): same
        # predicate as `kb review promote-candidates`, so digest and table agree.
        "promote_candidates": review_promote.count_promote_candidates(),
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

"""kbcore.review_apply — AUTOMATED application of reject_* triage suggestions.

Owner decision 2026-08-02, REVISING the 2026-07-18 decision that "a human
applies suggestions in bulk". The human apply loop had been dead 15 days while
853 known-bad items — every one reject-suggested by the nightly LLM triage —
sat ACTIVE in retrieval, growing ~290/day, and *permanently exempt from
quiet-expiry* (``review_auto._QUIET_ELIGIBLE`` deliberately never expires a
reject-suggested row, because closing it would leave the item live forever).
The backlog therefore could not drain by any existing path. Automation now
applies those suggestions under machine rails (:mod:`kbcore.review_apply_rails`).

WHAT DOES NOT MOVE (settled, do not re-litigate):

* promotion / trust escalation stays owner-only in kbcore (Invariant 10);
  nothing here ever raises trust — it only retires;
* automation never deletes (Invariant 5) — every effect is a status flip
  (``items.status`` active -> archived/duplicate, ``review_queue.resolved``);
* ``keep`` suggestions are NEVER auto-applied; they expire naturally.

AUTHORITY (why this may run without owner scope at the call site): the nightly
worker invokes it, and a worker has no principal to present. The authority is
not "the caller is the owner" but the SHAPE of the work — a DEMOTION of a row
the machine itself suggested, gated behind machine-checkable rails (a
cooling-off the owner can veto inside, an access exclusion that hands anything
an agent actually read back to the human, target liveness, a re-checked state
guard, and a cap), fully stamped and reversed by :func:`undo_auto_applied`.
Escalation would need owner scope; demotion under these rails does not.

The status transitions are NOT reimplemented here: reject_noise goes through
:func:`kbcore.review.reject` and reject_duplicate through
:func:`kbcore.consolidation.mark_duplicate` + :func:`kbcore.review.resolve_review`
— the same primitives the manual ``kb review apply-triage`` path uses.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from kbcore import consolidation, review
from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation
from kbcore.review_apply_rails import APPLIED_STATUS, COUNT_KEYS, STAMP_KIND, Row, eligible_rows

logger = logging.getLogger("kbcore.review_apply")


def _claim(qid: int, stamp: dict[str, Any]) -> bool:
    """STATE GUARD + STAMP in ONE transaction: re-check the state, then stamp the row.

    The guarded UPDATE closes the batch's only race window — between selection
    and apply a human may have promoted, rejected, superseded or dismissed the
    row, and any of those makes the machine's verdict stale. Matching nothing
    (returns False) means "raced": the caller skips SILENTLY, as the design
    requires — a human's action is never reported as an automation skip.

    Stamping BEFORE the flip is deliberate. A crash between the two leaves a
    stamped-but-open row whose item is still active, which the NEXT run simply
    re-selects and re-applies (the stamp is overwritten). The reverse ordering
    would lose the undo record for an already-flipped item.
    """
    with transaction() as conn:
        row = conn.execute(
            """
            UPDATE review_queue rq
            SET detail = COALESCE(rq.detail, '{}'::jsonb) || %(stamp)s
            FROM items i LEFT JOIN sources s ON s.id = i.source_id
            WHERE rq.id = %(qid)s
              AND rq.resolved = false
              AND i.id = rq.item_id
              AND i.status = 'active'
              AND COALESCE(s.trust, 'verified') = 'unverified'
            RETURNING rq.id
            """,
            {"stamp": Jsonb({"auto_resolution": stamp}), "qid": qid},
        ).fetchone()
    return row is not None


def _canonical_still_active(item_id: int | None) -> bool:
    """The dup target must be alive AT FLIP TIME (see the call site)."""
    if item_id is None:
        return False
    with connect() as conn:
        r = conn.execute("SELECT status FROM items WHERE id = %s", (item_id,)).fetchone()
    return bool(r and r[0] == "active")


def _unclaim(qid: int) -> None:
    """Release a claimed row so a later run re-evaluates it against fresh state."""
    with transaction() as conn:
        conn.execute(
            "UPDATE review_queue SET detail = detail - 'auto_resolution' "
            "WHERE id = %s AND NOT resolved",
            (qid,),
        )


def _apply_one(row: Row, cooling_days: int, batch: str) -> bool:
    """Claim + flip ONE row. True when the item was actually retired.

    Everything for this row happens in its OWN transaction(s) — never one giant
    batch transaction — so a crash mid-run loses at most this row's flip.
    """
    stamp: dict[str, Any] = {
        "kind": STAMP_KIND,
        "action": row.action,
        "at": datetime.now(UTC).isoformat(),
        "cooling_days": cooling_days,
        "batch": batch,
    }
    if row.action == "reject_duplicate":
        stamp["dup_of"] = row.dup_of
    if not _claim(row.qid, stamp):
        return False
    try:
        if row.action == "reject_noise":
            review.reject(row.qid)  # archives the item + resolves the row
            return True
        # RE-CHECK THE CANONICAL AT APPLY TIME, not just at selection. Within one
        # batch a reject_noise row can archive the canonical BEFORE this
        # reject_duplicate row flips (verifier reproduced it: B archived first,
        # then A retired as a duplicate whose derived_from pointed at the dead B).
        # mark_duplicate re-checks only the DUP's status, so the canonical's
        # liveness is this call's responsibility. A dead canonical means the
        # cluster changed under us — leave the row for the next run to re-select
        # against fresh cluster state rather than guessing a new canonical here.
        if not _canonical_still_active(row.dup_of):
            logger.info(
                "auto-apply: canonical %s no longer active; row %s deferred",
                row.dup_of,
                row.qid,
            )
            _unclaim(row.qid)
            return False
        if consolidation.mark_duplicate(row.item_id, row.dup_of):
            review.resolve_review(row.qid)
            return True
        # Item retired between claim and flip: leave the row open — the nightly
        # moot sweep (review_auto.close_moot_rows) closes it as 'item_retired'.
        logger.warning("auto-apply: item %s not active; row %s left open", row.item_id, row.qid)
    except Exception:  # noqa: BLE001 — per-row isolation; one bad row must not abort the run
        logger.exception("auto-apply of review row %s failed", row.qid)
    return False


def auto_apply_rejects(cap: int | None = None, *, dry_run: bool = False) -> dict[str, int]:
    """Apply stored ``reject_noise`` / ``reject_duplicate`` triage suggestions.

    Returns the run's counts: ``applied_noise``, ``applied_duplicate``,
    ``skipped_accessed``, ``skipped_cooling``, ``skipped_target_gone``,
    ``eligible_remaining``. The rails, each a documented+tested predicate:

    1. COOLING-OFF — the ``detail.triage.at`` stamp must be at least
       ``kb_auto_apply_cooling_days`` (default 3) old, so the owner sees the
       queued application in >= 3 nightly digests and can veto it by resolving
       the row. A missing/unparseable stamp fails CLOSED (skipped_cooling).
    2. ACCESS EXCLUSION — any access_log hit in the last 30 days hands the row
       back to the human (skipped_accessed).
    3. TARGET LIVENESS (reject_duplicate only) — the dup target must exist, be
       active, not be a summary and not be the item itself; surviving edges are
       cluster-canonicalized (``review_apply_rails.dup_canonicals``).
    4. STATE GUARD — re-checked atomically at apply time by :func:`_claim`.
    5. STAMP — ``detail.auto_resolution`` records kind/action/at/cooling_days/
       batch (+ the canonical dup_of) so :func:`undo_auto_applied` can reverse it.

    Rails 1-3 partition the candidates (first rail that stops a row wins); a row
    raced by a human between selection and apply is skipped SILENTLY, counted
    nowhere, because it was a human's decision rather than an automation skip.

    ``cap`` (None -> ``kb_auto_apply_cap``, default 300) bounds the applications
    per run; negative is unbounded, 0 applies nothing. ``eligible_remaining`` is
    what the cap left for the next run. ``dry_run`` reports the same counts
    (``applied_*`` = what WOULD be applied) and writes NOTHING.
    ``kb_auto_apply_enabled=false`` is the kill switch: all zeros, no DB access.
    """
    counts = dict.fromkeys(COUNT_KEYS, 0)
    if not settings.kb_auto_apply_enabled:
        return counts
    cooling = max(0, int(settings.kb_auto_apply_cooling_days))
    limit = settings.kb_auto_apply_cap if cap is None else int(cap)
    eligible = eligible_rows(cooling, counts)
    attempted = eligible if limit < 0 else eligible[: max(0, limit)]
    counts["eligible_remaining"] = len(eligible) - len(attempted)
    batch = datetime.now(UTC).isoformat()
    for r in attempted:
        key = "applied_noise" if r.action == "reject_noise" else "applied_duplicate"
        if dry_run or _apply_one(r, cooling, batch):
            counts[key] += 1
    if not dry_run and (counts["applied_noise"] or counts["applied_duplicate"]):
        logger.info("auto-apply batch %s: %s", batch, counts)
    return counts


def undo_auto_applied(since: datetime, *, dry_run: bool = False) -> list[int]:
    """Reverse auto-applied rejects stamped at/after ``since``. Owner CLI only.

    NEVER scheduled: this is the escape hatch for an owner who disagrees with a
    batch. Per row (its own transaction) it flips the item back to ``active`` —
    but ONLY from the exact status this automation left it in, so a later human
    supersede/archive is never undone — reopens the review row, and annotates the
    stamp with ``undone_at``. That annotation is also the VETO:
    ``review_apply_rails.candidates`` permanently excludes an undone row, so
    tonight's run cannot silently re-apply what the owner just reversed.

    Only ``resolved`` rows are reversed (a stamped-but-open row is a crashed
    apply, not an applied one). The ``derived_from`` relation a duplicate marking
    added is left in place — automation never deletes rows (Invariant 5); it is
    an audit trail, and re-marking is ``ON CONFLICT DO NOTHING``.

    Returns the reversed queue ids (``dry_run`` returns what WOULD be reversed
    and writes nothing).
    """
    if since.tzinfo is None:
        raise InvalidOperation("undo_auto_applied requires a timezone-aware UTC datetime")
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT rq.id, rq.item_id, rq.detail->'auto_resolution'
            FROM review_queue rq
            WHERE rq.resolved = true
              AND rq.detail->'auto_resolution'->>'kind' = %(kind)s
              AND rq.detail->'auto_resolution'->>'undone_at' IS NULL
              AND (rq.detail->'auto_resolution'->>'at')::timestamptz >= %(since)s
            ORDER BY rq.id
            """,
            {"kind": STAMP_KIND, "since": since},
        ).fetchall()
    if dry_run:
        return [int(r[0]) for r in rows]
    at = datetime.now(UTC).isoformat()
    undone: list[int] = []
    for qid, item_id, res in rows:
        prior = res if isinstance(res, dict) else {}
        expected = APPLIED_STATUS.get(str(prior.get("action")))
        with transaction() as conn:
            if expected is not None:
                conn.execute(
                    "UPDATE items SET status = 'active', updated_at = now() "
                    "WHERE id = %s AND status = %s",
                    (item_id, expected),
                )
            conn.execute(
                "UPDATE review_queue SET resolved = false, "
                "detail = COALESCE(detail, '{}'::jsonb) || %s WHERE id = %s",
                (Jsonb({"auto_resolution": {**prior, "undone_at": at}}), qid),
            )
        undone.append(int(qid))
    return undone

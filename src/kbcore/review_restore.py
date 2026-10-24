"""kbcore.review_restore — the owner's undo for an evidence auto-resolution.

Before the recency guard (kbcore.review_recency) the nightly retired 46 newer
items in 60 days in favour of older ones, and nothing could reverse that short
of hand SQL: ``supersede()`` only moves forward. :func:`restore_auto_resolved`
is that reverse, OWNER-ONLY and dry-run by default (``kb review restore``):

* the retired item flips ``superseded -> active``; ``superseded_by`` and
  ``supersede_reason`` are cleared, and what they said is appended to
  ``meta.restored`` with the owner's note. ``items.body`` is never touched and
  nothing is deleted: the ``supersedes`` relation stays as the audit edge of a
  supersession that DID happen (``items.status`` is the truth; the relation is
  history). A ``valid_to`` the supersession itself closed is reopened, so the
  restored fact is ``as_of``-visible again — but ONLY the stamped inferred
  transaction-time close (``meta.valid_time``): a close at the kept item's
  ``valid_from`` is indistinguishable from an asserted end, so it stands;
* the pair's contradiction row is reopened (or, if none survives, queued) with
  an ``owner_restore`` hold — an unconditional veto, so tonight's evidence pass
  cannot re-retire what the owner just restored — its suggestion moved aside to
  ``restored_suggestion`` (so ``kb review apply --all`` cannot re-apply it) and
  its streak reset. Any OTHER open row for the same pair (``record_contradiction``
  keys the relation by direction, so ``(a, b)`` and ``(b, a)`` can both exist;
  the twin sat hidden while one side was superseded) gets the same veto, or it
  would resurface with its own streak. The owner then settles the pair by hand;
* ``swap=True`` instead settles it the other way round: once the restore has
  committed, the previously KEPT item is superseded by the restored one through
  ``supersede()`` — the mandated correction path, which owns its transaction —
  and the pair's open rows close with an ``owner_restore_swap`` stamp. Should
  that second step fail, the state left is exactly the non-swap restore (item
  active, pair open and vetoed): never a lost item.

Only supersessions an EVIDENCE suggestion made are reversible here (the nightly
auto-resolve and the human early-apply of the same suggestion); any other
supersession was a human correction, and ``kb supersede`` is its path.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from kbcore import scope
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation, NotFound
from kbcore.review import queue_review_in
from kbcore.review_recency import AUTO_RESOLVE_REASON, HOLD_KEY, HOLD_RESTORED, RESOLVED_KIND
from kbcore.supersede import supersede

logger = logging.getLogger("kbcore.review_restore")

APPLIED_REASON = "review: applied evidence suggestion"  # kbcore.review.apply_suggestion
EVIDENCE_REASONS = (AUTO_RESOLVE_REASON, APPLIED_REASON)
SWAP_REASON = "owner: restore swap - the restored item replaces the one evidence kept"
SWAP_KIND = "owner_restore_swap"
DEFAULT_NOTE = "owner restore: wrongly retired by evidence auto-resolution"


class RestorePlan(BaseModel):
    """What a restore does (dry run) or did (``applied``) — the CLI's I/O shape."""

    model_config = ConfigDict(frozen=True)

    retired_id: int
    kept_id: int
    kept_status: str  # 'missing' when the kept row no longer exists
    supersede_reason: str
    queue_id: int | None  # the pair row reopened; None = a fresh row will be queued
    reopen_window: bool  # the supersession's valid_to close is undone
    twin_rows: int = 0  # other OPEN rows for the same pair, vetoed alongside
    swap: bool
    applied: bool = False


def _pair_row(conn: psycopg.Connection, retired: int, kept: int) -> tuple[int, dict] | None:
    """The contradiction row that settled this pair, locked: the one whose
    auto-resolution stamp retired ``retired`` first, then one whose suggestion
    did (rows resolved before the stamp existed), then the newest."""
    row = conn.execute(
        """
        SELECT id, detail FROM review_queue
        WHERE reason = 'contradiction'
          AND ((detail->>'a_id')::bigint, (detail->>'b_id')::bigint) IN ((%s, %s), (%s, %s))
        ORDER BY (detail->'auto_resolution'->>'kind' = %s
                  AND (detail->'auto_resolution'->>'retire')::bigint = %s) IS TRUE DESC,
                 ((detail->'suggested_resolution'->>'retire')::bigint = %s) IS TRUE DESC,
                 id DESC
        LIMIT 1
        FOR UPDATE
        """,
        (retired, kept, kept, retired, RESOLVED_KIND, retired, retired),
    ).fetchone()
    return None if row is None else (int(row[0]), row[1] if isinstance(row[1], dict) else {})


_PAIR_SQL = (
    "((detail->>'a_id')::bigint, (detail->>'b_id')::bigint) IN ((%(r)s, %(k)s), (%(k)s, %(r)s))"
)


def _plan(
    conn: psycopg.Connection, retired_id: int, swap: bool
) -> tuple[RestorePlan, dict[str, Any]]:
    """Validate + plan inside ``conn`` (rows locked). Returns (plan, pair row detail)."""
    row = conn.execute(
        "SELECT status, superseded_by, supersede_reason, meta, valid_to "
        "FROM items WHERE id = %s FOR UPDATE",
        (retired_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"item {retired_id} does not exist")
    status, kept, reason, meta, valid_to = row
    if status != "superseded" or kept is None:
        raise InvalidOperation(f"item {retired_id} is {status}, not superseded: nothing to restore")
    if reason not in EVIDENCE_REASONS:
        raise InvalidOperation(
            f"item {retired_id} was not retired by an evidence resolution "
            f"(reason {reason!r}); a human supersession is corrected with kb supersede"
        )
    krow = conn.execute("SELECT status FROM items WHERE id = %s", (kept,)).fetchone()
    kept_status = str(krow[0]) if krow else "missing"
    if swap and kept_status != "active":
        raise InvalidOperation(
            f"kept item {kept} is {kept_status}; --swap needs it active (restore without --swap)"
        )
    inferred = isinstance(meta, dict) and meta.get("valid_time") == "inferred_transaction_time"
    pair = _pair_row(conn, retired_id, int(kept))
    twins = conn.execute(
        f"SELECT count(*) FROM review_queue WHERE reason = 'contradiction' "  # noqa: S608
        f"AND resolved = false AND id <> %(q)s AND {_PAIR_SQL}",
        {"q": pair[0] if pair else -1, "r": retired_id, "k": int(kept)},
    ).fetchone()[0]
    plan = RestorePlan(
        retired_id=retired_id,
        kept_id=int(kept),
        kept_status=kept_status,
        supersede_reason=str(reason),
        queue_id=pair[0] if pair else None,
        reopen_window=valid_to is not None and inferred,
        twin_rows=int(twins),
        swap=swap,
    )
    return plan, (pair[1] if pair else {})


def restore_auto_resolved(
    retired_id: int, *, swap: bool = False, apply: bool = False, note: str | None = None
) -> RestorePlan:
    """Reverse an evidence auto-resolution of ``retired_id`` (module docstring).

    OWNER-ONLY (``scope.require_owner`` first, target-independent). ``apply=False``
    (the default) validates and returns the plan, writing nothing. ``apply=True``
    restores the item and reopens + vetoes the pair row in ONE transaction; with
    ``swap`` the kept item is then superseded by the restored one and the row
    closes. Raises NotFound / InvalidOperation for a missing, active, or
    human-superseded item, and for ``swap`` over a kept item that is not active.
    """
    scope.require_owner("restoring an auto-resolved item")
    if not apply:
        with connect() as conn:
            return _plan(conn, retired_id, swap)[0]
    at = datetime.now(UTC).isoformat()
    with transaction() as conn:
        plan, detail = _plan(conn, retired_id, swap)
        record = {
            "at": at,
            "note": note or DEFAULT_NOTE,
            "superseded_by": plan.kept_id,
            "supersede_reason": plan.supersede_reason,
            "swap": swap,
            "reopened_window": plan.reopen_window,
        }
        conn.execute(
            """
            UPDATE items
            SET status = 'active', superseded_by = NULL, supersede_reason = NULL,
                valid_to = CASE WHEN %(reopen)s THEN NULL ELSE valid_to END,
                meta = (CASE WHEN %(reopen)s THEN COALESCE(meta, '{}'::jsonb) - 'valid_time'
                             ELSE COALESCE(meta, '{}'::jsonb) END)
                       || jsonb_build_object('restored',
                            COALESCE(meta->'restored', '[]'::jsonb) || jsonb_build_array(%(rec)s)),
                updated_at = now()
            WHERE id = %(id)s AND status = 'superseded'
            """,
            {"reopen": plan.reopen_window, "rec": Jsonb(record), "id": retired_id},
        )
        veto: dict[str, Any] = {
            HOLD_KEY: {
                "kind": HOLD_RESTORED,
                "keep": plan.kept_id,
                "retire": retired_id,
                "reason": f"held: owner restored item {retired_id} - owner decision",
                "at": at,
            },
            "suggestion_streak": 0,
            "suggested_resolution": None,
        }
        patch = {**veto, "restored_suggestion": detail.get("suggested_resolution")}
        if isinstance(detail.get("auto_resolution"), dict):
            patch["auto_resolution"] = {**detail["auto_resolution"], "undone_at": at}
        if plan.queue_id is not None:
            conn.execute(
                "UPDATE review_queue SET resolved = false, "
                "detail = COALESCE(detail, '{}'::jsonb) || %s WHERE id = %s",
                (Jsonb(patch), plan.queue_id),
            )
        else:
            pair = {"a_id": plan.kept_id, "b_id": retired_id}
            qid = queue_review_in(conn, plan.kept_id, "contradiction", {**pair, **patch})
            plan = plan.model_copy(update={"queue_id": qid})
        pair_params = {"q": plan.queue_id, "r": retired_id, "k": plan.kept_id}
        conn.execute(  # the twins: each keeps its OWN suggestion aside
            "UPDATE review_queue SET detail = COALESCE(detail, '{}'::jsonb) "  # noqa: S608
            "|| jsonb_build_object('restored_suggestion', detail->'suggested_resolution') "
            f"|| %(veto)s WHERE reason = 'contradiction' AND resolved = false "
            f"AND id <> %(q)s AND {_PAIR_SQL}",
            {**pair_params, "veto": Jsonb(veto)},
        )
    logger.info("restored item %s (was superseded by %s)", retired_id, plan.kept_id)
    if swap:
        supersede(plan.kept_id, new_item_id=retired_id, reason=SWAP_REASON)
        stamp = {
            "auto_resolution": {
                "kind": SWAP_KIND,
                "keep": retired_id,
                "retire": plan.kept_id,
                "at": datetime.now(UTC).isoformat(),
            }
        }
        with transaction() as conn:  # every open row of the pair: it is settled now
            conn.execute(
                "UPDATE review_queue SET resolved = true, "  # noqa: S608
                "detail = COALESCE(detail, '{}'::jsonb) || %(stamp)s "
                f"WHERE reason = 'contradiction' AND resolved = false AND {_PAIR_SQL}",
                {**pair_params, "stamp": Jsonb(stamp)},
            )
    return plan.model_copy(update={"applied": True})

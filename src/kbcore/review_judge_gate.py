"""kbcore.review_judge_gate — the PRECISION GATE on nightly evidence auto-resolution.

THE DEFECT (measured 2026-10-25, KB 54572). The nightly evidence pass retires one
side of a queued contradiction once its evidence margin is stable
(:mod:`kbcore.review_recency`), but the pair entered the queue on ONE judge call.
A blind two-labeler ground truth over the 92 pairs the nightly auto-resolved
2026-08-25..10-24 found only 42 real conflicts (incompatible facts, or a stale
value against the current one); 37 were complementary facts (a bug and its fix, a
requirement and a gotcha, two documents that coexist) and 10 said the same thing.
On exactly those pairs the incumbent judge (qwen3:14b) was right on 52% of its
"contradict" answers; bonsai-2:27b on 78% (recall 0.83) at the same ~12 s/pair.

THE RULE. With ``settings.kb_auto_resolve_judge_gate`` on, the CONTRADICTION SEAT
(:func:`kbcore.llm.items_contradict` — ``KB_CONTRADICTION_JUDGE_MODEL``) re-judges
the pair right before anything is retired, asked the way the nightly step asks
(lower id as Item A, per-content seed). "No conflict" HOLDS the row
(``detail.auto_resolve_hold`` kind ``judge_no_conflict``): it stays open with its
suggestion visible and nothing is superseded — the owner decides. A judge that
cannot be reached RAISES, so the pair is simply retried next run: automation never
retires on an unconfirmed conflict. An unparseable answer reads as "no conflict"
(items_contradict's contract), i.e. it holds — the fail-safe direction.

SAME SEAT, NO CALL. The nightly step stamps ``detail.judged_by`` (model + think)
on every row it queues. Re-asking that same seat repeats a byte-identical,
same-seed request (prompt, Item order and :func:`kbcore.llm._draw_seed` are all
fixed; only run-to-run GPU noise could change the answer), so such a row counts as
confirmed without a call: the gate is the backlog filter for rows queued by a
DIFFERENT judge (every row before 2026-10-25 was queued by qwen3:14b).

NO VERDICT. A judge that times out (or answers 4xx) on THIS pair holds it too, kind
``judge_failed``: the request is seed-pinned, so it would stall the same way every
night. Only a real outage (connect error, model missing, 5xx) defers the pass.

STICKY. A gate hold stays in force until the owner acts
(:func:`kbcore.review_recency.hold_in_force`): a held row keeps a streak already
past the threshold, so releasing it on a gate-off or seat rollback would retire
it automatically on the very next pass — the defect this gate exists to stop.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from psycopg.types.json import Jsonb

from kbcore import llm
from kbcore.config import settings
from kbcore.consolidation import item_pair_texts
from kbcore.db import connect, transaction
from kbcore.errors import AlreadySuperseded, ExtractorUnavailable
from kbcore.review_recency import (
    GATE_DEFERRED,
    HOLD_FAILED,
    HOLD_JUDGE,
    HOLD_KEY,
    HOLD_RESTORED,
)

logger = logging.getLogger("kbcore.review_judge_gate")

#: :func:`gate`'s "go ahead and retire" answer.
CONFIRMED = None


def seat() -> dict[str, Any]:
    """The contradiction seat's identity, as stamped in ``detail.judged_by``."""
    return {
        "model": settings.contradiction_judge_model,
        "think": settings.contradiction_judge_think,
    }


def stamp_judged_by(a_id: int, b_id: int) -> None:
    """Record the current seat on the OPEN contradiction row for ``a -> b`` (just queued
    by the nightly step), so the gate never re-asks the seat that queued it."""
    with transaction() as conn:
        conn.execute(
            "UPDATE review_queue SET detail = detail || %s WHERE reason = 'contradiction' "
            "AND resolved = false AND (detail->>'a_id')::bigint = %s "
            "AND (detail->>'b_id')::bigint = %s",
            (Jsonb({"judged_by": seat()}), a_id, b_id),
        )


def assert_retire_active(retire: int) -> None:
    """The moot check, BEFORE any judge call: a loser an earlier resolution of this
    pass already retired raises what supersede() would, so the row closes as moot."""
    with connect() as conn:
        row = conn.execute("SELECT status FROM items WHERE id = %s", (retire,)).fetchone()
    if row is not None and row[0] == "superseded":
        raise AlreadySuperseded(f"item {retire} is already superseded")


def judge_confirms(keep: int, retire: int) -> bool:
    """The contradiction seat's verdict on the pair. Raises
    :class:`~kbcore.errors.ExtractorUnavailable` when the judge is unreachable."""
    a_id, b_id = (keep, retire) if keep <= retire else (retire, keep)
    (a_title, a_body), (b_title, b_body) = item_pair_texts(a_id, b_id)
    return llm.items_contradict(a_title, a_body, b_title, b_body)


def per_pair_failure(exc: ExtractorUnavailable) -> bool:
    """A judge failure about THIS pair (read timeout, 4xx other than model-missing)
    rather than an outage — kbcore.llm._chat wraps every httpx error the same way."""
    cause = exc.__cause__
    if isinstance(cause, httpx.TimeoutException):
        return not isinstance(cause, httpx.ConnectTimeout)
    if isinstance(cause, httpx.HTTPStatusError):
        status = cause.response.status_code
        return 400 <= status < 500 and status != 404
    return False


def gate(queue_id: int, keep: int, retire: int, *, judge: bool = True) -> str | None:
    """:data:`CONFIRMED` (retire) or why not: HOLD_JUDGE / HOLD_FAILED stamped now,
    GATE_DEFERRED (``judge=False``: the pass lost its judge), or "" (nothing stamped:
    the owner closed the row meanwhile). An outage raises ExtractorUnavailable."""
    assert_retire_active(retire)
    with connect() as conn:
        row = conn.execute(
            "SELECT detail->'judged_by' FROM review_queue WHERE id = %s", (queue_id,)
        ).fetchone()
    if row is not None and row[0] == seat():
        return CONFIRMED  # queued by this very seat: a re-ask is the same request
    if not judge:
        return GATE_DEFERRED
    kind, why = HOLD_JUDGE, "finds no conflict"
    try:
        if judge_confirms(keep, retire):
            return CONFIRMED
    except ExtractorUnavailable as exc:
        if not per_pair_failure(exc):
            raise
        kind, why = HOLD_FAILED, f"gave no verdict ({type(exc.__cause__).__name__})"
    reason = hold_reason(settings.contradiction_judge_model, why)
    if not hold_row(queue_id, keep, retire, kind, reason):
        return ""
    logger.info("contradiction row %s held: %s", queue_id, reason)
    return kind


def hold_reason(model: str, why: str = "finds no conflict") -> str:
    """The ASCII reason a gate-held row carries in ``detail.auto_resolve_hold.reason``."""
    return f"held: the contradiction judge ({model}) {why} - owner decision"


def hold_row(
    queue_id: int, keep: int, retire: int, kind: str = HOLD_JUDGE, reason: str | None = None
) -> bool:
    """Stamp a gate hold on an OPEN row; the row stays pending. True iff new.

    Idempotent (a row already held for the same ``(kind, keep, retire, model)`` is
    left byte-identical — its first ``at`` stands) and never touches a resolved row
    or an owner's ``owner_restore`` veto. A JSONB merge into the row's hold key.
    """
    model = settings.contradiction_judge_model
    probe = {"kind": kind, "keep": keep, "retire": retire, "model": model}
    stamp = {
        **probe,
        "reason": reason or hold_reason(model),
        "at": datetime.now(UTC).isoformat(),
    }
    with transaction() as conn:
        row = conn.execute(
            "UPDATE review_queue SET detail = COALESCE(detail, '{}'::jsonb) || %s "
            "WHERE id = %s AND resolved = false "
            "AND NOT COALESCE(detail->'auto_resolve_hold' @> %s, false) "
            "AND COALESCE(detail->'auto_resolve_hold'->>'kind', '') <> %s RETURNING id",
            (Jsonb({HOLD_KEY: stamp}), queue_id, Jsonb(probe), HOLD_RESTORED),
        ).fetchone()
    return row is not None

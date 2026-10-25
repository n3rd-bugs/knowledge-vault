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
retires on an unconfirmed conflict; a reply that carries no verdict never counts
as one (below).

PROVENANCE. The nightly step stamps ``detail.judged_by`` (model + think) on every
row it queues. The gate still re-asks even when that is the current seat: the
detection step reads the reply leniently (``bool(val)`` — a malformed
``{"contradict": ["false"]}`` queues the pair), and only this strict three-state
re-ask (:func:`verdict`) may authorise a retirement. Same request, same seed, so a
clean "yes" stays a yes; the cost is one judge call per candidate, batched after
the evidence refresh so the judge model loads once per pass.

NO VERDICT. When the judge gives no usable verdict on THIS pair (a read timeout,
an unusable or unparseable reply, a token-repeat abort) the row is NOT
retired and the failure is counted on it (``judge_failed``, ``attempts``); it is
re-judged next pass, and only the third consecutive failure makes the hold final
— a slot contended by an interactive job must not park pairs for good. Two
per-pair failures in a row within one pass trip the pass breaker (the worker then
treats the judge as down). An outage (connect error, any HTTP status) raises.

STICKY. A ``judge_no_conflict`` (or final ``judge_failed``) hold stays in force
until the owner acts (:func:`kbcore.review_recency.hold_in_force`): a held row
keeps a streak already past the threshold, so releasing it on a gate-off or seat
rollback would retire it on the very next pass — the defect this gate stops.
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
from kbcore.errors import ExtractorUnavailable
from kbcore.review_recency import (
    FAILED_FINAL_AT,
    GATE_DEFERRED,
    GATE_RETRY,
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
    """Record the current seat on the OPEN contradiction row for ``a -> b`` (just
    queued by the nightly step) — provenance: which judge put the pair in the queue."""
    with transaction() as conn:
        conn.execute(
            "UPDATE review_queue SET detail = detail || %s WHERE reason = 'contradiction' "
            "AND resolved = false AND (detail->>'a_id')::bigint = %s "
            "AND (detail->>'b_id')::bigint = %s",
            (Jsonb({"judged_by": seat()}), a_id, b_id),
        )


def verdict(keep: int, retire: int) -> bool | None:
    """The contradiction seat's THREE-state verdict: True / False, or None when the
    reply carries none (empty, unparseable, no usable ``contradict``). The request is
    byte-identical to :func:`kbcore.llm.items_contradict`'s (lower id as Item A, the
    same prompt, per-content seed and seat), whose bool contract maps None to False.
    Raises :class:`~kbcore.errors.ExtractorUnavailable` when the judge fails."""
    a_id, b_id = (keep, retire) if keep <= retire else (retire, keep)
    (a_title, a_body), (b_title, b_body) = item_pair_texts(a_id, b_id)
    content = llm._chat(
        [
            {"role": "system", "content": llm._CONTRADICT_SYSTEM},
            {
                "role": "user",
                "content": f"Item A: {a_title}\n{a_body}\n\nItem B: {b_title}\n{b_body}",
            },
        ],
        json_format=True,
        seed=llm._draw_seed(a_title, a_body, b_title, b_body),
        model=settings.contradiction_judge_model,
        think=settings.contradiction_judge_think,
    )
    try:
        data = llm.parse_model_json(content)
    except (ValueError, TypeError):
        return None
    val = data.get("contradict") if isinstance(data, dict) else None
    if isinstance(val, bool):
        return val
    if isinstance(val, str) and val.strip().lower() in ("true", "yes", "1", "false", "no", "0"):
        return val.strip().lower() in ("true", "yes", "1")
    return None


def per_pair_failure(exc: ExtractorUnavailable) -> bool:
    """A judge failure that may be about THIS pair (a read timeout, a reply _chat could
    not use) rather than an outage. Any HTTP status is an outage: Ollama's 4xx are
    request-shape errors common to every pair (e.g. think on a non-thinking model)."""
    cause = exc.__cause__
    if isinstance(cause, KeyError | ValueError | TypeError):  # unusable reply body
        return True
    return isinstance(cause, httpx.TimeoutException) and not isinstance(cause, httpx.ConnectTimeout)


def gate(queue_id: int, keep: int, retire: int, *, judge: bool = True) -> str | None:
    """:data:`CONFIRMED` (retire) or why not: HOLD_JUDGE stamped now, GATE_RETRY (no
    verdict, counted; re-judged next pass), HOLD_FAILED (no verdict a third time:
    final), GATE_DEFERRED (``judge=False``: the pass lost its judge), or "" (nothing
    stamped: the owner closed the row meanwhile). An outage raises."""
    with connect() as conn:
        row = conn.execute(
            "SELECT resolved FROM review_queue WHERE id = %s", (queue_id,)
        ).fetchone()
    if row is None or row[0] is True:
        return ""  # closed since the snapshot: nothing to judge
    if not judge:
        return GATE_DEFERRED
    try:
        answer = verdict(keep, retire)
    except ExtractorUnavailable as exc:
        if not per_pair_failure(exc):
            raise
        return fail_row(queue_id, keep, retire, type(exc.__cause__).__name__)
    if answer is None:
        return fail_row(queue_id, keep, retire, "unparseable")
    if answer:
        return CONFIRMED
    reason = hold_reason(settings.contradiction_judge_model)
    if not hold_row(queue_id, keep, retire, HOLD_JUDGE, reason):
        return ""
    logger.info("contradiction row %s held: %s", queue_id, reason)
    return HOLD_JUDGE


def fail_row(queue_id: int, keep: int, retire: int, what: str) -> str:
    """Count a no-verdict on an OPEN row (same pair + seat: attempts + 1, else 1); the
    third makes the hold final. GATE_RETRY / HOLD_FAILED, or "" when the row was
    closed or carries an owner veto meanwhile."""
    model = settings.contradiction_judge_model
    with transaction() as conn:
        row = conn.execute(
            "SELECT detail->'auto_resolve_hold' FROM review_queue "
            "WHERE id = %s AND resolved = false FOR UPDATE",
            (queue_id,),
        ).fetchone()
        prior = (row[0] if row else None) or {}
        if row is None or prior.get("kind") == HOLD_RESTORED:
            return ""
        same = (prior.get("kind"), prior.get("keep"), prior.get("retire"), prior.get("model"))
        attempts = (
            (prior.get("attempts") or 0) + 1 if same == (HOLD_FAILED, keep, retire, model) else 1
        )
        final = attempts >= FAILED_FINAL_AT
        reason = hold_reason(model, f"gave no verdict ({what}; attempt {attempts})")
        stamp = {"kind": HOLD_FAILED, "keep": keep, "retire": retire, "model": model,
                 "attempts": attempts, "final": final, "reason": reason,
                 "at": datetime.now(UTC).isoformat()}  # fmt: skip
        conn.execute(
            "UPDATE review_queue SET detail = COALESCE(detail, '{}'::jsonb) || %s WHERE id = %s",
            (Jsonb({HOLD_KEY: stamp}), queue_id),
        )
    logger.warning("contradiction row %s: %s", queue_id, reason)
    return HOLD_FAILED if final else GATE_RETRY


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

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

The hold is in force while the suggestion it judged AND the seat's model are both
unchanged (:func:`kbcore.review_recency.hold_in_force`): swapping the judge model,
or switching the gate off, releases held rows back into the nightly refresh.
"""

from __future__ import annotations

from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from kbcore import llm
from kbcore.config import settings
from kbcore.consolidation import item_pair_texts
from kbcore.db import transaction
from kbcore.review_recency import HOLD_JUDGE, HOLD_KEY, HOLD_RESTORED


def judge_confirms(keep: int, retire: int) -> bool:
    """The contradiction seat's verdict on the pair. Raises
    :class:`~kbcore.errors.ExtractorUnavailable` when the judge is unreachable."""
    a_id, b_id = (keep, retire) if keep <= retire else (retire, keep)
    (a_title, a_body), (b_title, b_body) = item_pair_texts(a_id, b_id)
    return llm.items_contradict(a_title, a_body, b_title, b_body)


def hold_reason(model: str) -> str:
    """The ASCII reason a gate-held row carries in ``detail.auto_resolve_hold.reason``."""
    return f"held: the contradiction judge ({model}) finds no conflict - owner decision"


def hold_row(queue_id: int, keep: int, retire: int) -> bool:
    """Stamp the gate hold on an OPEN row; the row stays pending. True iff new.

    Idempotent (a row already held for the same ``(keep, retire, model)`` is left
    byte-identical — its first ``at`` stands) and never touches a resolved row or
    an owner's ``owner_restore`` veto. A JSONB merge into the row's hold key.
    """
    model = settings.contradiction_judge_model
    probe = {"kind": HOLD_JUDGE, "keep": keep, "retire": retire, "model": model}
    stamp = {**probe, "reason": hold_reason(model), "at": datetime.now(UTC).isoformat()}
    with transaction() as conn:
        row = conn.execute(
            "UPDATE review_queue SET detail = COALESCE(detail, '{}'::jsonb) || %s "
            "WHERE id = %s AND resolved = false "
            "AND NOT COALESCE(detail->'auto_resolve_hold' @> %s, false) "
            "AND COALESCE(detail->'auto_resolve_hold'->>'kind', '') <> %s RETURNING id",
            (Jsonb({HOLD_KEY: stamp}), queue_id, Jsonb(probe), HOLD_RESTORED),
        ).fetchone()
    return row is not None

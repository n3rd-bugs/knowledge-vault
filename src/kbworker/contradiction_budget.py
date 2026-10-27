"""kbworker.contradiction_budget — how much judging the nightly contradiction step may spend.

Split from :mod:`kbworker.tail_ops` (300-line ratchet) when the no-verdict breaker landed
(KB 55589): the day-aware pair cap, moved verbatim, and the breaker. ``tail_ops`` re-exports
both, so callers and tests keep reaching ``tail_ops._contradiction_pair_cap``. The breaker
takes the stats dict as an argument — this module never imports ``tail_ops`` (no cycle).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from kbcore.config import settings

#: From this many pairs judged in a run on, half or more without a verdict stands the step down.
NO_VERDICT_MIN_JUDGED = 6


def _contradiction_pair_cap(now: datetime | None = None) -> int:
    """Tonight's contradiction-pair budget — day-aware (review #6), the sibling of
    ``consolidate._evidence_pair_cap`` for the step before it.

    Unbounded days (``kbcore.drainsched.is_unbounded_day`` — the single "box is
    free tonight" definition) keep the full ``kb_contradiction_pair_cap`` (500,
    the catch-up bound); deadline days bind at ``min(cap,
    kb_contradiction_pair_cap_weekday)`` — measured 129-153 s/pair, so the
    154-pair nights spent ~21 binding minutes past what 40 covers, growing with
    ingest. Convergence-safe: judged-pair memory persists across nights
    (migration 017) and the candidate query is sim-DESC, so a capped night
    spends its budget on the closest pairs and never re-pays judged ones. This
    cap is also what bounds the re-judging wave after any embedder swap. ``min``
    keeps BOTH kill switches (0 in the main knob kills everywhere; 0 in the
    weekday knob is weekday-only). ``now`` is for day-pinned tests.
    """
    from kbcore.drainsched import is_unbounded_day

    cap = int(settings.kb_contradiction_pair_cap)
    if is_unbounded_day(now):
        return cap
    return min(cap, int(settings.kb_contradiction_pair_cap_weekday))


def no_verdict_guard(
    stop: Callable[[], str | None] | None, stats: Mapping[str, int]
) -> Callable[[], str | None]:
    """``stop`` plus the breaker. A reply without a verdict is never memorized (KB 55589), so
    a seat that cannot answer at all — misconfigured, or every draw running out of room —
    would spend the whole cap (500 on an unbounded night) on replies that change nothing,
    then do it again the next night. Once :data:`NO_VERDICT_MIN_JUDGED` FRESH pairs are
    judged in the run and half or more came back without a verdict, the step stands down
    with reason "no verdict", recorded and reported like a deadline cut; an occasional
    blank never trips it. Only fresh pairs count — a pair that blanked before and is being
    retried (:mod:`kbworker.contradiction_blanks`) does not — so a fixed block of such
    pairs can neither trip it nor hold the head of the queue. The step asks every few
    pairs, so a seat that cannot answer costs about ten pairs."""

    def check() -> str | None:
        if stop is not None and (reason := stop()):
            return reason
        judged, blank = stats["fresh_judged"], stats["fresh_blank"]
        return "no verdict" if judged >= NO_VERDICT_MIN_JUDGED and 2 * blank >= judged else None

    return check

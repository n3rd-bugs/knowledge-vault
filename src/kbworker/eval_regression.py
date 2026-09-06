"""kbworker.eval_regression — the sentinel's regression detector (§8).

Split from :mod:`kbworker.eval_sentinel` (mechanical move, review #5 Wave F):
the verified-lift watch and chain-resolution telemetry pushed that file to the
300-line ratchet, and the detector is its own concern — pure comparison logic
over tonight's metrics, the prior run's baseline and the anchored floors.
``eval_sentinel`` re-imports every name (the established seam every caller and
test already uses, same pattern as kbworker.tail_steps / kbworker.digest), and
the logger keeps the ``kbworker.eval_sentinel`` name these WARNING lines have
always carried in journald.

DETECTOR HYGIENE (review #9a EV-2, KB 31857 follow-up): the baseline and the
recovery anchor read only FALLBACK-FREE prior evals (``kbcore.eval_baseline``),
and a trip measured on a night whose own replay fell back is a MEASUREMENT,
not a corpus regression — labelled as such in the WARNING, recorded under the
ADDED key ``eval_regression_measurement`` and never enqueued as a
``search_regression`` row; nor does such a night auto-resolve an open row
(a contaminated HIGH is not evidence of recovery either). ``eval_regression``
keeps its meaning: True only for a fallback-free trip.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from kbcore import eval_baseline
from kbcore import review as review_mod
from kbcore.config import settings

logger = logging.getLogger("kbworker.eval_sentinel")

REGRESSION_EPSILON = 0.05  # absolute hit@5 / MRR drop vs the prior run that trips it
REVIEW_REASON = "search_regression"


def _tonights_fallbacks(counts: Mapping[str, Any]) -> int:
    """How many of tonight's primary-replay cases fell back to the fused order.

    ``eval_sentinel._run`` snapshots ``counts['eval_rerank_tally']`` BEFORE it
    calls the detector; a run without the tally (rerank off) reads as 0.
    """
    return eval_baseline.rerank_fallbacks(counts)


def _detect_regression(counts: dict[str, Any], hit_at_5: float, mrr: float) -> None:
    prior = eval_baseline.previous_eval_metrics()
    open_row = review_mod.open_review_by_reason(REVIEW_REASON)
    floor_h = settings.kb_eval_min_hit5
    floor_m = settings.kb_eval_min_mrr
    fallbacks = _tonights_fallbacks(counts)

    # Two independent trips: (1) a drop beyond epsilon vs the previous run, and
    # (2) an ANCHORED ABSOLUTE floor. The floor catches a gradual sub-epsilon drift
    # the previous-run comparison can never see (0.90 → 0.0 at 0.05/night is silent
    # otherwise), and it fixes the downward-ratcheting auto-resolve baseline (finding).
    below_floor = hit_at_5 < floor_h or mrr < floor_m
    dropped = prior is not None and (
        prior["hit_at_5"] - hit_at_5 > REGRESSION_EPSILON or prior["mrr"] - mrr > REGRESSION_EPSILON
    )
    if (below_floor or dropped) and fallbacks:
        # A trip on a fallback night is a MEASUREMENT (module docstring): say so,
        # record it, and leave the queue alone — the next fallback-free night
        # re-measures against the same clean baseline; the floor still backstops.
        counts["eval_regression_measurement"] = {
            "rerank_fallback": fallbacks,
            "hit_at_5": round(hit_at_5, 4),
            "mrr": round(mrr, 4),
            "below_floor": below_floor,
            "dropped": bool(dropped),
        }
        logger.warning(
            "SEARCH-QUALITY measurement (rerank fallback %d): hit@5 %.3f MRR %.3f tripped "
            "(prior %s, floor %.2f/%.2f, epsilon %.2f) on a replay measured partly on the "
            "fused order — not recorded as a corpus regression",
            fallbacks,
            hit_at_5,
            mrr,
            f"{prior['hit_at_5']:.3f}/{prior['mrr']:.3f}" if prior else "none",
            floor_h,
            floor_m,
            REGRESSION_EPSILON,
        )
        return
    if below_floor or dropped:
        counts["eval_regression"] = True
        # Recovery ANCHOR = the pre-drift baseline we must climb back to. Never let
        # it ratchet DOWN: take the max of the prior run, the floor, and any anchor
        # already on the open row — so a partial rebound cannot self-close a still
        # degraded state (the flap the finding describes).
        base_h = max(prior["hit_at_5"] if prior else 0.0, floor_h)
        base_m = max(prior["mrr"] if prior else 0.0, floor_m)
        if open_row is not None:
            _qid, det = open_row
            base_h = max(base_h, float(det.get("baseline_hit_at_5", 0.0)))
            base_m = max(base_m, float(det.get("baseline_mrr", 0.0)))
        logger.warning(
            "SEARCH-QUALITY REGRESSION: hit@5 %.3f MRR %.3f (prior %s, floor %.2f/%.2f, "
            "recovery anchor %.3f/%.3f, epsilon %.2f)",
            hit_at_5,
            mrr,
            f"{prior['hit_at_5']:.3f}/{prior['mrr']:.3f}" if prior else "none",
            floor_h,
            floor_m,
            base_h,
            base_m,
            REGRESSION_EPSILON,
        )
        if open_row is None:  # dedup: exactly one open flag at a time
            review_mod.queue_review(
                None,
                REVIEW_REASON,
                {
                    "baseline_hit_at_5": round(base_h, 4),
                    "baseline_mrr": round(base_m, 4),
                    "observed_hit_at_5": round(hit_at_5, 4),
                    "observed_mrr": round(mrr, 4),
                    "floor_hit_at_5": floor_h,
                    "floor_mrr": floor_m,
                    "epsilon": REGRESSION_EPSILON,
                },
            )
        return
    # No regression: self-heal a stale flag only on FULL recovery to the SAME
    # anchor (>= baseline, not baseline − epsilon) AND above the floor, so a partial
    # rebound never auto-retracts a still-degraded state and drags the baseline down.
    # Never on a fallback night: a stalled replay's number is not evidence either way.
    if open_row is not None and fallbacks:
        logger.info(
            "search-quality: open regression row %d left as is — tonight's replay fell back "
            "on %d case(s), so its numbers cannot prove recovery",
            open_row[0],
            fallbacks,
        )
        return
    if open_row is not None:
        queue_id, detail = open_row
        base_h = float(detail.get("baseline_hit_at_5", 0.0))
        base_m = float(detail.get("baseline_mrr", 0.0))
        if hit_at_5 >= base_h and mrr >= base_m and hit_at_5 >= floor_h and mrr >= floor_m:
            review_mod.resolve_review(queue_id)
            logger.info(
                "search-quality recovered to baseline; auto-resolved review row %d", queue_id
            )

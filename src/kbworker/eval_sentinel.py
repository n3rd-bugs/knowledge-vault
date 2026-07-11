"""kbworker.eval_sentinel — search-quality regression sentinel (§8).

At the END of a successful nightly consolidation, replays the golden retrieval
eval (hit@5 + MRR) and compares it against the PREVIOUS run's metrics — read back
from the last report item's ``meta`` (no new table). A drop beyond
REGRESSION_EPSILON logs a WARNING and enqueues ONE ``search_regression`` review
row so the owner sees it in `kb review list`; quality climbing back to baseline
auto-resolves that row. It NEVER fails or aborts consolidation — every path is
caught.

GPU courtesy (Invariant 8): real searches → bge-m3 embeds only; kbcore.search has
no extractor path, so the 14B is never invoked. Kill switch:
``kb_eval_in_consolidation``.
"""

from __future__ import annotations

import logging
from typing import Any

from kbcore import consolidation, eval_harness
from kbcore import review as review_mod
from kbcore.config import settings

logger = logging.getLogger("kbworker.eval_sentinel")

REGRESSION_EPSILON = 0.05  # absolute hit@5 / MRR drop vs the prior run that trips it
REVIEW_REASON = "search_regression"


def run_sentinel(counts: dict[str, Any]) -> None:
    """Replay the eval, record metrics into ``counts``, flag regressions.

    Records ``eval_status`` (ok / skipped / disabled / error) plus, when ok,
    ``eval_hit_at_5`` / ``eval_mrr`` / ``eval_queries`` / ``eval_regression``.
    Wrapped so a broken eval (missing golden, embed down, …) never breaks the run.
    """
    if not settings.kb_eval_in_consolidation:
        counts["eval_status"] = "disabled"  # kill switch: skip the eval entirely
        return
    try:
        _run(counts)
    except Exception:  # noqa: BLE001 — the sentinel must never break consolidation
        logger.exception("search-quality sentinel errored; consolidation unaffected")
        counts["eval_status"] = "error"


def _run(counts: dict[str, Any]) -> None:
    golden = eval_harness.load_golden()
    if not golden:
        counts["eval_status"] = "skipped"  # golden set missing/empty
        logger.info("search-quality sentinel: golden set missing/empty — eval skipped")
        return
    _results, hit_at_5, mrr = eval_harness.evaluate(golden=golden)
    counts["eval_status"] = "ok"
    counts["eval_hit_at_5"] = round(hit_at_5, 4)
    counts["eval_mrr"] = round(mrr, 4)
    counts["eval_queries"] = len(golden)
    counts["eval_regression"] = False
    logger.info(
        "search-quality eval: hit@5=%.3f MRR=%.3f over %d queries", hit_at_5, mrr, len(golden)
    )
    _detect_regression(counts, hit_at_5, mrr)


def _detect_regression(counts: dict[str, Any], hit_at_5: float, mrr: float) -> None:
    prior = consolidation.previous_eval_metrics()
    open_row = review_mod.open_review_by_reason(REVIEW_REASON)
    floor_h = settings.kb_eval_min_hit5
    floor_m = settings.kb_eval_min_mrr

    # Two independent trips: (1) a drop beyond epsilon vs the previous run, and
    # (2) an ANCHORED ABSOLUTE floor. The floor catches a gradual sub-epsilon drift
    # the previous-run comparison can never see (0.90 → 0.0 at 0.05/night is silent
    # otherwise), and it fixes the downward-ratcheting auto-resolve baseline (finding).
    below_floor = hit_at_5 < floor_h or mrr < floor_m
    dropped = prior is not None and (
        prior["hit_at_5"] - hit_at_5 > REGRESSION_EPSILON or prior["mrr"] - mrr > REGRESSION_EPSILON
    )
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
    if open_row is not None:
        queue_id, detail = open_row
        base_h = float(detail.get("baseline_hit_at_5", 0.0))
        base_m = float(detail.get("baseline_mrr", 0.0))
        if hit_at_5 >= base_h and mrr >= base_m and hit_at_5 >= floor_h and mrr >= floor_m:
            review_mod.resolve_review(queue_id)
            logger.info(
                "search-quality recovered to baseline; auto-resolved review row %d", queue_id
            )

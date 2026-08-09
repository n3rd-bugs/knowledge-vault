"""kbworker.eval_sentinel — search-quality regression sentinel + instruments (§8).

At the END of a successful nightly consolidation, replays the golden retrieval
eval (hit@5 + MRR) and compares it against the PREVIOUS run's metrics — read back
from the last report item's ``meta`` (no new table). A drop beyond
REGRESSION_EPSILON logs a WARNING and enqueues ONE ``search_regression`` review
row so the owner sees it in `kb review list`; quality climbing back to baseline
auto-resolves that row. It NEVER fails or aborts consolidation — every path is
caught.

Two REPORT-ONLY instruments ride along after the primary metrics, each behind its
own guard so a broken instrument can never cost us the metric that gates
regressions: the graph-expansion A/B (:func:`_graph_expansion_ab`) and the
top-5 trust composition (:func:`_trust_composition`).

Cost: with the A/B PARKED (:data:`GRAPH_AB_ENABLED` False) the nightly eval
replays the golden set TWICE (primary + the trust/composition pass); re-arming
the A/B restores its two extra replays. GPU courtesy (Invariant 8) is unchanged:
real searches → embeds only; kbcore.search has no extractor path, so the 14B is
never invoked. Kill switches: ``kb_eval_in_consolidation`` (whole eval),
:data:`GRAPH_AB_ENABLED`, :data:`TRUST_METRIC_ENABLED`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from kbcore import consolidation, eval_harness, search_expand
from kbcore import review as review_mod
from kbcore.config import settings
from kbcore.search import search

logger = logging.getLogger("kbworker.eval_sentinel")

REGRESSION_EPSILON = 0.05  # absolute hit@5 / MRR drop vs the prior run that trips it
REVIEW_REASON = "search_regression"
# Instrument switches. Deliberately module constants, not config knobs: these
# only decide whether a measurement is TAKEN — no production retrieval path
# reads them, and a .env-tunable measurement is one more thing that can silently
# be off when the owner goes looking for the number.
# GRAPH_AB PARKED 2026-08-09 (Wave-3): the A/B measured ~0 delta — the live corpus
# has no relation-ONLY golden case and the three staged fixtures do not resolve
# (every typed neighbour of a strong seed is already a direct top-5 hit, is
# superseded, or is a nightly-regenerated summary with an unstable id). Off
# reclaims the two nightly A/B replays; re-enable with real proposed fixtures.
GRAPH_AB_ENABLED = False
TRUST_METRIC_ENABLED = True
TRUST_K = 5  # top-K window the trust composition is measured over


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
    # Instruments LAST and independently guarded: the regression flag above is
    # the load-bearing output, and a measurement that errors must cost nothing.
    if GRAPH_AB_ENABLED:
        _instrument(counts, "eval_ab_status", lambda: _graph_expansion_ab(golden))
    else:
        counts["eval_ab_status"] = "disabled"
    if TRUST_METRIC_ENABLED:
        _instrument(counts, "eval_trust_status", lambda: _trust_composition(golden))
    else:
        counts["eval_trust_status"] = "disabled"


def _instrument(
    counts: dict[str, Any], status_key: str, measure: Callable[[], dict[str, Any]]
) -> None:
    """Run one report-only measurement, folding its result into ``counts``.

    The measurement returns its whole result dict, so a failure leaves NO partial
    metrics behind — the owner reads either a complete instrument or
    ``<status_key> = error``, never half a number they might act on.
    """
    try:
        counts.update(measure())
        counts[status_key] = "ok"
    except Exception:  # noqa: BLE001 — an instrument must never break the sentinel
        logger.exception("%s: instrument failed; primary eval metrics unaffected", status_key)
        counts[status_key] = "error"


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


# --- instrument 1: graph-expansion A/B ---------------------------------------


def _ab_side(golden: list[dict], expand: bool) -> tuple[float, float]:
    """(hit@5, MRR) for the golden set with expansion FORCED to ``expand``.

    Reuses the SHIPPED knob end-to-end — the case's ``kwargs.expand`` channel,
    which eval_harness maps through ``search_expand.hops_from_expand`` into
    ``search(hops=...)`` exactly as the MCP surface does. Nothing about
    expansion is reimplemented here, so the A/B measures production code.

    The force is UNIFORM: a case that pins its own ``expand`` (eval/golden.yaml
    has one) is overridden on BOTH sides. That is what makes the two numbers
    comparable — a per-case knob would leave the sides measuring different
    configurations. Cases are copied, never mutated in place: the primary
    metrics were computed from the originals and must stay reproducible.
    """
    forced = [
        {**case, "kwargs": {**(case.get("kwargs") or {}), "expand": expand}} for case in golden
    ]
    _results, hit_at_5, mrr = eval_harness.evaluate(golden=forced)
    return hit_at_5, mrr


def _graph_expansion_ab(golden: list[dict]) -> dict[str, Any]:
    """Answer "does graph expansion help on THIS corpus?" from data, per night.

    Runs the golden set with expansion OFF (hops=0) and ON
    (``hops_from_expand(True)`` = ``max(KB_GRAPH_EXPAND_HOPS, 1)``, the MCP
    ``expand=true`` rule) and reports each side's hit@5/MRR plus the delta
    (ON − OFF). REPORT-ONLY — flips no default, feeds no regression check.

    PARKED (:data:`GRAPH_AB_ENABLED` False, 2026-08-09): the delta is only as
    informative as the relation-ONLY cases behind it, and the corpus has none
    that resolve (see eval/golden.proposed.yaml). Kept wired so a future fixture
    set re-arms it by flipping one constant; the mechanism stays test-covered.
    """
    hops_on = search_expand.hops_from_expand(True)
    off_hit, off_mrr = _ab_side(golden, expand=False)
    on_hit, on_mrr = _ab_side(golden, expand=True)
    logger.info(
        "graph-expansion A/B over %d queries: OFF(hops=0) hit@5=%.3f MRR=%.3f | "
        "ON(hops=%s) hit@5=%.3f MRR=%.3f | delta %+.3f/%+.3f",
        len(golden),
        off_hit,
        off_mrr,
        hops_on,
        on_hit,
        on_mrr,
        on_hit - off_hit,
        on_mrr - off_mrr,
    )
    return {
        "eval_ab_hops_on": hops_on,
        "eval_ab_off_hit_at_5": round(off_hit, 4),
        "eval_ab_off_mrr": round(off_mrr, 4),
        "eval_ab_on_hit_at_5": round(on_hit, 4),
        "eval_ab_on_mrr": round(on_mrr, 4),
        "eval_ab_delta_hit_at_5": round(on_hit - off_hit, 4),
        "eval_ab_delta_mrr": round(on_mrr - off_mrr, 4),
    }


# --- instrument 2: trust composition of the top-5 ----------------------------


def _trust_composition(golden: list[dict]) -> dict[str, Any]:
    """Trust AND type composition of the golden queries' top-5 (report-only).

    Two shares, POOLED (micro) over every top-5 hit of every query so a query
    returning one hit does not weigh like one returning five:

    * ``eval_top5_unverified_share`` — hits with trust != 'verified'. ~91% of the
      corpus is unverified (quote-grounded auto-extractions, Invariant 4) and
      NOTHING else sees whether the verified minority SURFACES: a golden case
      passes identically whether its answer was hand-verified or unreviewed.
    * ``eval_top5_summary_share`` — hits whose ``type == 'summary'``.
      Consolidation-minted summaries are ~9% of items, all unverified, and exempt
      from every hygiene pass; a month of this share says whether they earn their
      top-5 slots or crowd out the primaries they summarize.

    Structurally report-only: replays the QUERIES read-only with ``tool=None`` (no
    access_log write — eval reads are not usage), touches no ranking input, and
    returns numbers. Retrieval order is unchanged. Measured on the PRODUCTION
    DEFAULT config — plain query, default hops, no per-case knobs — because the
    question is what an ordinary agent query surfaces.
    """
    unverified = 0
    summary = 0
    total = 0
    for case in golden:
        query = case.get("q")
        if not query:
            continue
        for hit in search(query, k=TRUST_K, tool=None):
            total += 1
            unverified += int(hit.trust != "verified")
            summary += int(hit.type == "summary")
    unverified_share = round(unverified / total, 4) if total else 0.0
    summary_share = round(summary / total, 4) if total else 0.0
    logger.info(
        "top-%d composition over %d queries: %d/%d unverified (%.1f%%), %d/%d summary (%.1f%%)",
        TRUST_K,
        len(golden),
        unverified,
        total,
        100 * unverified_share,
        summary,
        total,
        100 * summary_share,
    )
    return {
        "eval_top5_unverified_share": unverified_share,
        "eval_top5_unverified": unverified,
        "eval_top5_summary_share": summary_share,
        "eval_top5_summary": summary,
        "eval_top5_results": total,
    }

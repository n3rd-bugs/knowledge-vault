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

from kbcore import eval_harness, search_expand
from kbcore.config import settings
from kbcore.search import search
from kbworker import eval_trust

# THE ESTABLISHED SEAM (mechanical split, review #5 Wave F): the regression
# detector moved to its own module when the verified-lift watch reached this
# file's 300-line ratchet. Every caller and test keeps reaching it as
# ``eval_sentinel.<name>`` — same pattern as kbworker.tail_steps / digest.
from kbworker.eval_regression import (  # noqa: F401
    REGRESSION_EPSILON,
    REVIEW_REASON,
    _detect_regression,
)

logger = logging.getLogger("kbworker.eval_sentinel")
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
    # Chain-resolution telemetry (review #5 ret-eval-1): how many id pins the
    # harness followed to a live chain head tonight — doc-refresh churn becomes
    # a meta number instead of a red night. getattr, so a stubbed evaluate()
    # returning bare tuples (the sentinel test rig) still counts 0.
    counts["eval_chain_resolved"] = sum(1 for r in _results if getattr(r, "chain_resolved", False))
    # Review #5 ops-4: which rerank path scored tonight's replay — 'active'
    # (sidecar scored), 'fallback' (sidecar failed, fused order stood), 'off'.
    # A red night reads differently if the reranker silently sat out.
    try:
        from kbcore import rerank as _rerank

        counts["eval_rerank_outcome"] = _rerank.last_outcome()
    except Exception:  # noqa: BLE001 — observability may never fail the eval
        pass
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
    ``<status_key> = error``, never half a number they might act on. A
    measurement may set its OWN status (the trust watch reports 'watch');
    'ok' fills in only when it did not.
    """
    try:
        counts.update(measure())
        counts.setdefault(status_key, "ok")
    except Exception:  # noqa: BLE001 — an instrument must never break the sentinel
        logger.exception("%s: instrument failed; primary eval metrics unaffected", status_key)
        counts[status_key] = "error"


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

    Two Wave-F riders on the same read-only pass (review #5): the DISTINCT top-5
    item ids are persisted (``eval_top5_item_ids`` — the weekly promote table
    prefers candidates the eval actually surfaces; they were counted but never
    persisted), and :func:`kbworker.eval_trust.verified_lift` turns the share
    into the watched lift ratio (+ ``eval_trust_status='watch'`` on a trip).
    """
    unverified = 0
    summary = 0
    total = 0
    top5_ids: set[int] = set()
    for case in golden:
        query = case.get("q")
        if not query:
            continue
        for hit in search(query, k=TRUST_K, tool=None):
            total += 1
            unverified += int(hit.trust != "verified")
            summary += int(hit.type == "summary")
            top5_ids.add(hit.item_id)
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
    result = {
        "eval_top5_unverified_share": unverified_share,
        "eval_top5_unverified": unverified,
        "eval_top5_summary_share": summary_share,
        "eval_top5_summary": summary,
        "eval_top5_results": total,
        "eval_top5_item_ids": sorted(top5_ids),
    }
    result.update(eval_trust.verified_lift(unverified_share, len(golden)))
    return result

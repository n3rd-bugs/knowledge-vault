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

E1 TELEMETRY (review #8 H5, KB 31857): the composition pass also records WHICH
hit satisfied each golden case — ``eval_satisfier_types`` /
``eval_summary_only_cases`` / ``eval_satisfier_ids`` (kbworker.eval_composition)
— the >=5-night series the eval epoch's summaries verdict and identifier-mass
go/no-go read. The rerank rollup ``eval_rerank_outcome`` is fallback > active >
off and the digest renders the tally verbatim (H7a).

MRR QUANTUM: MRR is a mean of 1/rank over the positive-criterion cases, so ONE
golden case slipping rank 1 -> 2 moves it by exactly (1 - 1/2) / n_positive =
0.5/52 = 0.0096 on today's set — or an equivalent decomposition (two cases
slipping 2 -> 3 and 3 -> 4 read the same). The 08-29 step 0.9365 -> 0.9269 was
exactly one quantum of corpus composition (661 new summaries + doc-refresh
churn, 22/22 top-5 ids swapped). A single-quantum move is a PER-CASE read at
the eval epoch, never drift; REGRESSION_EPSILON (0.05) is ~5 quanta away.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from kbcore import eval_harness, search_expand
from kbcore.config import settings

# `search` is the composition instrument's patch surface (tests patch
# `eval_sentinel.search`); kbworker.eval_composition reads it back through here.
from kbcore.search import search  # noqa: F401

# The top-5 composition instrument moved to kbworker.eval_composition
# (mechanical split, 2026-09-03, review #8 H0) — same seam: callers and tests
# reach it as ``eval_sentinel._trust_composition`` / ``TRUST_K``, and it reads
# ``search`` back through this module (see its docstring).
from kbworker.eval_composition import (  # noqa: F401
    TRUST_K,
    _trust_composition,
)

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


def run_sentinel(counts: dict[str, Any]) -> None:
    """Replay the eval, record metrics into ``counts``, flag regressions.

    Records ``eval_status`` (ok / skipped / disabled / error) plus, when ok,
    ``eval_hit_at_5`` / ``eval_mrr`` / ``eval_queries`` / ``eval_regression``.
    Wrapped so a broken eval (missing golden, embed down, …) never breaks the run.

    SELF-TIMED into ``counts['step_secs']['eval_sentinel']`` (review #6): the
    consolidate call site cannot run this step through ``_run_step`` (it fills
    ``counts`` itself instead of returning one number), so it reports into the
    same cost ledger itself — an ADDED step_secs key, nothing renamed — and
    mirrors ``_run_step``'s >=60s WARNING so a slow eval is visible in journald.
    """
    t0 = time.monotonic()
    try:
        if not settings.kb_eval_in_consolidation:
            counts["eval_status"] = "disabled"  # kill switch: skip the eval entirely
            return
        try:
            _run(counts)
        except Exception:  # noqa: BLE001 — the sentinel must never break consolidation
            logger.exception("search-quality sentinel errored; consolidation unaffected")
            counts["eval_status"] = "error"
    finally:
        secs = round(time.monotonic() - t0, 1)
        counts.setdefault("step_secs", {})["eval_sentinel"] = secs
        if secs >= 60:
            logger.warning("consolidation step 'eval_sentinel' took %.0fs", secs)


def _run(counts: dict[str, Any]) -> None:
    golden = eval_harness.load_golden()
    if not golden:
        counts["eval_status"] = "skipped"  # golden set missing/empty
        logger.info("search-quality sentinel: golden set missing/empty — eval skipped")
        return
    try:  # arm the per-case rerank tally for the replay below (review #6)
        from kbcore import rerank as _rerank_arm

        _rerank_arm.reset_tally()
    except Exception:  # noqa: BLE001 — observability may never fail the eval
        pass
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
    # Review #5 ops-4 / review #6 instrument fix / review #8 H7a: which rerank
    # path scored tonight's replay, PER CASE — kbcore.rerank accumulates a
    # thread-local {active, fallback} tally across the harness's searches,
    # 'off' is derived (see rerank.outcome_tally). ``eval_rerank_outcome``
    # keeps its key and value domain and rolls up fallback > active > off:
    # 'fallback' if ANY case was measured on the fused order, else 'active' if
    # the sidecar scored anything, else 'off' (NO case reranked). The review #6
    # order (fallback > off > active) read 'off' on every healthy night because
    # one golden case structurally has <2 fused candidates — the scalar hid
    # the sidecar's activity. The tally is an ADDED key the digest renders
    # VERBATIM. Snapshot here, before the instruments replay more searches.
    try:
        from kbcore import rerank as _rerank

        tally = _rerank.outcome_tally(len(golden))
        counts["eval_rerank_tally"] = tally
        counts["eval_rerank_outcome"] = next(
            (o for o in ("fallback", "active", "off") if tally.get(o)), "off"
        )
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

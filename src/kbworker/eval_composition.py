"""kbworker.eval_composition — the top-5 trust/type composition instrument.

Split from :mod:`kbworker.eval_sentinel` (mechanical move, 2026-09-03, review
#8 H0): the sentinel sat at 298/300 and the E1 satisfier-type telemetry (H5)
lands HERE. ``eval_sentinel`` re-imports every name (the established seam, as
with eval_regression), so ``eval_sentinel._trust_composition`` / ``TRUST_K``
keep resolving.

ONE seam read: the suite patches ``eval_sentinel.search`` (to prove a broken
instrument costs one status, never the metric), and a bare-name ``search`` here
would resolve to kbcore's real function, so the replay calls
``_seam().search`` — the sentinel's binding — at call time (lazy: import
cycle). Unpatched that IS ``kbcore.search.search``. Same logger name as the
parent on purpose.

E1 SATISFIER TELEMETRY (review #8 H5, KB 31857). The same read-only pass now
records WHICH hit satisfied each golden case:

* ``eval_satisfier_types`` — case counts by the first satisfier's class,
  ``{summary, verified, unverified, unsatisfied}`` (a summary-typed hit is
  'summary' whatever its trust; 'unsatisfied' = no top-5 hit matched);
* ``eval_summary_only_cases`` — cases whose EVERY matching top-5 hit is a
  consolidation summary. ANY-of ``expect_substr`` lets one summary satisfy
  several cases, so this is the count a summary retirement would turn red —
  the number the ~Sep 20 summaries verdict reads, with the share trend;
* ``eval_satisfier_ids`` — the distinct first-satisfier ids (E1's per-case
  diff names the item behind a single-quantum MRR move).

Matching follows the harness's own semantics — ``expect_item_id`` chain-resolved
and owning the case, else ANY-of ``expect_substr`` over title+snippet+body — but
against THIS pass's PRODUCTION-DEFAULT replay (no per-case kwargs), so a
kwargs-pinned case is scored on what an ordinary agent query surfaces. Its own
guard: a broken rider costs ``eval_satisfier_status='error'``, never the
composition shares.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import ModuleType
from typing import Any

from kbcore import eval_harness
from kbcore.items import get_item
from kbworker import eval_trust

logger = logging.getLogger("kbworker.eval_sentinel")
TRUST_K = 5  # top-K window the trust composition is measured over
SATISFIER_CLASSES = ("summary", "verified", "unverified", "unsatisfied")


def _seam() -> ModuleType:
    """``kbworker.eval_sentinel`` — the patched test surface (lazy: import cycle)."""
    from kbworker import eval_sentinel

    return eval_sentinel


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
    Review #8 H5 adds the satisfier telemetry (module docstring) on the same hits.
    """
    unverified = 0
    summary = 0
    total = 0
    top5_ids: set[int] = set()
    per_case: list[tuple[dict, list[Any]]] = []
    for case in golden:
        query = case.get("q")
        if not query:
            continue
        hits = list(_seam().search(query, k=TRUST_K, tool=None))
        per_case.append((case, hits))
        for hit in hits:
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
        # The lift NUMERATOR, persisted explicitly (review #6): every surface
        # rendering the lift renders this count and the corpus fraction beside
        # it, so denominator decay can never masquerade as a ranking move.
        "eval_top5_verified": total - unverified,
        "eval_top5_summary_share": summary_share,
        "eval_top5_summary": summary,
        "eval_top5_results": total,
        "eval_top5_item_ids": sorted(top5_ids),
    }
    try:  # the satisfier rider has its OWN guard: it may never cost the shares
        result.update(_satisfier_telemetry(per_case))
        result["eval_satisfier_status"] = "ok"
    except Exception:  # noqa: BLE001 — report-only rider
        logger.exception("satisfier telemetry failed; composition shares unaffected")
        result["eval_satisfier_status"] = "error"
    result.update(eval_trust.verified_lift(unverified_share, len(golden)))
    return result


# --- E1 rider: which hit satisfied each case ---------------------------------


def _satisfier_class(hit: Any) -> str:
    """'summary' (type) beats trust; else 'verified' / 'unverified' by trust."""
    if getattr(hit, "type", None) == "summary":
        return "summary"
    return "verified" if getattr(hit, "trust", None) == "verified" else "unverified"


def _body(item_id: Any) -> str:
    """The hit's FULL body for substring matching (the harness matches bodies too).

    ``""`` when the row cannot be read — a stubbed hit in a hermetic test, or a
    row gone between the search and the read — so matching degrades to
    title+snippet instead of erroring the whole rider.
    """
    try:
        return get_item(int(item_id), with_relations=False).body or ""
    except Exception:  # noqa: BLE001 — degrade to title+snippet, never raise
        return ""


def _matching_hits(case: Mapping[str, Any], hits: list[Any]) -> list[Any]:
    """The top-K hits that satisfy the case's POSITIVE criterion, harness semantics.

    ``expect_item_id`` owns the case (chain-resolved exactly like the harness,
    so a doc-refresh head still counts); otherwise ANY-of ``expect_substr``
    over title + snippet + full body, case-insensitive. Order preserved, so
    ``[0]`` is the rank-first satisfier.
    """
    if "expect_item_id" in case:
        want = eval_harness._chain_head(int(case["expect_item_id"]))
        return [h for h in hits if h.item_id == want]
    expected = [str(s).lower() for s in (case.get("expect_substr") or [])]
    if not expected:
        return []
    out: list[Any] = []
    for hit in hits:
        haystack = f"{hit.title}\n{hit.snippet}\n{_body(hit.item_id)}".lower()
        if any(sub in haystack for sub in expected):
            out.append(hit)
    return out


def _satisfier_telemetry(per_case: list[tuple[dict, list[Any]]]) -> dict[str, Any]:
    """Fold each case's satisfiers into the three E1 keys (module docstring).

    Forbid-only cases have no positive criterion and are skipped — they can
    never be 'satisfied', and counting them as unsatisfied would fake a miss.
    """
    types: dict[str, int] = dict.fromkeys(SATISFIER_CLASSES, 0)
    summary_only = 0
    ids: set[int] = set()
    for case, hits in per_case:
        if not any(k in case for k in ("expect_item_id", "expect_substr")):
            continue
        matching = _matching_hits(case, hits)
        if not matching:
            types["unsatisfied"] += 1
            continue
        types[_satisfier_class(matching[0])] += 1
        ids.add(int(matching[0].item_id))
        if all(getattr(h, "type", None) == "summary" for h in matching):
            summary_only += 1
    logger.info("golden satisfiers by class: %s; summary-only cases: %d", types, summary_only)
    return {
        "eval_satisfier_types": types,
        "eval_summary_only_cases": summary_only,
        "eval_satisfier_ids": sorted(ids),
    }

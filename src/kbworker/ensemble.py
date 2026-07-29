"""kbworker.ensemble — pipeline v2 ensemble/converge/verify stage (WP-B2).

Measured design (MODEL_RESEARCH_2026-07.md addendum 2, owner-approved
2026-07-18). The evidence that fixed the shape of this module:

- Models find DIFFERENT knowledge: only 29% of cross-model clusters were
  multi-model on the bench, so the correct combinator is UNION + per-item
  verification. Consensus voting is explicitly wrong here — it would discard
  most of the knowledge (12/24 clusters had no primary-model member at all).
- Ensemble(qwen3:14b + granite4.1:8b + gemma3:12b) + quote repair yielded 21
  quote-anchored knowledge units vs 5 under the single-model policy (~4x).
- Judge quality is measured and asymmetric: the primary (qwen3:14b) as a
  FACTORED judge was perfect on probes (approved the real items, caught 6/6
  fabrications); gemma3:12b FAILED both directions (rejected 4 real, approved
  5/6 fakes) and is disqualified from the judge slot. The judge is therefore
  always the primary model.
- Survivor policy (e): a cluster representative survives iff it is quote-USABLE
  (grounded or repaired — the SEC-3 gate runs before any policy) AND (its
  source model is the primary, OR >=2 distinct models corroborate the cluster,
  OR the primary-model factored judge returns supported=true). Judge failures
  are fail-closed for non-primary singletons (counted, never ingested) and can
  never block primary-sourced or corroborated items — those never reach the
  judge.

GPU courtesy (Invariant 8): only the primary stays resident (settings-driven
keep_alive); every EXTRA extractor runs keep_alive=0. JUDGING is MODEL-MAJOR
(2026-07-29): with OLLAMA_MAX_LOADED_MODELS=1 the card holds one model, so the
judgements a run needs are collected first, GROUPED by judge model and executed
group by group — each judge model loads at most once per run instead of once per
alternation. This module COMPUTES and REPORTS only — it imports no DB machinery
and never writes. kbworker.sessions owns all ingest wiring, which is what makes
shadow mode safe by construction.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

from kbcore import embed, llm
from kbcore.config import settings
from kbcore.errors import ExtractionFailed, ExtractorUnavailable
from kbworker.sessions import MAX_GATE_REASONS, gate_quotes

logger = logging.getLogger("kbworker.ensemble")

# Shadow-mode accumulator: would-be survivors across a drain run, read into the
# nightly consolidation report (kbworker.consolidate resets it per run).
_shadow_survivors = 0


def mode() -> str:
    """Effective pipeline mode from settings: ``'off' | 'shadow' | 'live'``."""
    if not settings.extract_ensemble:
        return "off"
    return "shadow" if settings.kb_ensemble_shadow else "live"


def reset_shadow_counter() -> None:
    """Zero the shadow accumulator (start of a drain/consolidation run)."""
    global _shadow_survivors
    _shadow_survivors = 0


def shadow_survivor_total() -> int:
    """Would-be survivors accumulated since the last reset."""
    return _shadow_survivors


def record_shadow(report: dict[str, Any], v1_saved: int) -> None:
    """Log the one-line shadow report and accumulate the would-be survivor count."""
    global _shadow_survivors
    would = len(report["survivors"])
    _shadow_survivors += would
    logger.info(
        "ensemble shadow [%s]: candidates=%s clusters=%d corroborated=%d judged=%d "
        "judge_errors=%d would_save=%d v1_saved=%d",
        report["uri"],
        ",".join(f"{m}={n}" for m, n in report["candidates"].items()) or "-",
        report["clusters"],
        report["corroborated"],
        report["judged"],
        report["judge_errors"],
        would,
        v1_saved,
    )


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _extract_all(
    text: str,
    primary: str,
    extras: list[str],
    pre: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Run every configured extractor; per-model isolation for the extras.

    The primary uses the settings-driven keep_alive (resident by default) and a
    primary failure RAISES with the v1 taxonomy (ExtractorUnavailable /
    ExtractionFailed — the queue drain owns retry semantics). Extras run
    keep_alive=0 (Invariant 8: only the primary stays resident) and a failing
    extra logs + is skipped, never aborting the run.
    """
    if pre is not None:
        return dict(pre), []
    per_model = {primary: llm.extract_items(text, model=primary)}
    failed: list[str] = []
    for m in extras:
        try:
            per_model[m] = llm.extract_items(text, model=m, keep_alive=0)
        except (ExtractorUnavailable, ExtractionFailed) as e:
            logger.warning("ensemble extractor %s skipped: %s", m, e)
            failed.append(m)
    return per_model, failed


def _cluster(entries: list[dict[str, Any]], threshold: float) -> list[list[dict[str, Any]]]:
    """Greedy single-pass clustering of quote-usable items at cosine >= threshold.

    Embeds ``title: body`` on the DOCUMENT side (:func:`kbcore.embed.embed_texts`
    — never the query prefix; instruction-prefixed documents break the embedder
    contract). Each entry joins the best-matching existing cluster, scored
    against the cluster's SEED vector (its first member — deterministic), or
    starts a new cluster. Entries arrive primary-first, so primary items seed.
    """
    texts = [
        f"{(e['item'].get('title') or '').strip()}: {(e['item'].get('body') or '').strip()}"
        for e in entries
    ]
    vectors = embed.embed_texts(texts)
    clusters: list[dict[str, Any]] = []
    for entry, vec in zip(entries, vectors, strict=True):
        best: dict[str, Any] | None = None
        best_cos = 0.0
        for c in clusters:
            cos = _cosine(vec, c["vec"])
            if cos > best_cos:  # strict > : ties keep the FIRST cluster found
                best, best_cos = c, cos
        if best is not None and best_cos >= threshold:
            best["members"].append(entry)
        else:
            clusters.append({"vec": vec, "members": [entry]})
    return [c["members"] for c in clusters]


def _representative(members: list[dict[str, Any]], primary: str) -> dict[str, Any]:
    """Best member of a cluster: grounded > repaired, then the primary model's
    member, then the highest repair ratio (min is stable → insertion order
    breaks full ties, and entries were built primary-first)."""

    def key(e: dict[str, Any]) -> tuple[int, int, float]:
        repaired = e["repair"] is not None
        ratio = e["repair"]["ratio"] if repaired else 1.0
        return (1 if repaired else 0, 0 if e["model"] == primary else 1, -ratio)

    return min(members, key=key)


def prefetch_extractions(texts: list[str]) -> list[dict[str, list[dict[str, Any]]]]:
    """Run every configured extractor across a COHORT, model by model.

    Returns one ``{model: items}`` per input text, in order. The ordering is the
    entire point: looping model-major means each model is loaded once for the
    whole cohort, where the per-row path reloads all three for every row. On a
    card that holds one model at a time that is the dominant cost.

    A model that fails is recorded as absent for EVERY text in the cohort — the
    same "skip the extra, never abort the run" rule the per-row path uses. The
    primary is NOT special-cased here: a primary failure surfaces per text when
    run_ensemble finds it missing, so one bad cohort cannot mask it.
    """
    primary = settings.kb_extract_model
    extras = list(dict.fromkeys(m for m in settings.extract_ensemble if m != primary))
    ka = settings.kb_drain_phase_keep_alive
    out: list[dict[str, list[dict[str, Any]]]] = [{} for _ in texts]
    for model in [primary, *extras]:
        for i, text in enumerate(texts):
            try:
                out[i][model] = llm.extract_items(text, model=model, keep_alive=ka)
            except (ExtractorUnavailable, ExtractionFailed) as e:
                logger.warning("cohort extractor %s failed on item %d: %s", model, i, e)
    return out


@dataclass
class _Judgement:
    """One DEFERRED judge call: which cluster plan entry it decides, by which
    model, and under which failure semantics.

    ``fail_closed`` is the whole reason the two branches stay distinguishable
    after the judge loop was reordered: a non-primary singleton judged by the
    primary is fail-closed (an unsupported verdict OR a judge exception rejects
    the representative), while a primary singleton sent to the SECONDARY
    cross-family judge is demote-to-review (it ingests either way, only the
    ``verified_by`` label changes).
    """

    entry: dict[str, Any]
    judge_model: str
    fail_closed: bool


def _group_keep_alive(model: str, primary: str) -> int:
    """keep_alive for one judge GROUP — the model must survive the whole group.

    The phase keep-alive (`kb_drain_phase_keep_alive`, the knob
    :func:`prefetch_extractions` already uses for its cohort phases) is the
    FLOOR: the primary keeps its settings-driven residency when that is longer,
    so a group never reloads mid-way. What this is NOT is the old
    ``keep_alive=0`` on the secondary judge: that unloaded the judge after every
    single call, so with one model slot each judgement paid a full cold load.

    A NEGATIVE setting ("resident forever") is clamped to the phase value rather
    than propagated. Under ``OLLAMA_MAX_LOADED_MODELS=1`` an unbounded pin holds
    the only slot permanently: the next phase's model — the other judge, the next
    cohort's extractor, or the embedder that runs straight after — waits for a
    slot that can never free and dies on the client timeout, which reads as a
    network fault rather than a residency bug. Measured 2026-07-29: that pin
    stalled the whole drain, while a cold load costs 4-25s. Lengthening residency
    is therefore NOT universally safe on a one-slot card, and this helper must
    never be the thing that reintroduces the pin (Invariant 8, corrected).
    """
    resident = settings.kb_llm_keep_alive
    phase = settings.kb_drain_phase_keep_alive
    if model == primary and resident > phase:
        return resident
    return phase


def _judge_by_model(pending: list[_Judgement], transcript: str, primary: str) -> int:
    """Run every pending judgement MODEL-MAJOR, stamping verdicts onto the plan.

    Groups the judgements by judge model and executes the groups sequentially,
    issuing a group's `llm.verify_item` calls back to back so the model loads at
    most once. Mutates each :class:`_Judgement`'s ``entry`` in place
    (``verified_by`` / ``survives``) and returns the judge-error count; the
    caller derives ``rejected_reps`` from the plan.
    """
    groups: dict[str, list[_Judgement]] = {}
    for p in pending:
        groups.setdefault(p.judge_model, []).append(p)

    errors = 0
    # Group order (deterministic): every non-primary judge first, alphabetically,
    # and the PRIMARY model's group LAST. The primary is also the extractor
    # primary and the only model Invariant 8 keeps resident, so ending on it
    # leaves the right model on the card, and a run whose only judgements are
    # primary-judged (no secondary judge configured) never swaps at all.
    for model in sorted(groups, key=lambda m: (m == primary, m)):
        batch = groups[model]
        keep_alive = _group_keep_alive(model, primary)
        logger.info(
            "ensemble judge group: model=%s judgements=%d keep_alive=%s",
            model,
            len(batch),
            keep_alive,
        )
        for p in batch:
            item = p.entry["rep"]["item"]
            try:
                verdict = llm.verify_item(
                    str(item.get("title") or ""),
                    str(item.get("body") or ""),
                    transcript,
                    judge_model=model,
                    keep_alive=keep_alive,
                )
            except (ExtractorUnavailable, ExtractionFailed) as e:
                errors += 1
                if p.fail_closed:
                    # Fail-closed: a judge failure never ingests the singleton
                    # but never aborts the run either.
                    logger.warning("ensemble judge failed for %r: %s", item.get("title"), e)
                    p.entry["survives"] = False
                else:
                    # Item still survives (primary-sourced).
                    logger.warning("secondary judge failed for %r: %s", item.get("title"), e)
                continue
            supported = bool(verdict.get("supported"))
            if p.fail_closed:
                if supported:
                    p.entry["verified_by"] = "primary-judge"
                else:
                    p.entry["survives"] = False
            else:
                p.entry["verified_by"] = (
                    "secondary-judge" if supported else "secondary-judge-failed"
                )
    return errors


def run_ensemble(
    text: str,
    project: str,
    uri: str,
    *,
    code_ref: dict[str, str] | None = None,
    branch: str | None = None,
    extra_detail: dict[str, Any] | None = None,
    pre_extracted: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Extract with every configured model, converge, verify — REPORT only.

    Orchestrates the measured pipeline: per-model extraction (primary resident,
    extras keep_alive=0, per-model isolation) → the EXACT v1 quote gate
    (:func:`kbworker.sessions.gate_quotes`) → greedy cosine clustering of the
    quote-usable UNION → one representative per cluster → survivor policy (e),
    whose judge calls are collected first and executed GROUPED BY JUDGE MODEL so
    no run alternates between models on a one-slot card.
    Writes NOTHING: the caller (kbworker.sessions) decides shadow vs live
    ingestion from the returned report, which carries the counts, the survivor
    entries (each with its ``extraction_pipeline`` meta-enrichment and B1 repair
    record), and the pass-through ingest context (project/uri/code_ref/branch/
    extra_detail) so both call sites share one signature.
    """
    primary = settings.kb_extract_model
    extras = list(dict.fromkeys(m for m in settings.extract_ensemble if m != primary))
    min_ratio = settings.kb_quote_repair_min_ratio

    # pre_extracted lets the drain run each model ONCE across a cohort of jobs
    # (prefetch_extractions) instead of once per job. With one model slot on the
    # card every switch is a full load, so that is the difference between ~3
    # extractor loads per row and ~3 per cohort. The rest of the pipeline is
    # unchanged and still runs per job.
    per_model, failed = _extract_all(text, primary, extras, pre=pre_extracted)

    # Quote-gate per model; the union of USABLE items feeds clustering,
    # primary first so its items seed clusters and win ties.
    entries: list[dict[str, Any]] = []
    candidates: dict[str, int] = {}
    usable: dict[str, int] = {}
    gate_rejections: list[dict[str, str]] = []
    unanchored = 0
    for m in [primary, *extras]:
        if m not in per_model:
            continue  # failed extra — already logged and counted
        # Same gate as v1, including the FIX-1 modality check; its per-item
        # reasons ride the report so the ingest side can put them in the review
        # detail (audit 2026-07-26).
        gated, dropped, rejections = gate_quotes(per_model[m], text, min_ratio)
        candidates[m] = len(per_model[m])
        usable[m] = len(gated)
        unanchored += dropped
        gate_rejections.extend({**r, "model": m} for r in rejections)
        entries.extend({"item": it, "repair": repair, "model": m} for it, repair in gated)

    threshold = settings.kb_ensemble_cluster_threshold
    clusters = _cluster(entries, threshold) if entries else []

    # PASS ONE — decide each cluster's fate WITHOUT calling a judge; judgements
    # are only recorded so they can be batched by model (see _judge_by_model).
    cluster_detail: list[dict[str, Any]] = []
    plan: list[dict[str, Any]] = []
    pending: list[_Judgement] = []
    corroborated = 0
    for members in clusters:
        found_by = list(dict.fromkeys(e["model"] for e in members))
        cluster_detail.append({"models": found_by, "size": len(members)})
        if len(found_by) >= 2:
            corroborated += 1
        rep = _representative(members, primary)
        entry: dict[str, Any] = {
            "rep": rep,
            "found_by": found_by,
            "verified_by": "",
            "survives": True,
        }
        plan.append(entry)
        # Survivor policy (e), checked in order; only non-primary singletons
        # ever reach the primary judge. Primary singletons additionally get a
        # SECONDARY cross-family judge when configured (granite4.1:8b passed
        # the probe bench 2026-07-18: 7/7 reals approved, 6/6 fabricated
        # probes caught, ~2.7s/item) — but demote-to-review semantics apply:
        # a secondary-judge fail still ingests (v1 would have kept the item)
        # with verified_by='secondary-judge-failed', a visible flag for triage,
        # never a silent drop.
        if rep["model"] == primary:
            # Corroborated primary reps keep the 'primary-source' label (the
            # pinned WP-B2 contract); corroboration merely SKIPS the judge.
            # This also stands if the secondary judge ERRORS out below.
            entry["verified_by"] = "primary-source"
            if len(found_by) < 2 and settings.kb_ensemble_secondary_judge:
                pending.append(
                    _Judgement(entry, settings.kb_ensemble_secondary_judge, fail_closed=False)
                )
        elif len(found_by) >= 2:
            entry["verified_by"] = "corroboration"
        else:
            pending.append(_Judgement(entry, primary, fail_closed=True))

    # Judge model-major: one load per model per run, not one per alternation.
    judged = len(pending)
    judge_errors = _judge_by_model(pending, text, primary)

    # PASS TWO — apply the verdicts. A rejected representative is exactly a
    # fail-closed judgement that came back unsupported or errored.
    survivors: list[dict[str, Any]] = []
    rejected_reps = 0
    for entry in plan:
        if not entry["survives"]:
            rejected_reps += 1
            continue
        rep = entry["rep"]
        found_by = entry["found_by"]
        survivors.append(
            {
                "item": rep["item"],
                "repair": rep["repair"],
                "model": rep["model"],
                "extraction_pipeline": {
                    "found_by": found_by,
                    "corroboration": len(found_by),
                    "quote_state": "grounded" if rep["repair"] is None else "repaired",
                    "verified_by": entry["verified_by"],
                },
            }
        )

    return {
        "project": project,
        "uri": uri,
        "code_ref": code_ref,
        "branch": branch,
        "extra_detail": extra_detail,
        "primary": primary,
        "extractor_errors": failed,
        "candidates": candidates,
        "usable": usable,
        "unanchored": unanchored,
        "gate_rejections": gate_rejections[:MAX_GATE_REASONS],
        "clusters": len(clusters),
        "cluster_detail": cluster_detail,
        "corroborated": corroborated,
        "judged": judged,
        "judge_errors": judge_errors,
        "rejected_reps": rejected_reps,
        "survivors": survivors,
    }

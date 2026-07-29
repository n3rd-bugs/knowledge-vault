"""kbworker.judging — COHORT-WIDE, model-major factored judging (2026-07-29).

Split out of :mod:`kbworker.ensemble` (which was already past the ~300-line
guidance) when judging was hoisted from RUN scope to COHORT scope.

Measured cause (live drain, 2026-07-29): the ollama journal showed 50 model-load
events for 37 chat calls — MORE LOADS THAN CALLS — alternating
qwen3 -> granite -> qwen3 -> granite. With ``OLLAMA_MAX_LOADED_MODELS=1`` the
card holds ONE model, so every alternation is a full cold load (granite 4s,
gemma 17s, qwen3 25s) and ~90% of drain wall clock was loading, not inference.
Extraction was already cohort-wide (``ensemble.prefetch_extractions``); judging
was model-major only WITHIN one job, so a cohort of 8 jobs still alternated ~16
times.

:func:`judge_cohort` therefore collects the judgements of EVERY job in a cohort
into ONE group map and runs the groups back to back: every secondary-judge call
for the whole cohort, then every primary-judge call for the whole cohort. Loads
per cohort go from ``2 * jobs`` to ``2`` (one per distinct judge model).

FAILURE MODEL — split by exception class, the drain's own taxonomy:

- :class:`ExtractionFailed` (content-deterministic: unparseable verdict, non-bool
  ``supported``) stays STRICTLY per item, exactly as the per-job loop did.
  Re-running that item fails identically, and no neighbour — in this job or any
  other — is affected.
- :class:`ExtractorUnavailable` (connectivity: the judge model is gone) is NOT
  about the item: every remaining judgement in that group would fail the same
  way. The rest of the GROUP is abandoned into each plan's ``undecided``, the
  next group still runs (a dead secondary judge must not cost the primary judge
  its group), and nothing propagates out of :func:`judge_cohort`.

Verdicts stamped before a model died STAND — entries are mutated in place, so
jobs judged earlier in the group are complete and correct.
:func:`kbworker.ensemble.finish_ensemble` then decides each job's fate from its
own ``undecided`` list: demote-to-review leftovers proceed (a secondary-judge
failure NEVER drops a primary-sourced item — the pinned WP-B2 contract), while
any abandoned FAIL-CLOSED judgement fails the job so the drain DEFERS it whole.
Deferring satisfies fail-closed strictly harder than dropping does, and honours
consolidate's rule that an outage never burns a row's retry budget.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from kbcore import llm
from kbcore.config import settings
from kbcore.errors import ExtractionFailed, ExtractorUnavailable

# Deliberately the ENSEMBLE logger: the judge-group line is the ensemble stage's
# observability contract (journald filters and tests select that name). Moving
# the code to a new module must not move the log.
logger = logging.getLogger("kbworker.ensemble")


@dataclass
class Judgement:
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


@dataclass
class EnsemblePlan:
    """One job's pipeline state, paused exactly at the judge boundary.

    Everything before the judge (extraction, the quote gate, clustering, PASS
    ONE) is already done and lives in ``partial`` (the 15 pre-judge report keys)
    and ``plan`` (the per-cluster entries the judge mutates in place). ``text``
    is pinned because the judge needs the transcript and the ingest side stores
    it as the item's ``raw``.

    Deliberately holds NO embedding vectors: :func:`kbworker.ensemble._cluster`
    keeps them local, so a cohort of paused plans retains only transcripts plus
    entry dicts.
    """

    text: str
    primary: str
    plan: list[dict[str, Any]]
    pending: list[Judgement]
    partial: dict[str, Any]
    judge_errors: int = 0
    undecided: list[Judgement] = field(default_factory=list)
    outage: str | None = None


def _group_keep_alive(model: str, primary: str) -> int:
    """keep_alive for one judge GROUP — the model must survive the whole group.

    The phase keep-alive (`kb_drain_phase_keep_alive`, the knob
    :func:`kbworker.ensemble.prefetch_extractions` already uses for its cohort
    phases) is the FLOOR: the primary keeps its settings-driven residency when
    that is longer, so a group never reloads mid-way. What this is NOT is the old
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

    A COHORT group is up to N times longer in wall clock than a single job's, and
    that is deliberately NOT a reason to lengthen it: Ollama resets the timer on
    every request, so at ~2.7s/judgement no inter-call gap comes close.
    """
    resident = settings.kb_llm_keep_alive
    phase = settings.kb_drain_phase_keep_alive
    if model == primary and resident > phase:
        return resident
    return phase


def _run_group(
    model: str,
    batch: list[tuple[int, Judgement]],
    plans: Sequence[EnsemblePlan],
    keep_alive: int,
) -> bool:
    """Execute one judge model's judgements back to back. False iff it went dark.

    Mutates each judgement's ``entry`` in place (``verified_by`` / ``survives``)
    and ticks ``judge_errors`` on the ORIGINATING plan — never a shared counter,
    or job A's judge error would surface in job B's shadow line and review
    detail.
    """
    for pos, (idx, judgement) in enumerate(batch):
        plan = plans[idx]
        item = judgement.entry["rep"]["item"]
        try:
            verdict = llm.verify_item(
                str(item.get("title") or ""),
                str(item.get("body") or ""),
                plan.text,
                judge_model=model,
                keep_alive=keep_alive,
            )
        except ExtractionFailed as e:
            # CONTENT-deterministic: this item's verdict is unparseable, which
            # says nothing about the model's health. Strictly per item, exactly
            # as the per-job loop behaved.
            plan.judge_errors += 1
            if judgement.fail_closed:
                logger.warning("ensemble judge failed for %r: %s", item.get("title"), e)
                judgement.entry["survives"] = False
            else:
                logger.warning("secondary judge failed for %r: %s", item.get("title"), e)
            continue
        except ExtractorUnavailable as e:
            # CONNECTIVITY: the model is gone, so every remaining judgement in
            # this group would fail identically. Abandon the rest of the GROUP
            # (not the cohort — the other judge may still be alive) and let each
            # plan decide its own fate from its undecided list.
            remaining = batch[pos:]
            logger.warning(
                "judge model %s unreachable (%s); abandoning %d judgement(s) across %d plan(s)",
                model,
                e,
                len(remaining),
                len({i for i, _ in remaining}),
            )
            for r_idx, r_judgement in remaining:
                stranded = plans[r_idx]
                stranded.judge_errors += 1
                stranded.undecided.append(r_judgement)
                if stranded.outage is None:
                    stranded.outage = f"judge {model} unreachable: {e}"
            return False
        supported = bool(verdict.get("supported"))
        if judgement.fail_closed:
            if supported:
                judgement.entry["verified_by"] = "primary-judge"
            else:
                judgement.entry["survives"] = False
        else:
            judgement.entry["verified_by"] = (
                "secondary-judge" if supported else "secondary-judge-failed"
            )
    return True


def judge_cohort(plans: Sequence[EnsemblePlan]) -> list[int]:
    """Run EVERY plan's pending judgements MODEL-MAJOR across the whole cohort.

    One group map for all jobs: each distinct judge model loads at most ONCE per
    cohort, not once per job. Returns the per-plan judge-error counts (in
    ``plans`` order) for logging; the authoritative counters live on the plans.
    """
    if not plans:
        return []
    # Every plan in a cohort reads the same settings, so the first plan's primary
    # is the cohort's primary.
    primary = plans[0].primary

    # Plan-major, then cluster order within a plan: deterministic, and it keeps
    # "cluster order is preserved WITHIN a judge group" true for the 1-plan case
    # that run_ensemble is.
    groups: dict[str, list[tuple[int, Judgement]]] = {}
    for idx, plan in enumerate(plans):
        for judgement in plan.pending:
            groups.setdefault(judgement.judge_model, []).append((idx, judgement))

    aborted = 0
    # Group order (deterministic): every non-primary judge first, alphabetically,
    # and the PRIMARY model's group LAST. The primary is also the extractor
    # primary and the only model Invariant 8 keeps resident, so ending on it
    # leaves the right model on the card, and a run whose only judgements are
    # primary-judged (no secondary judge configured) never swaps at all.
    for model in sorted(groups, key=lambda m: (m == primary, m)):
        batch = groups[model]
        keep_alive = _group_keep_alive(model, primary)
        # ONE line per group, carrying the cohort size: "did judging actually
        # batch?" must be answerable from the journal, not from archaeology.
        logger.info(
            "ensemble judge group: model=%s judgements=%d plans=%d keep_alive=%s",
            model,
            len(batch),
            len({i for i, _ in batch}),
            keep_alive,
        )
        if not _run_group(model, batch, plans, keep_alive):
            aborted += 1
    if aborted and aborted == len(groups):
        # Cohort-level breaker: no second pass, no retry storm. Each plan's
        # undecided list already carries the right per-job consequence.
        logger.error(
            "every judge group (%d) went dark this cohort; %d plan(s) will defer or "
            "proceed on their undecided policy",
            aborted,
            len(plans),
        )
    return [p.judge_errors for p in plans]

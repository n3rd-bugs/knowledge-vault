"""kbworker.drainloop — Step 0: the refill loop and the run-level drain step.

Split from :mod:`kbworker.consolidate` (mechanical move, 2026-09-03, review #8
H0): that file sat at its size-ratchet ceiling (783 = 758 + 25) and the honest
``drained_deferred`` count (H6) has to land in THIS loop. ``consolidate``
re-imports every name below — it is the established seam every caller and test
resolves through (same shape as kbworker.cohort / tail_ops / powerguard), and
the shared dicts (``_COHORT_STATS``, ``_STOP_LOG``) stay one object each,
mutated in place, never rebound.

THE SEAM READS. The suite rebinds ``consolidate.should_stop``,
``consolidate.QUEUE_BATCH``, ``consolidate.MAX_REFILLS``,
``consolidate._cohort_prepare`` and ``consolidate._drain_ingest_queue`` and
then drives ``consolidate._drain_ingest_queue()`` / ``_drain_step()`` expecting
the rebinding to bite. A bare-name global in THIS module would resolve here,
not there, so exactly those five names are read back through :func:`_seam` at
call time (lazy import: cycle) — the ``tail_steps._note_stop`` pattern. With
nothing patched ``consolidate.<name> is drainloop.<name>``, so the behaviour is
the pre-split behaviour, and the bodies are otherwise byte-identical to the
moved code. Everything else is imported from its home module, which is where
the tests patch it (as a module attribute). Same logger name as the parent on
purpose — journald grep recipes depend on it.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

from kbcore import drainsched, ingest_queue
from kbcore.config import settings
from kbworker import allocator, ensemble
from kbworker.cohort import (
    _COHORT_STATS,
    _drain_job,
    record_deferral_pressure,
    reset_cohort_stats,
)
from kbworker.prep import _assemble_jobs, _batch_jobs
from kbworker.tail_ops import StopCheck, _note_stop

logger = logging.getLogger("kbworker.consolidate")

# MEANING CHANGED 2026-08-01: this is the ceiling on ingest_queue ROWS one REFILL
# may materialise — a memory bound — and no longer the budget for a whole run.
# It was both, and the two units are not the same thing: a run stopped after 200
# ROWS, but the work is measured in JOBS (a coalesced session of 350 rows is one
# group, a one-row note is also one group), so a night of small submissions
# stopped at 200 rows with hours of window left and a night of one big session
# spent the whole budget on it. The drain now refills until the queue is empty or
# the stand-down guard fires; this only bounds how much is read per refill.
QUEUE_BATCH = 200

# Backstop on the refill loop. Each refill is bounded, but the SUPPLY is not:
# clients can enqueue while the drain runs, and a single-row submission is
# eligible immediately (no quiet period), so "refill until pending is empty"
# could in principle never terminate. The real bound is the deadline/battery
# guard; this exists so a run without one (an owner-requested `main(stop=None)`)
# still ends. At kb_drain_cohort=30 this is 6,000 groups — orders of magnitude
# past what a night's GPU window can extract, so hitting it is a loud anomaly.
MAX_REFILLS = 200


def _seam() -> ModuleType:
    """``kbworker.consolidate`` — the patched test surface (lazy: import cycle)."""
    from kbworker import consolidate

    return consolidate


def _drain_ingest_queue() -> tuple[int, int]:
    """Step 0: extract queued remote submissions — the ONLY place the 14B runs on
    remote content (Invariant 8). Per-group isolation: one bad group logs + continues.

    A REFILL LOOP (2026-08-01), not one poll. The old shape asked
    ``pending_groups`` once for :data:`QUEUE_BATCH` ROWS and processed whatever
    that returned, which conflated two units: the budget was rows, the work is
    jobs. A night of small submissions therefore stopped after 200 one-row groups
    with most of its window unused, while a night holding one 350-row coalesced
    session spent the entire budget on that single session. Each iteration now
    asks for enough groups to fill ONE cohort, processes them through the
    unchanged cohort machinery, and asks again — until the queue is drained, the
    stand-down guard fires, or :data:`MAX_REFILLS` trips.

    PROCESSED GROUPS ARE EXCLUDED FOR THE REST OF THE RUN, and this is load
    bearing rather than an optimisation. A group that DEFERS (extractor/embedder
    outage) is left 'pending' on purpose, and a content failure under the attempts
    cap is left retryable-'failed' — both are still drainable a millisecond
    later, so a naive refill would re-fetch the group it just failed and retry it
    until the deferral/attempts cap retired it, collapsing a policy of "one
    attempt per NIGHT" into "N attempts per run". The keys are tracked here, for
    this run only, and passed to ``pending_groups`` as ``exclude``.

    THREE PASSES PER COHORT (2026-07-29), then ingest job by job. Batching is per
    cohort and never per run: the crash window stays one cohort wide (the same
    exposure the extraction prefetch already had) and only ``kb_drain_cohort``
    plans — each pinning its transcript — are live at once. Ingest and
    ``mark_group_extracted`` deliberately stay per job: batching those would
    widen the pre-existing "written but still pending" window for real.

    The stand-down guard is asked BETWEEN cohorts here — across refills too, so
    the exemption is for the run's FIRST cohort only, not for the first cohort of
    every refill; the check before that first one belongs to the run
    (:func:`_drain_step`), because "should this run start at all" is a question
    about the run, not about a batch. Stopping can land mid-GROUP now that a
    group spans several window jobs — its rows are simply still 'pending', so the
    next run re-extracts the group from its first window and the already-ingested
    items dedupe on (kind, uri, content_hash) (Invariant 3).

    Returns ``(rows_drained, items_saved)``.
    """
    cohort = max(1, int(settings.kb_drain_cohort))
    # Seeded where the dict lives (kbworker.cohort — mechanical move 2026-08-14);
    # every key is ADDED, never renamed, because the report is read night-over-night.
    reset_cohort_stats(cohort)
    live = ensemble.mode() == "live"

    drained = 0
    saved_total = 0
    reserve = allocator.TailReserve.for_run()  # window left for the LLM tail
    # The weekday drain budget's clock (drainsched). PER RUN, like every capacity
    # rail (the expiry, triage, auto-apply and contradiction caps): a resumed
    # fragment gets a fresh allotment — day-keyed caps are Wave I. ``started_at``
    # is its wall-clock twin for the honest deferred count (review #8 H6): a row
    # enqueued after it was not waiting when this run began, so it is not 'deferred'.
    run_t0 = time.monotonic()
    started_at = datetime.now(UTC)
    batches_done = 0  # across ALL refills: only the run's FIRST cohort is exempt
    processed: set[tuple[str, str]] = set()
    stopped = False
    for _refill in range(_seam().MAX_REFILLS):
        groups = ingest_queue.pending_groups(
            limit=_seam().QUEUE_BATCH, max_groups=cohort, exclude=processed
        )
        # Belt and braces for the exclusion: whatever the SQL returns, a group
        # this run has already touched is never processed twice, so the loop
        # always makes progress and therefore always terminates.
        groups = [g for g in groups if (g["project"], g["group_uri"]) not in processed]
        if not groups:
            break
        processed.update((g["project"], g["group_uri"]) for g in groups)
        _COHORT_STATS["cohort_refills"] += 1
        _COHORT_STATS["cohort_groups_considered"] += len(groups)
        _COHORT_STATS["cohort_eligible"] += len(groups)

        jobs = _assemble_jobs(groups)
        cohorting = cohort > 1 and live and bool(jobs)
        batches = _batch_jobs(jobs, cohort) if cohorting else ([jobs] if jobs else [])

        for n, batch in enumerate(batches):
            # Same exemption as the guard, for the same reason: the run's first
            # cohort is _drain_step's call, not a batch's.
            reason = (
                _seam().should_stop()
                or drainsched.drain_budget_reason(run_t0)
                or reserve.deny_reason(batches[n:])
                if batches_done
                else None
            )
            if reason:
                left = sum(len(b) for b in batches[n:])
                logger.warning(
                    "drain pausing (%s) after %d refill(s): %d cohort(s) / %d job(s) "
                    "deferred — rows stay 'pending' and the next eligible run resumes here",
                    reason,
                    _COHORT_STATS["cohort_refills"],
                    len(batches) - n,
                    left,
                )
                _COHORT_STATS["deadline_deferred_jobs"] = left  # never renamed (telemetry)
                # Review #6 contract, made honest by review #8 (H6): the still-pending
                # ROWS this run leaves behind — on EVERY stop reason (budget, deadline,
                # battery, tail-reserve), not only the budget, and only rows that
                # already existed when the run started, so a submission landing
                # mid-run is never reported as shed. The SAME number goes to the stop
                # log, so the pause marker's 'deferred' and the digest agree; the
                # jobs-unit telemetry above keeps its own key (never renamed).
                deferred = ingest_queue.queue_depth(before=started_at)
                _COHORT_STATS["drained_deferred"] = deferred
                _note_stop("queued_drained", reason, deferred)
                stopped = True
                break
            batches_done += 1
            t0 = time.monotonic()
            pre_by_key, plan_by_key = _seam()._cohort_prepare(batch) if cohorting else ({}, {})
            for job in batch:
                rows, saved = _drain_job(
                    job,
                    pre_extracted=pre_by_key.get(job["key"]),
                    planned=plan_by_key.get(job["key"]),
                )
                drained += rows
                saved_total += saved
            reserve.record(len(batch), time.monotonic() - t0)  # this run's s/job
        if stopped:
            break
    else:
        logger.warning(
            "drain hit the %d-refill backstop with work still pending — the queue is "
            "being filled at least as fast as it drains",
            _seam().MAX_REFILLS,
        )
    # AFTER the loop, so tonight's deferral bumps are counted: an impending
    # deferral-cap escalation must reach the digest (review #3 P1 rider).
    record_deferral_pressure()
    return drained, saved_total


def _drain_step(counts: dict[str, Any], stop: StopCheck | None) -> int:
    """Step 0, with the stand-down guard asked BEFORE the first cohort.

    BOOT-CATCH-UP CONSEQUENCE, and it is the intended behaviour: the timer is
    ``Persistent=true``, so a box that boots at 14:00 after a power cut fires the
    missed 03:30 run immediately — already hours past the noon deadline. Asking
    the guard before the first cohort means such a run DEFERS the whole drain to
    the next nightly instead of putting the 14B on the GPU in the middle of the
    working day, which is exactly what the deadline exists to prevent. Nothing is
    lost: the rows stay 'pending' and ingest is idempotent (Invariant 3).
    """
    ensemble.reset_shadow_counter()  # per-run shadow accumulator (WP-B2)
    started_at = datetime.now(UTC)  # H6: the honest-count boundary for a pre-cohort stop
    reason = stop() if stop is not None else None
    if reason:
        # The long-lived watcher runs main() repeatedly in ONE process; leaving
        # the previous run's cohort telemetry standing would report work this
        # run never did.
        _COHORT_STATS.clear()
        # H6: a pre-first-cohort stand-down defers the WHOLE backlog. Record it
        # under the same contract key the mid-run stops write (rows that existed
        # at run start only) and hand the stop log the same number.
        deferred = ingest_queue.queue_depth(before=started_at)
        _COHORT_STATS["drained_deferred"] = deferred
        _note_stop("queued_drained", reason, deferred)
        return 0
    drained, saved = _seam()._drain_ingest_queue()
    counts["queued_items_saved"] = saved
    counts["ensemble_shadow_survivors"] = ensemble.shadow_survivor_total()
    return drained

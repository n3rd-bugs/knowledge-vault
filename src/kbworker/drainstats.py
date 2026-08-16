"""kbworker.drainstats — the drain's shared per-run telemetry dict + its seeding.

Split from :mod:`kbworker.cohort` (mechanical move, 2026-08-14): the deferral-cap
telemetry pushed that file past the 300-line ratchet, and the stats dict is its
own concern. ``cohort`` re-imports every name, which is the established seam
``consolidate`` and ``prep`` already use — the dict stays ONE object, mutated in
place on every side and never rebound. Same logger name as the drain on purpose:
journald grep recipes depend on it.
"""

from __future__ import annotations

import logging

from kbcore import ingest_queue
from kbworker import ensemble

logger = logging.getLogger("kbworker.consolidate")

# Cohort telemetry for the run report. The FIRST attempt at cohorting shipped
# with only a logger.info, and when throughput did not improve there was no way
# to answer "did it even run?" without grepping journald — the same blind spot
# that let 40% of embeddings run on CPU unnoticed. An optimisation you cannot
# observe is an optimisation you cannot trust.
_COHORT_STATS: dict[str, int] = {}


def reset_cohort_stats(cohort_size: int) -> None:
    """Zero the per-run cohort telemetry (start of a drain).

    Keys are ADDED, never renamed: the report is persisted in
    ``consolidation_requests.result`` and compared night over night.
    """
    _COHORT_STATS.clear()
    _COHORT_STATS.update(
        {
            "cohort_size": cohort_size,
            "cohort_batches": 0,
            "cohort_jobs": 0,
            "cohort_failures": 0,
            "cohort_plans": 0,
            "cohort_plan_failures": 0,
            "cohort_judge_batches": 0,
            "cohort_judge_failures": 0,
            "cohort_eligible": 0,
            "cohort_mode": ensemble.mode(),
            # Refill telemetry (added, never renamed): how many times the drain
            # went back for more, and how many groups it took up in total. A run
            # that did one refill on a deep queue is the old rows-vs-jobs bug
            # coming back, and there would otherwise be no way to see it.
            "cohort_refills": 0,
            "cohort_groups_considered": 0,
            # Per-window checkpoint telemetry (ADDED 2026-08-10, never renamed): windows
            # a resumed group skipped, groups resumed, failed writes (swallowed in cohort).
            "cohort_windows_resumed": 0,
            "cohort_groups_resumed": 0,
            "cohort_checkpoint_failures": 0,
            # Deferral-cap visibility (ADDED 2026-08-14, review #3 P1 rider):
            # rows mark_deferred ESCALATED to terminal 'failed' this run, and the
            # end-of-drain count of groups sitting near the cap. An escalation
            # used to be journald-only (retention <36h) — silent by the time an
            # owner looked.
            "deferral_escalations": 0,
            "groups_near_deferral_cap": 0,
            # Content-failure visibility (ADDED 2026-08-16, review #4 P0 rider):
            # groups that failed CONTENT-deterministically this run, and groups
            # every row of which hit the attempts cap doing so. The terminal
            # retirement gets the digest line KB 19666's watchlist promised;
            # sub-cap content failures stay report-only (they retry tomorrow).
            "content_failures": 0,
            "attempts_cap_retirements": 0,
        }
    )


def record_deferral_pressure() -> None:
    """End-of-drain: count groups near the deferral cap into the run report.

    AFTER the drain on purpose, so tonight's deferral bumps are included: a group
    arriving at DEFERRAL_CAP-2 tonight is ~2 nights from a terminal escalation
    the owner would otherwise first learn about from a queue that had quietly
    stopped retrying. Swallow-and-count like every piece of bookkeeping here —
    telemetry must never fail the drain it reports on.
    """
    try:
        _COHORT_STATS["groups_near_deferral_cap"] = ingest_queue.near_deferral_cap_groups()
    except Exception:  # noqa: BLE001 — never let bookkeeping fail the drain
        logger.warning("near-deferral-cap count failed", exc_info=True)

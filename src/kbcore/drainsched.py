"""kbcore.drainsched — WHEN the drain's wall-clock deadline applies.

The noon deadline exists to protect the daytime GPU for interactive work
(owner decision 2026-08-01). That constraint is a WEEKDAY constraint: on
weekends the box is free, so the deadline is suspended and the nightly runs
until the queue and the LLM tail are exhausted (owner decision 2026-08-02) —
which also makes weekends natural catch-up days for exactly the tail steps
(triage, contradictions, summaries) that a saturated weekday window starves.

The BATTERY guard is deliberately not routed through here: grid sags pause
the drain on Saturdays too. Only the clock is day-aware.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from kbcore.config import settings

logger = logging.getLogger("kbcore.drainsched")

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def effective_deadline(now: datetime | None = None) -> str:
    """``kb_drain_deadline`` when it applies TODAY, else ``""`` (no deadline).

    Judged in LOCAL time, like the deadline itself. An empty
    ``kb_drain_deadline_days`` means every day. A malformed day list fails
    TOWARD PROTECTION — the deadline applies — because the failure mode of the
    opposite choice is an unbounded GPU run on a workday, which is precisely
    what the deadline exists to prevent.
    """
    raw = (settings.kb_drain_deadline or "").strip()
    if not raw:
        return ""
    spec = (settings.kb_drain_deadline_days or "").strip().lower()
    if not spec:
        return raw
    days = {d.strip() for d in spec.split(",") if d.strip()}
    unknown = days - _DAYS.keys()
    if unknown:
        logger.warning(
            "KB_DRAIN_DEADLINE_DAYS has unknown day(s) %s — applying the deadline "
            "every day (failing toward GPU protection)",
            sorted(unknown),
        )
        return raw
    today = (now or datetime.now()).weekday()
    return raw if today in {_DAYS[d] for d in days} else ""


def is_unbounded_day(now: datetime | None = None) -> bool:
    """True when NO wall-clock deadline applies today, so the window is open-ended.

    The single definition of "the box is free tonight" — deliberately derived
    from :func:`effective_deadline` rather than from a second weekend notion, so
    a change to the day list (or turning the deadline off entirely) moves every
    dependent feature at once. Today's dependant is the nightly's weekend triage
    top-up (``kbworker.tail_steps.review_triaged_topup``), which spends the idle
    evening on the review backlog it could never reach on a workday.

    It says nothing about POWER: the battery guard is not routed through here
    (see the module docstring), so an unbounded day still stands down on a sag.
    """
    return not effective_deadline(now)


def triage_batch(now: datetime | None = None) -> int:
    """Tonight's review-triage batch — day-aware (review #6, 300 weekday / 500 unbounded).

    Unbounded days keep the full ``kb_review_triage_batch`` (backlog catch-up);
    deadline days bind at ``min(batch, kb_review_triage_batch_weekday)``. Honest
    justification: verdict consumption runs ~1.4x SLOWER than production once
    owner bulk-applies are counted, and against the ~16k awaiting-apply backlog a
    marginal weekday verdict carries months of latency — the last ~200 weekday
    rows are ~35 minutes of GPU with near-zero realized value, and dropping them
    removes the tension between the ~87.5-min batch and the 90-min triage share.
    ``min`` keeps BOTH kill switches: the batch knob stays the master (0 disables
    the step everywhere) and 0 in the weekday knob is a weekday-only kill.
    ``now`` exists so tests can day-pin the calendar-sensitive branch.
    """
    batch = int(settings.kb_review_triage_batch)
    if is_unbounded_day(now):
        return batch
    return min(batch, int(settings.kb_review_triage_batch_weekday))


# The drain-budget stand-down verdict — distinct from 'deadline'/'battery' and
# the allocator's 'tail-reserve', so a report can tell "the weekday budget was
# spent" apart from "the window ran out".
DRAIN_BUDGET_REASON = "drain-budget"


def drain_budget_reason(started_monotonic: float, now: datetime | None = None) -> str | None:
    """:data:`DRAIN_BUDGET_REASON` once a deadline day's drain spent its budget, else None.

    The tail reserve protects the LAST ``kb_tail_reserve_minutes`` of the window,
    but nothing bounded the drain's TOTAL appetite — a weekend-sized backlog
    landing on a weekday could still consume the whole night (review #6; legal
    now that the queue_groups split landed in Wave F).
    ``kb_drain_weekday_budget_minutes`` caps the drain's wall clock on days the
    deadline applies; unbounded days stay unbounded (the weekend IS the
    catch-up), and 0 disables the budget. Asked between cohorts by
    ``kbworker.consolidate._drain_ingest_queue`` against the drain's start on the
    monotonic clock; a budget stop defers rows exactly like the deadline does —
    still 'pending', resumed next run — and the shed volume rides the report as
    ``counts['drained_deferred']``. ``now`` is for day-pinned tests.
    """
    minutes = int(settings.kb_drain_weekday_budget_minutes)
    if minutes <= 0 or is_unbounded_day(now):
        return None
    if time.monotonic() - started_monotonic < minutes * 60.0:
        return None
    return DRAIN_BUDGET_REASON

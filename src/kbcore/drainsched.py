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

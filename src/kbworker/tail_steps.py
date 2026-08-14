"""kbworker.tail_steps — the nightly tail's P0-2 wiring (auto-apply + digest).

Split from :mod:`kbworker.allocator` when the day-aware deadline shim pushed
that file past the 300-line new-file ratchet — and rightly so: these are TAIL
STEP adapters (owner-decision plumbing), not reserve arithmetic. Both import
their kbcore implementations lazily so a missing module degrades to a no-op
rather than failing the run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from kbcore.config import settings

# THE ESTABLISHED SEAM (mechanical split, 2026-08-14): the digest moved to its
# own module when the deferral-cap lines reached this file's 300-line ratchet.
# Every caller and test keeps reaching it as ``tail_steps.<name>`` — same
# pattern as kbworker.consolidate's re-exports.
from kbworker.digest import (  # noqa: F401
    DIGEST_EXPIRY_HORIZON_DAYS,
    _expiring_soon,
    _stale_hook_projects,
    digest_lines,
    push_run_digest,
)

logger = logging.getLogger("kbworker.tail_steps")

StopCheck = Callable[[], "str | None"]

# Backstop on the top-up slice loop, in the spirit of consolidate.MAX_REFILLS. The real bound is
# the wall clock (every slice moves the clock toward the cutoff), so this only exists so a
# hypothetical slice that stores suggestions without ever shrinking the pending set cannot spin all
# night. At slice 200 and the measured 10.4 s/row this is ~29 h of work — reaching it is a loud
# anomaly.
TOPUP_MAX_SLICES = 50


# --------------------------------------------------------------- tail wiring
# The two P0-2 hooks. Their implementations live in kbcore modules this package
# does not own, so both are imported LAZILY inside the call: a missing module
# degrades to "the step did nothing", never to a failed nightly run.


def auto_apply_step(counts: dict[str, Any], stop: StopCheck | None = None) -> int:
    """Tail step ``auto_applied``: apply cooled reject/duplicate triage verdicts.

    Placed after triage and before contradictions: the freshest suggestions are
    applied the night AFTER they cool. Stop-gated like every tail step — a run
    standing down applies nothing, and the verdicts (stored, not recomputed) are
    applied next run. Returns rows applied; the per-reason breakdown rides into
    the report under ``auto_apply_counts``.
    """
    if stop is not None and (reason := stop()):
        _note_stop("auto_applied", reason, 0)
        return 0
    try:
        from kbcore import review_apply
    except ImportError:
        logger.warning("kbcore.review_apply is not installed — auto-apply skipped this run")
        return 0
    result = review_apply.auto_apply_rejects()
    counts["auto_apply_counts"] = dict(result)
    applied = int(result.get("applied_noise", 0)) + int(result.get("applied_duplicate", 0))
    logger.info("auto-apply: %d row(s) applied (%s)", applied, result)
    return applied


def quiet_expire_step(counts: dict[str, Any], stop: StopCheck | None = None) -> int:
    """Tail step ``review_expired``: close aged, never-recalled, TRIAGED rows.

    STOP-GATED like every other tail step (owner amendment 2026-08-08). It was
    the one automation that ran unconditionally — including in a zero-work
    boot-catch-up run that stood down before triage — so rows could be closed on
    a night when nothing was allowed to EXAMINE them. Standing down costs
    nothing: the rows stay open and the next run takes them, oldest first.

    Returns the rows closed; the run's full counts (eligible / remaining) ride
    into the report under ``quiet_expiry_counts``, minus the sample, which is a
    CLI affordance rather than something to persist night over night.
    """
    if stop is not None and (reason := stop()):
        _note_stop("review_expired", reason, 0)
        return 0
    try:
        from kbcore import review_expire
    except ImportError:
        logger.warning("kbcore.review_expire is not installed — quiet expiry skipped this run")
        return 0
    result = review_expire.expire_quiet_rows(settings.kb_review_expire_days)
    counts["quiet_expiry_counts"] = {k: v for k, v in result.items() if k != "sample"}
    expired = int(result.get("expired", 0))
    if expired or result.get("eligible_remaining"):
        logger.info("quiet expiry: %d row(s) closed (%s)", expired, counts["quiet_expiry_counts"])
    return expired


def _topup_cutoff(now: datetime | None = None) -> datetime | None:
    """Today's top-up cutoff as a LOCAL datetime, or ``None`` when disabled.

    Parsed defensively like ``kbcore.drainsched`` parses its day list, and for
    the same reason: this runs unattended at 2am, so a typo in the knob must
    degrade to "the feature is off", logged once, and never to a crash that
    takes the surrounding step (or the report) with it. Empty is the documented
    kill switch and is silent; anything unparseable warns.
    """
    raw = (settings.kb_triage_topup_until or "").strip()
    if not raw:
        return None
    try:
        hh, mm = (int(x) for x in raw.split(":", 1))
        return (now or datetime.now()).replace(hour=hh, minute=mm, second=0, microsecond=0)
    except (ValueError, TypeError):
        logger.warning(
            "KB_TRIAGE_TOPUP_UNTIL %r is not HH:MM — the weekend triage top-up is OFF this run",
            raw,
        )
        return None


def review_triaged_topup(counts: dict[str, Any], stop: StopCheck | None = None) -> int:
    """Tail step ``review_triaged_topup``: spend an unbounded night on the backlog.

    Owner request 2026-08-08 — "at weekend triage as many items as we can".
    Saturday's run finished at 19:45 and the card then idled until the next 01:00
    firing, while thousands of unverified extractions sat untriaged. On a day
    with NO drain deadline (:func:`kbcore.drainsched.is_unbounded_day` — ONE
    definition of "the box is free", not a second weekend notion) this keeps
    calling the same :func:`kbcore.review_triage.run_triage` the normal step
    uses, until any of: the cutoff knob, an empty queue (a slice that triages 0),
    or the run's stand-down guard.

    SLICES, NOT ONE BIG BATCH, and that is the whole design: the run holds
    ``pg_advisory_lock(42)`` end to end, so a 1000-row batch (~2.9 h) admitted at
    22:59 would still be running at the next 01:00 firing and make that WHOLE RUN
    skip — the top-up would have eaten the night it was meant to add to. A
    ``kb_triage_topup_slice``-sized slice bounds the overshoot past the cutoff to
    one slice (~35 min at 200 rows), which leaves the report, the digest and the
    post-run backup their room.

    The cutoff is anchored ONCE, at entry, so a slice that runs past midnight
    cannot be handed a fresh 23-hour window by tomorrow's date; the day check IS
    re-asked per slice, so a Sunday top-up that survives into Monday stops rather
    than leaking the weekend into the work week. Battery is still honoured — the
    guard is the run's own, unchanged — and standing down costs nothing: the rows
    are simply left open and untriaged for the next run.

    UNATTENDED RUNS ONLY (``stop is None`` tops up nothing), for the same reason
    :func:`kbworker.consolidate.nightly` — not ``main`` — arms the guard: an
    owner who types ``kb consolidate-now`` asked for a consolidation, not for a
    nine-hour triage grind that holds the advisory lock till 23:00 (and would
    make the real nightly skip). Requiring the guard also means the top-up is
    never running where nothing could stop it. The owner-invoked path for
    "triage more, now" is the one that already exists: ``kb review triage``.
    """
    from kbcore import review_triage
    from kbcore.drainsched import is_unbounded_day

    cutoff = _topup_cutoff()
    slice_size = int(settings.kb_triage_topup_slice)
    if stop is None or cutoff is None or slice_size <= 0:
        return 0

    triaged = 0
    slices = 0
    for _slice in range(TOPUP_MAX_SLICES):
        if not is_unbounded_day():
            # Not a stand-down: a deadline day simply has no idle window to fill,
            # and the normal triage step already had its bounded turn.
            break
        if datetime.now() >= cutoff:
            logger.info(
                "triage top-up reached its %s cutoff after %d row(s)", cutoff.time(), triaged
            )
            break
        if reason := stop():
            _note_stop("review_triaged_topup", reason, 0)
            break
        stored = review_triage.run_triage(slice_size, should_stop=stop)
        slices += 1
        triaged += stored
        # Slice telemetry rides into the run report beside the count, so a night
        # that stopped after one slice is distinguishable from one that never
        # started — the same reason the drain records cohort_refills.
        counts["triage_topup_slices"] = slices
        if stored == 0:
            break  # the queue is drained (or the LLM is refusing every row)
    else:
        logger.warning(
            "triage top-up hit its %d-slice backstop — the pending set is not shrinking",
            TOPUP_MAX_SLICES,
        )
    if triaged:
        logger.info("triage top-up: %d extra row(s) triaged on an unbounded night", triaged)
    return triaged


def _note_stop(step: str, reason: str, deferred: int) -> None:
    """Record a stand-down in consolidate's stop log (lazy: import cycle)."""
    from kbworker import consolidate

    consolidate._note_stop(step, reason, deferred)

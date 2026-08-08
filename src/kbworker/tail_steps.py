"""kbworker.tail_steps — the nightly tail's P0-2 wiring (auto-apply + digest).

Split from :mod:`kbworker.allocator` when the day-aware deadline shim pushed
that file past the 300-line new-file ratchet — and rightly so: these are TAIL
STEP adapters (owner-decision plumbing), not reserve arithmetic. Both import
their kbcore implementations lazily so a missing module degrades to a no-op
rather than failing the run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from typing import Any

from kbcore.config import settings

logger = logging.getLogger("kbworker.tail_steps")

StopCheck = Callable[[], "str | None"]

DIGEST_EXPIRY_HORIZON_DAYS = 7  # digest horizon for 'rows start expiring'

# Backstop on the top-up slice loop, in the spirit of consolidate.MAX_REFILLS.
# The real bound is the wall clock (every slice moves the clock toward the
# cutoff), so this only exists so a hypothetical slice that stores suggestions
# without ever shrinking the pending set cannot spin all night. At slice 200 and
# the measured 10.4 s/row this is ~29 h of work — reaching it is a loud anomaly.
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


def _expiring_soon(value: Any, today: date | None = None) -> bool:
    """True when an ISO expiry date is already due or due within the horizon."""
    try:
        when = date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return False
    return when <= (today or date.today()) + timedelta(days=DIGEST_EXPIRY_HORIZON_DAYS)


def digest_lines(counts: Mapping[str, Any]) -> list[str]:
    """The one-line-per-fact summary of a run, for the push notification.

    Only facts the run produced: a key the report does not carry (failed step,
    knob off) contributes NO line, rather than a zero that reads like a working
    step which did nothing.
    """
    spec = (
        ("drained", "queued_drained"),
        ("saved", "queued_items_saved"),
        ("triaged", "review_triaged"),
        ("auto-applied", "auto_applied"),
        ("judged pairs", "contradiction_pairs_judged"),
    )
    lines = [f"{label}: {counts[key]}" for label, key in spec if counts.get(key) is not None]
    # The weekend top-up, POSITIVE-ONLY (unlike the spec above): it does not run
    # on a deadline day at all, so a "topup triaged: 0" every weekday night would
    # be noise in the one message the owner reads without going looking.
    if topup := counts.get("review_triaged_topup"):
        lines.append(f"topup triaged: {topup}")
    expiry = counts.get("first_expiry_date")
    if expiry and _expiring_soon(expiry):
        lines.append(f"review rows start expiring {expiry}")
    # The urgent cut of the above (Package O): rows that will age out UNEXAMINED
    # within a week — each becomes permanently-unverified knowledge if not triaged.
    expiring_7d = counts.get("untriaged_expiring_7d")
    if expiring_7d:
        lines.append(f"EXPIRY: {expiring_7d} untriaged rows expire within 7d")
    stale = _stale_hook_projects(counts.get("fleet_degenerate_by_project"))
    if stale:
        # NAMED, not counted: the action is "go update that machine's hooks", and
        # a bare number tells an owner nothing about which box to walk over to.
        lines.append(f"STALE HOOKS suspected: {', '.join(stale)}")
    return lines


def _stale_hook_projects(report: Any) -> list[str]:
    """Projects whose queue rows show a machine still on pre-fix hooks.

    ONE definition of "nonzero", which lives in :mod:`kbcore.fleet_health` beside
    the query that produced the report — a second copy here would be a second
    thing to keep in step. Lazily imported like the other tail steps, so a
    missing module costs this ONE line and not the digest.
    """
    try:
        from kbcore import fleet_health
    except ImportError:
        return []
    return fleet_health.stale_hook_projects(report)


def push_run_digest(counts: Mapping[str, Any]) -> bool:
    """Best-effort push of :func:`digest_lines`. Never raises, never fails a run.

    The digest is the only part of the pass an owner sees without going looking,
    so it must never break the pass that produced it. The credential stays inside
    ``kbcore.notify`` — that is where "never log the token" lives.
    """
    lines = digest_lines(counts)
    if not lines:
        return False
    try:
        from kbcore import notify

        return bool(notify.push_digest(lines))
    except Exception:  # noqa: BLE001 — a digest can never fail the run that made it
        logger.warning("nightly digest push unavailable", exc_info=True)
        return False

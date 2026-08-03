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
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger("kbworker.tail_steps")

StopCheck = Callable[[], "str | None"]

DIGEST_EXPIRY_HORIZON_DAYS = 7  # digest horizon for 'rows start expiring'


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
    expiry = counts.get("first_expiry_date")
    if expiry and _expiring_soon(expiry):
        lines.append(f"review rows start expiring {expiry}")
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

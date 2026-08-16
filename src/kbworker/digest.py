"""kbworker.digest — the nightly run's push-notification digest.

Split from :mod:`kbworker.tail_steps` (mechanical move, 2026-08-14): the
deferral-cap lines pushed that file to the 300-line ratchet, and the digest is
its own concern — pure functions over the run's counts plus one best-effort
push. ``tail_steps`` re-imports every name (the established seam every caller
and test already uses, same pattern as kbworker.consolidate's re-exports), and
the logger keeps the ``kbworker.tail_steps`` name these lines have always
carried in journald.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger("kbworker.tail_steps")

DIGEST_EXPIRY_HORIZON_DAYS = 7  # digest horizon for 'rows start expiring'


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
    # Deferral-cap pressure (review #3 P1 rider, 2026-08-14). An escalation to
    # terminal 'failed' used to be SILENT: no digest line, and journald's <36h
    # retention destroyed the evidence before the owner ever looked. Both lines
    # are POSITIVE-ONLY and pure ASCII (ntfy to a phone); the near-cap line
    # fires ~2 nights of deferrals BEFORE the escalation it warns about.
    if near_cap := counts.get("groups_near_deferral_cap"):
        lines.append(f"drain: {near_cap} group(s) near deferral cap")
    if escalations := counts.get("deferral_escalations"):
        lines.append(f"drain: {escalations} row(s) escalated to failed at the deferral cap")
    # Attempts-cap retirement (review #4 P0 rider, 2026-08-16): a content-failing
    # group's LAST act — after this the queue never offers it again, so the owner
    # hears it here or from nobody. POSITIVE-ONLY and pure ASCII like its
    # neighbours; content_failures stays report-only by default (a sub-cap
    # failure retries tomorrow and is not yet news — noise budget, review #4).
    if retired := counts.get("attempts_cap_retirements"):
        lines.append(f"drain: {retired} group(s) retired at attempts cap")
    # The weekend top-up, POSITIVE-ONLY (unlike the spec above): it does not run
    # on a deadline day at all, so a "topup triaged: 0" every weekday night would
    # be noise in the one message the owner reads without going looking.
    if topup := counts.get("review_triaged_topup"):
        lines.append(f"topup triaged: {topup}")
    # The auto-apply breakdown the tail step stashed in the report. All
    # POSITIVE-ONLY and all pure ASCII (this goes to a phone over ntfy):
    #   re-queued  — the deadlock-breaker fired on a provably-dead target;
    #   stuck      — rows the machine has given up on (a re-queue came back with the
    #                same unappliable verdict). Nothing else will ever move these;
    #                the owner is the only remaining path, so the owner is told;
    #   winners    — duplicate rows whose own item won its cluster, and how many of
    #                those closes the run's cap let through. A close is a row flip
    #                the owner can reverse, so it is reported, not silent (wfulfrf33).
    auto = counts.get("auto_apply_counts") or {}
    if invalidated := auto.get("triage_invalidated"):
        lines.append(f"triage re-queued: {invalidated}")
    if stuck := auto.get("triage_unresolvable"):
        lines.append(f"triage stuck (needs owner): {stuck}")
    if winners := auto.get("skipped_cluster_winner"):
        lines.append(f"cluster winners: {winners} ({auto.get('cluster_winners_closed', 0)} closed)")
    # The two quiet-expiry facts, POSITIVE-ONLY (owner amendment 2026-08-08): what the capped sweep
    # actually closed tonight, and the backlog the untriaged gate now holds open — the number that
    # only falls when work is done. Pure ASCII: this line goes out over ntfy to a phone.
    if expired := counts.get("review_expired"):
        lines.append(f"expired: {expired}")
    if never_triaged := counts.get("never_triaged_open"):
        lines.append(f"never-triaged open: {never_triaged}")
    # THE PROMOTE PROGRAM (review #3 strategic P1, KB 19663): recall-proven
    # unverified items waiting on an owner promote — the owner sees the queue
    # exists without asking. POSITIVE-ONLY and pure ASCII, like its neighbours.
    if promotable := counts.get("promote_candidates"):
        lines.append(f"promote-worthy: {promotable}")
    # Demand-side gap counter (review #4 Wave E, KB 21332): zero-hit searches in
    # the last day (kbcore.search_misses, stashed by push_run_digest). POSITIVE-
    # ONLY and pure ASCII like its neighbours — a night with no misses says
    # nothing rather than a noise line of 'search misses: 0'.
    if misses := counts.get("search_misses_24h"):
        lines.append(f"search misses: {misses}")
    expiry = counts.get("first_expiry_date")
    if expiry and _expiring_soon(expiry):
        lines.append(f"review rows start expiring {expiry}")
    # The urgent cut of the above (Package O): untriaged rows reaching the age at
    # which a TRIAGED row would close. Since the untriaged gate they are held
    # open rather than closed, so this is a pile-up warning, not a loss warning.
    expiring_7d = counts.get("untriaged_expiring_7d")
    if expiring_7d:
        lines.append(f"EXPIRY: {expiring_7d} untriaged rows reach expiry age within 7d")
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
    ``kbcore.notify`` — that is where "never log the token" lives. Two riders,
    each fully guarded: the day's zero-hit count is stashed before the lines are
    built, and the WEEKLY report step runs after the nightly push (day-gated
    inside kbworker.weekly — no new timer; review #4, KB 21332).
    """
    _note_search_misses(counts)
    lines = digest_lines(counts)
    pushed = False
    if lines:
        try:
            from kbcore import notify

            pushed = bool(notify.push_digest(lines))
        except Exception:  # noqa: BLE001 — a digest can never fail the run that made it
            logger.warning("nightly digest push unavailable", exc_info=True)
    _maybe_weekly(counts)
    return pushed


def _note_search_misses(counts: Mapping[str, Any]) -> None:
    """Stash the last day's zero-hit search count into ``counts`` (ADDED key).

    The producer lives in kbcore.search_misses (migration 018). Fail-open and
    lazily imported like every digest dependency: a missing module, schema or DB
    costs this ONE line, never the digest — which is the last thing standing
    between a finished run and the owner's phone.
    """
    if not isinstance(counts, dict):
        return
    try:
        from kbcore import search_misses

        counts["search_misses_24h"] = search_misses.count_since(days=1)
    except Exception:  # noqa: BLE001 — telemetry may never cost the digest
        logger.warning("search-miss count unavailable for the digest", exc_info=True)


def _maybe_weekly(counts: Mapping[str, Any]) -> None:
    """WEEKLY TREND REPORT rider (review #4 strategy P1, KB 21332).

    Day-gated INSIDE :func:`kbworker.weekly.maybe_weekly` (kb_weekly_report /
    kb_weekly_day), so most nights this is a no-op. Lazily imported and fully
    guarded — the weekly can never fail the nightly that carries it.
    """
    if not isinstance(counts, dict):
        return
    try:
        from kbworker import weekly

        weekly.maybe_weekly(counts)
    except Exception:  # noqa: BLE001 — the weekly may never fail the nightly
        logger.warning("weekly report step failed; nightly digest unaffected", exc_info=True)

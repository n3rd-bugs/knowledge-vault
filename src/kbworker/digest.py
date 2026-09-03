"""kbworker.digest — the nightly run's push-notification digest.

Split from :mod:`kbworker.tail_steps` (mechanical move, 2026-08-14): the
deferral-cap lines pushed that file to the 300-line ratchet, and the digest is
its own concern — pure functions over the run's counts plus one best-effort
push. ``tail_steps`` re-imports every name (the established seam every caller
and test already uses, same pattern as kbworker.consolidate's re-exports), and
the logger keeps the ``kbworker.tail_steps`` name these lines have always
carried in journald.

DIGEST HYGIENE (review #6): ALERT-class lines render FIRST (phone previews
truncate; the 08-18/19 red nights rendered EVAL RED dead last) and the digest
is hard-capped at :data:`DIGEST_MAX_LINES` — kbcore.notify bounds bytes, not
lines, and 25+ lines are now reachable.

TRUTH-TELLING BUNDLE (review #8 H7, KB 31857): the alert tier gained the lines
the cap must never truncate — a rerank FALLBACK tally, a battery PAUSE, swap
past :data:`SWAP_ALERT_PCT`, the miss counters — so the 20-line cap trims only
routine facts; a resumed run says so in its FIRST fact line; the drain's
deferred count renders the honest H6 number; a run-duration line closes the
facts. Rendering only — every number comes from the run's counts, and a key the
run does not carry still contributes NO line.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

# MECHANICAL SPLIT (2026-09-03, review #8 H0): the composed one-line renderers
# moved to kbworker.digest_lines when this file reached the 300-line cap. They
# are re-imported HERE because this module is the seam every caller and test
# resolves through (tail_steps re-imports from digest in turn) — _alert_lines /
# _fact_lines below keep calling them by their bare names. The RETIRED expiry
# helper (DIGEST_EXPIRY_HORIZON_DAYS / _expiring_soon, review #6) moved with
# them in H7 to keep this file under its cap; both names still resolve here.
from kbworker.digest_lines import (  # noqa: F401
    DIGEST_EXPIRY_HORIZON_DAYS,
    NIGHT_ROLLOVER_HOUR,
    _drain_deferred_line,
    _eval_red_line,
    _expiring_soon,
    _promote_runs_line,
    _rerank_fell_back,
    _rerank_tally_line,
    _resume_line,
    _run_duration_line,
    _stale_hook_projects,
    _stopped_line,
    _trust_watch_line,
    night_of,
)

logger = logging.getLogger("kbworker.tail_steps")

DIGEST_MAX_LINES = 20  # hard line cap on the nightly push (review #6)
SWAP_ALERT_PCT = 70  # host swap past this is alert-class (review #8 H7b; was a fact past 80)


def digest_lines(counts: Mapping[str, Any]) -> list[str]:
    """The one-line-per-fact summary of a run, for the push notification.

    Only facts the run produced: a key the report does not carry (failed step,
    knob off) contributes NO line, rather than a zero that reads like a working
    step which did nothing. ALERT-class lines first, then the facts, capped at
    :data:`DIGEST_MAX_LINES` — a fat night truncates facts, never an alert.
    """
    return (_alert_lines(counts) + _fact_lines(counts))[:DIGEST_MAX_LINES]


def _alert_lines(counts: Mapping[str, Any]) -> list[str]:
    """The lines that must survive a truncated phone preview, rendered first."""
    lines: list[str] = []
    if counts.get("eval_regression"):
        lines.append(_eval_red_line(counts))
    if counts.get("eval_trust_status") == "watch":
        lines.append(_trust_watch_line(counts))
    # Untriaged rows reaching the age at which a TRIAGED row would close. Since
    # the untriaged gate they are HELD OPEN, so this is a pile-up warning.
    if expiring_7d := counts.get("untriaged_expiring_7d"):
        lines.append(f"EXPIRY: {expiring_7d} untriaged rows reach expiry age within 7d")
    stale = _stale_hook_projects(counts.get("fleet_degenerate_by_project"))
    if stale:
        # NAMED, not counted: the action is "go update that machine's hooks",
        # and a bare number does not say which box to walk over to.
        lines.append(f"STALE HOOKS suspected: {', '.join(stale)}")
    # An escalation to terminal 'failed' used to be SILENT (review #3 P1 rider);
    # it is the drain's most consequential outcome, so it rides with the alerts.
    if escalations := counts.get("deferral_escalations"):
        lines.append(f"drain: {escalations} row(s) escalated to failed at the deferral cap")
    # Review #8 H7a/b: a rerank FALLBACK night was measured on the fused order —
    # the tally rides the alert tier VERBATIM so the scalar can never hide it.
    tally = counts.get("eval_rerank_tally")
    if _rerank_fell_back(tally) and (tally_line := _rerank_tally_line(tally)):
        lines.append(tally_line)
    # A battery PAUSE is alert-class (H7b/d): the run is waiting on the grid and
    # the AC-restore trigger; a pure deadline cut stays a routine fact below.
    stopped_line = _stopped_line(counts.get("stopped"))
    if stopped_line and stopped_line.startswith("paused"):
        lines.append(stopped_line)
    # Host swap telemetry (review #5 strat-7/ops-3), alert tier past 70% (H7b).
    swap = counts.get("swap_used_pct")
    if isinstance(swap, int | float) and not isinstance(swap, bool) and swap > SWAP_ALERT_PCT:
        lines.append(f"swap: {swap:.0f}% used")
    # Demand-side gap counters (review #4 Wave E, KB 21332; thin split Wave F) —
    # alert tier since H7b: a truncated preview must still show what agents
    # asked for and never got.
    if misses := counts.get("search_misses_24h"):
        lines.append(f"empty-result searches: {misses}")
    if thin := counts.get("search_misses_thin_24h"):
        lines.append(f"thin-result searches: {thin}")
    return lines


def _fact_lines(counts: Mapping[str, Any]) -> list[str]:
    """The positive-only fact lines, in their long-standing order. Pure ASCII —
    every one of these goes out over ntfy to a phone."""
    lines: list[str] = []
    # Review #8 H7d: a run that resumed a battery pause says so FIRST.
    if resume := _resume_line(counts):
        lines.append(resume)
    spec = (
        ("drained", "queued_drained"),
        ("saved", "queued_items_saved"),
        ("triaged", "review_triaged"),
        ("auto-applied", "auto_applied"),
        ("judged pairs", "contradiction_pairs_judged"),
    )
    lines += [f"{label}: {counts[key]}" for label, key in spec if counts.get(key) is not None]
    # Deferral-cap pressure (review #3 P1 rider): fires ~2 nights of deferrals
    # BEFORE the escalation it warns about (the escalation line is ALERT-class).
    if near_cap := counts.get("groups_near_deferral_cap"):
        lines.append(f"drain: {near_cap} group(s) near deferral cap")
    # The honest drained_deferred count (review #6 contract, re-cut by H6 to
    # every stop path) — rows older than the run start still pending.
    if deferred_line := _drain_deferred_line(counts):
        lines.append(deferred_line)
    # Attempts-cap retirement (review #4 P0 rider): a content-failing group's
    # LAST act — the owner hears it here or from nobody.
    if retired := counts.get("attempts_cap_retirements"):
        lines.append(f"drain: {retired} group(s) retired at attempts cap")
    # Review #5 val-3/strat-8: wall-blocked reject suggestions given their
    # terminal (reversible) disposition tonight.
    if closed := counts.get("wall_blocked_closed"):
        lines.append(f"wall-blocked rejects closed: {closed}")
    # The weekend top-up does not run on a deadline day at all, so a "topup
    # triaged: 0" every weekday night would be noise.
    if topup := counts.get("review_triaged_topup"):
        lines.append(f"topup triaged: {topup}")
    # The auto-apply breakdown: re-queued (deadlock-breaker fired), stuck (the
    # machine gave up — the owner is the only remaining path), winners
    # (cluster-winner duplicate closes let through by the cap — wfulfrf33).
    auto = counts.get("auto_apply_counts") or {}
    if invalidated := auto.get("triage_invalidated"):
        lines.append(f"triage re-queued: {invalidated}")
    if stuck := auto.get("triage_unresolvable"):
        lines.append(f"triage stuck (needs owner): {stuck}")
    if winners := auto.get("skipped_cluster_winner"):
        lines.append(f"cluster winners: {winners} ({auto.get('cluster_winners_closed', 0)} closed)")
    # Quiet expiry + cap saturation (review #6): 08-28 hit the cap invisibly on
    # every surface the owner reads. The raise-the-cap decision stays gated on
    # its watchlist trigger, but saturation must be VISIBLE.
    if expired := counts.get("review_expired"):
        quiet = counts.get("quiet_expiry_counts") or {}
        remaining = quiet.get("eligible_remaining") if isinstance(quiet, Mapping) else 0
        if isinstance(remaining, int) and not isinstance(remaining, bool) and remaining > 0:
            lines.append(f"expired: {expired} (cap; {remaining} still eligible)")
        else:
            lines.append(f"expired: {expired}")
    if never_triaged := counts.get("never_triaged_open"):
        lines.append(f"never-triaged open: {never_triaged}")
    # THE PROMOTE PROGRAM (review #3 strategic P1, KB 19663): the owner sees the
    # recall-proven unverified queue exists without asking.
    if promotable := counts.get("promote_candidates"):
        lines.append(f"promote-worthy: {promotable}")
    if promote_runs := _promote_runs_line(counts):  # H13c: uptake costs one paste
        lines.append(promote_runs)
    # The rerank tally on a clean night is a routine fact (H7a: rendered
    # verbatim either way; a fallback night carried it in the alert tier).
    tally = counts.get("eval_rerank_tally")
    if not _rerank_fell_back(tally) and (tally_line := _rerank_tally_line(tally)):
        lines.append(tally_line)
    # Rerank sidecar probe (review #5 ops-4): only a failure is news.
    if counts.get("rerank_probe") == "failed":
        lines.append("rerank probe: failed")
    # Review #6: a step cut short by the stand-down guard was indistinguishable
    # from an idle one on every rendered surface — one line names the cut steps.
    # A battery pause rendered in the alert tier; a deadline-only cut lands here.
    stopped_line = _stopped_line(counts.get("stopped"))
    if stopped_line and not stopped_line.startswith("paused"):
        lines.append(stopped_line)
    if duration := _run_duration_line(counts):
        lines.append(duration)
    return lines


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
    _note_promote_runs(counts)
    lines = digest_lines(counts)
    pushed = False
    if lines:
        try:
            from kbcore import notify

            if counts.get("eval_regression"):
                # Red eval night (review #5 ops-1): a SUCCESSFUL unit run whose
                # quality regressed — OnFailure never fires, so the digest itself
                # must escalate. Green nights keep the byte-identical default call.
                pushed = bool(notify.push_digest(lines, title="KB ALERT", priority="high"))
            else:
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
        # Thin-miss stash (review #5 val-1 contract, cross-package): the
        # relevance instrument records subtype='thin' rows and count_since
        # gained a subtype parameter (default 'empty' — the key above keeps its
        # semantics). Guarded so a pre-thin search_misses (no subtype yet)
        # costs the thin key only, never the digest.
        try:
            counts["search_misses_thin_24h"] = search_misses.count_since(days=1, subtype="thin")
        except TypeError:  # pre-thin signature: no subtype parameter
            pass
    except Exception:  # noqa: BLE001 — telemetry may never cost the digest
        logger.warning("search-miss count unavailable for the digest", exc_info=True)


def _note_promote_runs(counts: Mapping[str, Any]) -> None:
    """Stash last night's promote-run summary into ``counts`` (ADDED key, H13c).

    :func:`kbcore.review_promote_runs.promote_runs_summary` is READ-ONLY and
    owner-gated (the nightly runs as the owner; a grantee context simply gets
    no line). Same posture as :func:`_note_search_misses`: lazily imported,
    fail-open — a missing module or query costs this ONE line, never the digest.
    """
    if not isinstance(counts, dict):
        return
    try:
        from kbcore import review_promote_runs

        counts["promote_runs_ready"] = review_promote_runs.promote_runs_summary(days=1)
    except Exception:  # noqa: BLE001 — telemetry may never cost the digest
        logger.warning("promote-run summary unavailable for the digest", exc_info=True)


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

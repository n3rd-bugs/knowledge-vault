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
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger("kbworker.tail_steps")

DIGEST_MAX_LINES = 20  # hard line cap on the nightly push (review #6)

# RETIRED LINE (review #6): 'review rows start expiring <date>' was permanently
# true post-rails — wallpaper; 'expired: N' + the EXPIRY 7d line do its job. The
# constant and helper stay importable (kbworker.tail_steps re-imports them).
DIGEST_EXPIRY_HORIZON_DAYS = 7


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
    return lines


def _fact_lines(counts: Mapping[str, Any]) -> list[str]:
    """The positive-only fact lines, in their long-standing order. Pure ASCII —
    every one of these goes out over ntfy to a phone."""
    spec = (
        ("drained", "queued_drained"),
        ("saved", "queued_items_saved"),
        ("triaged", "review_triaged"),
        ("auto-applied", "auto_applied"),
        ("judged pairs", "contradiction_pairs_judged"),
    )
    lines = [f"{label}: {counts[key]}" for label, key in spec if counts.get(key) is not None]
    # Deferral-cap pressure (review #3 P1 rider): fires ~2 nights of deferrals
    # BEFORE the escalation it warns about (the escalation line is ALERT-class).
    if near_cap := counts.get("groups_near_deferral_cap"):
        lines.append(f"drain: {near_cap} group(s) near deferral cap")
    # Weekday drain budget (review #6 cross-package contract): items the drain
    # SHED to the weekend tonight — previously inferable only from finish times.
    if deferred := counts.get("drained_deferred"):
        lines.append(f"drain: {deferred} item(s) deferred to the weekend budget")
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
    # Demand-side gap counters (review #4 Wave E, KB 21332; thin split Wave F).
    if misses := counts.get("search_misses_24h"):
        lines.append(f"empty-result searches: {misses}")
    if thin := counts.get("search_misses_thin_24h"):
        lines.append(f"thin-result searches: {thin}")
    # Host swap telemetry (review #5 strat-7/ops-3): rendered only past 80%.
    swap = counts.get("swap_used_pct")
    if isinstance(swap, int | float) and not isinstance(swap, bool) and swap > 80:
        lines.append(f"swap: {swap:.0f}% used")
    # Rerank sidecar probe (review #5 ops-4): only a failure is news.
    if counts.get("rerank_probe") == "failed":
        lines.append("rerank probe: failed")
    # Review #6: a step cut short by the stand-down guard was indistinguishable
    # from an idle one on every rendered surface — one line names the cut steps.
    if stopped_line := _stopped_line(counts.get("stopped")):
        lines.append(stopped_line)
    return lines


def _stopped_line(stopped: Any) -> str | None:
    """One positive-only line for the steps the run cut short, or None.

    ``counts['stopped']`` is ``{step: {'reason', 'deferred'}}``. A cut involving
    'battery' is a PAUSE — grid restore (kb-ac-restore.timer) or the next run
    resumes it — rendered ``paused (battery): evidence_refreshed(20), ...``;
    other reasons keep ``stopped early``. Reasons join with ``/`` and stay
    verbatim (they are DATA). Shape-tolerant as ever."""
    if not isinstance(stopped, Mapping) or not stopped:
        return None
    reasons: list[str] = []
    parts: list[str] = []
    for step, info in stopped.items():
        detail = info if isinstance(info, Mapping) else {}
        reason = str(detail.get("reason", "?"))
        if reason not in reasons:
            reasons.append(reason)
        parts.append(f"{step}({detail.get('deferred', '?')})")
    label = "paused" if "battery" in reasons else "stopped early"
    return f"{label} ({'/'.join(reasons)}): {', '.join(parts)}"


def _eval_red_line(counts: Mapping[str, Any]) -> str:
    """The red-eval digest line: observed hit@5 against the anchored floor.

    The floor is read lazily (the digest's convention — a broken import costs
    ONE line, never the digest) at RENDER time, so the line always names the
    floor the sentinel actually compared against tonight.
    """
    try:
        from kbcore.config import settings

        floor: str = f"{settings.kb_eval_min_hit5:g}"
    except Exception:  # noqa: BLE001 — a digest line may never cost the digest
        floor = "?"
    hit = counts.get("eval_hit_at_5")
    return f"EVAL RED: hit@5 {hit if hit is not None else '?'} (floor {floor})"


def _trust_watch_line(counts: Mapping[str, Any]) -> str:
    """TRUST WATCH with the lift's COMPONENTS beside it (review #6): ~90% of the
    08-20..08-27 lift climb was corpus-denominator decay, so a bare ratio reads
    as a ranking move it is not. Numerator (verified top-5 count) and
    denominator (corpus verified fraction) render with the composite whenever
    the run carries them; an older report renders the bare line as before."""
    share = counts.get("eval_top5_unverified_share")
    lift = counts.get("eval_top5_verified_lift")
    line = (
        f"TRUST WATCH: top5 unverified {share if share is not None else '?'}, "
        f"verified lift {f'{lift}x' if lift is not None else 'n/a'}"
    )
    verified = counts.get("eval_top5_verified")
    total = counts.get("eval_top5_results")
    unverified = counts.get("eval_top5_unverified")
    if verified is None and total is not None and unverified is not None:
        verified = total - unverified  # pre-Wave-G reports carry only the parts
    frac = counts.get("eval_corpus_verified_fraction")
    if verified is not None and total and frac is not None:
        line += f" (v-top5 {verified}/{total}, corpus {frac})"
    return line


def _stale_hook_projects(report: Any) -> list[str]:
    """Projects whose queue rows show a machine still on pre-fix hooks.

    ONE definition of "nonzero", which lives in :mod:`kbcore.fleet_health`
    beside the query that produced the report. Lazily imported like the other
    tail steps, so a missing module costs this ONE line and not the digest.
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

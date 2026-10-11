"""kbworker.digest_lines — the digest's composed one-line renderers.

Split from :mod:`kbworker.digest` (mechanical move, 2026-09-03, review #8 H0):
digest.py sat exactly at the 300-line cap and the truth-telling bundle (H7)
needs room to render. Pure functions over the run's counts — no I/O, no state,
lazy imports only (a broken import costs ONE line, never the digest, as ever).
``digest`` re-imports every name (the established seam; ``tail_steps``
re-imports from ``digest``), so ``digest._stopped_line`` etc. keep resolving.
Pure ASCII output — every line goes out over ntfy to a phone.

Review #8 H7 renderers: the rerank tally VERBATIM (the scalar can never hide
it), the honest ``drained_deferred`` line (H6 contract), the RESUME line for a
run that picked up a battery pause (H7d — the producer is the prior marker
payload in ``counts['resumed_from_pause']``), a run-duration line, and
:func:`night_of`, the ONE local-night rule the weekly's per-night rows share.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

NIGHT_ROLLOVER_HOUR = 12  # a run STARTING before noon LOCAL belongs to the previous night

# RETIRED LINE (review #6): 'review rows start expiring <date>' was permanently
# true post-rails — wallpaper; 'expired: N' + the EXPIRY 7d line do its job. The
# constant and helper stay importable (digest and kbworker.tail_steps re-import
# them); moved here from digest.py in H7 (mechanical, no behaviour change).
DIGEST_EXPIRY_HORIZON_DAYS = 7


def _expiring_soon(value: Any, today: date | None = None) -> bool:
    """True when an ISO expiry date is already due or due within the horizon."""
    try:
        when = date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return False
    return when <= (today or date.today()) + timedelta(days=DIGEST_EXPIRY_HORIZON_DAYS)


def window_bounds(
    now: datetime, tz: tzinfo | None = None, nights_back: int = 7, start_after: date | None = None
) -> tuple[date, date]:
    """``(first, last)`` COMPLETED local nights a weekly covers (review #9a INS-6):
    ``last`` is the night BEFORE ``night_of(now)`` — the weekly fires inside the
    Sunday night, so that night's later runs roll into NEXT week instead of
    vanishing; ``first`` tiles after the prior weekly's ``nights_through``."""
    last = night_of(now, tz) - timedelta(days=1)
    span = timedelta(days=nights_back - 1)
    first = start_after + timedelta(days=1) if start_after else last - span
    return min(first, last), last


def night_of(at: datetime, tz: tzinfo | None = None) -> date:
    """The LOCAL calendar night a run belongs to (review #8 H7d / H9f; #9a INS-1).

    Stored instants are UTC (Invariant 7); this is the display-time rule both
    the digest's RESUME line and the weekly's per-night rows share: convert to
    local (``tz`` None = the box's zone; tests pin one) and file anything
    before :data:`NIGHT_ROLLOVER_HOUR` under the night that began the day
    before. A naive input is UTC.

    ``at`` is the run's START (review #9a INS-1: keyed on the finish, a nightly
    overrunning the rollover filed under the NEXT night — the Sep 5 06:57
    finish split its own night in two). The rollover is NOON, decided
    deliberately: the 01:30 nightly, a grid-restore resume at 07:10 or 09:32
    and a 10:00 boot catch-up are all continuations of the night they finish,
    so they land on ONE row; a run starting after noon opens the coming night.
    """
    aware = at if at.tzinfo is not None else at.replace(tzinfo=UTC)
    local = aware.astimezone(tz)
    night = local.date()
    return night - timedelta(days=1) if local.hour < NIGHT_ROLLOVER_HOUR else night


def _stopped_line(stopped: Any) -> str | None:
    """One positive-only line for the steps the run cut short, or None.

    ``counts['stopped']`` is ``{step: {'reason', 'deferred'}}``. A cut involving
    'battery' is a PAUSE — grid restore (kb-ac-restore.timer) or the next run
    resumes it — rendered ``paused (battery): evidence_refreshed(20), ...``;
    other reasons keep ``stopped early``. Reasons join with ``/`` and stay
    verbatim (they are DATA). Steps whose deferred count is 0 are DROPPED
    (review #8 H7e: RUN1 rendered 7 steps, 3 of them '(0)' — a cut that left
    nothing behind is not news); if every count is 0 the cut steps are still
    NAMED, bare. Shape-tolerant as ever."""
    if not isinstance(stopped, Mapping) or not stopped:
        return None
    reasons: list[str] = []
    parts: list[str] = []
    names: list[str] = []
    for step, info in stopped.items():
        detail = info if isinstance(info, Mapping) else {}
        reason = str(detail.get("reason", "?"))
        if reason not in reasons:
            reasons.append(reason)
        names.append(str(step))
        deferred = detail.get("deferred", "?")
        if deferred != 0:
            parts.append(f"{step}({deferred})")
    label = "paused" if "battery" in reasons else "stopped early"
    return f"{label} ({'/'.join(reasons)}): {', '.join(parts or names)}"


def _rerank_tally_line(tally: Any) -> str | None:
    """``rerank: active N / fallback N / off N`` — the per-case tally VERBATIM.

    Review #8 H7a: ``eval_rerank_outcome`` is a rollup (fallback > active > off)
    and a scalar can hide a tally — 'off' read as 'sidecar idle' on nights the
    sidecar scored 51 of 52 cases. The three counts render as one line whenever
    the run carries them; an older report (no tally) renders nothing.
    """
    if not isinstance(tally, Mapping):
        return None
    vals = {k: tally.get(k) for k in ("active", "fallback", "off")}
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in vals.values()):
        return None
    return f"rerank: active {vals['active']} / fallback {vals['fallback']} / off {vals['off']}"


def _rerank_fell_back(tally: Any) -> bool:
    """True when at least one replayed case was measured on the fused order."""
    return isinstance(tally, Mapping) and bool(tally.get("fallback"))


def _drain_deferred_line(counts: Mapping[str, Any]) -> str | None:
    """``drain: N deferred`` — the HONEST count of queue rows the run left behind.

    H6 contract (pkg-pipeline writes ``counts['drained_deferred']`` on EVERY
    stop path — budget, guard, pre-first-cohort stand-down — as rows older than
    the run start; this renders it, key name unchanged). The old wording
    ('deferred to the weekend budget') was only true for the budget path.
    """
    deferred = counts.get("drained_deferred")
    if isinstance(deferred, int) and not isinstance(deferred, bool) and deferred > 0:
        return f"drain: {deferred} deferred"
    return None


def _hm(secs: float) -> str:
    """``5h 12m`` past an hour, else ``12m 03s`` — phone-width, ASCII."""
    total = int(round(secs))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {seconds:02d}s"


TIMED_STEP_MIN_SECS = 1.0  # a ledger entry under this is an instant/stood-down step


def _run_duration_line(counts: Mapping[str, Any]) -> str | None:
    """How long the run took, from the data the report already carries.

    ``counts['run_secs']`` (wall clock) is preferred when a producer lands it
    (an ADDED key, not required); otherwise the ``step_secs`` cost ledger sums
    to the TIMED total and the label says so — the ledger omits untimed glue,
    so the two numbers are not the same thing and are never labelled alike.
    The wall-clock form counts the steps that took >= :data:`TIMED_STEP_MIN_SECS`
    against the ledger size (review #9a INS-7: ``run_report._run_step`` ledgers
    EVERY invoked step, so '(20 steps timed)' was a constant on every run —
    five of those twenty were stood down and fifteen more finished instantly).
    """
    steps = counts.get("step_secs")
    timed = (
        {
            k: float(v)
            for k, v in steps.items()
            if isinstance(v, int | float) and not isinstance(v, bool)
        }
        if isinstance(steps, Mapping)
        else {}
    )
    wall = counts.get("run_secs")
    if isinstance(wall, int | float) and not isinstance(wall, bool) and wall > 0:
        slow = sum(1 for v in timed.values() if v >= TIMED_STEP_MIN_SECS)
        return f"run: {_hm(wall)} ({slow}/{len(timed)} steps >= 1s)"
    if timed and sum(timed.values()) > 0:
        return f"steps timed: {_hm(sum(timed.values()))} over {len(timed)}"
    return None


def _instant(value: Any) -> datetime | None:
    """An ISO instant as an aware datetime (naive = UTC), or None when unparseable."""
    try:
        at = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _resume_line(counts: Mapping[str, Any], tz: tzinfo | None = None) -> str | None:
    """``RESUME of <night> (paused HH:MM local, N deferred)`` — or None.

    Review #8 H7d: a resumed run used to read as an ordinary short night. The
    producer contract is ``counts['resumed_from_pause']`` = the PRIOR pause
    marker payload (powerguard's ``record_battery_pause`` returning what it
    replaced), absent/None when the run did not resume anything. ``'at'`` is
    the paused run's FIRST battery stand-down instant (review #9a INS-2 — it
    used to be the marker's write time, 2h44m after the grid came back on Sep
    5), ``'deferred'`` its total, and ``'started'`` (ADDED, tail_ops) the paused
    run's start, which keys the night (INS-1; ``'at'`` when absent, pre-H.1
    markers). Night and clock are LOCAL at render time only.
    """
    prior = counts.get("resumed_from_pause")
    if not isinstance(prior, Mapping):
        return None
    at = _instant(prior.get("at"))
    if at is None:
        return None
    started = _instant(prior.get("started")) or at
    deferred = prior.get("deferred")
    shown = deferred if isinstance(deferred, int) and not isinstance(deferred, bool) else "?"
    return (
        f"RESUME of {night_of(started, tz).isoformat()} "
        f"(paused {at.astimezone(tz).strftime('%H:%M')} local, {shown} deferred)"
    )


RUN_KEY_CHARS = 48  # a promote-run key is previewed on the phone, not quoted whole


def _promote_runs_line(counts: Mapping[str, Any]) -> str | None:
    """``promote-run ready: N runs / M items (largest: <key>, K items)`` — or None.

    Review #8 H13c, the digest half: ``counts['promote_runs_ready']`` is
    :func:`kbcore.review_promote_runs.promote_runs_summary`'s dict, stashed by
    the digest's own fail-open rider, so uptake costs one paste of the largest
    run's key into ``kb review promote-run``. The key is stored ``sources.uri``
    text (CAP-write data): flattened to one line, ASCII-folded and bounded, the
    weekly's injection posture. Positive-only — zero ready runs render nothing.
    """
    ready = counts.get("promote_runs_ready")
    if not isinstance(ready, Mapping):
        return None
    runs, items = ready.get("runs"), ready.get("items")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (runs, items)) or runs <= 0:
        return None
    line = f"promote-run ready: {runs} runs / {items} items"
    key = ready.get("largest_key")
    if key:
        flat = " ".join(str(key).split()).encode("ascii", "replace").decode("ascii")
        line += f" (largest: {flat[:RUN_KEY_CHARS]}, {ready.get('largest_items', '?')} items)"
    return line


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

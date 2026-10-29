"""kbworker.eval_persistence — the per-case PERSISTENCE trip (E2, review #10).

WHY (review #9 KB 40402, amended at review #10): the floors and the epsilon
read whole-set means, so ONE case going from rank 1 to a miss and staying
there is invisible as long as the rest of the set holds — case 14's real
18-day loss of its verified satisfier read as a single quantum. The trip reads
the case itself: a golden case is PERSISTENT when it failed on the
:data:`PERSIST_NIGHTS` most recent consecutive same-epoch, fallback-free
NIGHTS including tonight AND passed on at least one earlier such night. No
flapper list and no known-red list: a case that never passed in the epoch can
never arm, so a declared known-red costs nothing.

NIGHTS are the repo's one night rule — ``digest_lines.night_of`` (noon
rollover, local zone) of the run's START, exactly as the weekly's table rows
file runs (``weekly_instruments.run_start``: the report instant minus
``run_secs``) — and a night's verdict comes from its LAST fallback-free run
(``weekly_instruments._eval_run``'s rule). TONIGHT has no report yet (the run
report is written after the sentinel), so its start is ``counts['run_started_at']``
— stamped by ``consolidate.main`` at its run clock, the instant tomorrow's
history will reconstruct as report time minus ``run_secs`` — and, for a run
without the stamp, an estimate: now minus the step ledger so far
(:func:`tonight_start`). Fallback runs never reach
the history (``kbcore.eval_baseline.case_history``), so a fallback night
neither counts nor resets a streak; a fallback run TONIGHT evaluates nothing
(the detector does not call in). Reports without ``eval_case_passed`` predate
E2 night 1 and are simply not history.

STICKY ARMING (review #10 fix F1): the history is a bounded window, and a case
red for longer than it would lose its last pass from view and silently disarm.
So the trip reads its own past verdict back, PER CASE (:func:`carried`): a
case's NEWEST listing in any history report's ``eval_case_persistent`` keeps
it persistent tonight when every night from that listing's on failed it (each
night's LAST run decides, exactly as the series does) and tonight fails —
keeping that entry's ``since`` and ``satisfier``, with ``nights`` = the listed
nights + the red nights since + tonight (a listing from an earlier run of
tonight's own night already counted it). Its first pass tonight is a
first-pass notice. Per case, not per report (fix-round review D1): a report
that merely OMITS a case — a same-night green flap, an UNKNOWN night with no
list — can never drop it while it stays red. The window-derived entry wins
where both apply.

Output, by golden INDEX (1-based, tonight's ``eval_case_passed`` order — the
golden order, fixed within an epoch) and satisfier id, never query text:

* ``eval_case_persistent`` — ``[{case, satisfier, since, nights}]``: the
  first satisfier on the last passing night (or null), the ISO date of the
  streak's first failing night, the streak length. Any entry trips
  ``eval_regression`` (kbworker.eval_regression, cause ``persistent``).
* ``eval_case_first_pass`` — ``[{case, satisfier}]``: a NOTICE (not an alert)
  for a case passing tonight after failing on >= :data:`PERSIST_NIGHTS`
  consecutive prior nights — the cheap false-green check for a known-red.

Pure functions over the history the detector reads; :func:`_now` and
:data:`NIGHT_TZ` are the clock/zone seams tests pin.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

from kbworker.digest_lines import night_of
from kbworker.weekly_instruments import run_start

PERSIST_NIGHTS = 3  # consecutive failing nights (tonight included) that arm the trip
NIGHT_TZ: tzinfo | None = None  # night_of's zone: None = the box's own (tests pin one)
Report = tuple[datetime, Mapping[str, Any]]
Carry = dict[
    int, tuple[date, Mapping[str, Any]]
]  # case idx -> its newest listing (:func:`carried`)


def _now() -> datetime:
    return datetime.now(UTC)


def _num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def tonight_start(counts: Mapping[str, Any], now: datetime) -> datetime:
    """Tonight's run START: ``counts['run_started_at']`` (UTC ISO) when the run
    stamped it, else ``now`` minus the ``step_secs`` ledger timed so far."""
    stamped = counts.get("run_started_at")
    if isinstance(stamped, str):
        try:
            at = datetime.fromisoformat(stamped)
            return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
        except ValueError:
            pass  # a malformed stamp: the ledger estimate below
    ledger = counts.get("step_secs")
    secs = sum(float(v) for v in ledger.values() if _num(v)) if isinstance(ledger, Mapping) else 0
    return now - timedelta(seconds=secs)


def _passed(meta: Mapping[str, Any]) -> Mapping[str, Any]:
    v = meta.get("eval_case_passed")
    return v if isinstance(v, Mapping) else {}


def _satisfier(meta: Mapping[str, Any], key: str) -> int | None:
    sat = meta.get("eval_case_satisfier")
    v = sat.get(key) if isinstance(sat, Mapping) else None
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def night_series(
    history: Sequence[Report],
    tonight_at: datetime,
    tonight: Mapping[str, Any],
    tz: tzinfo | None = None,
) -> list[tuple[date, Mapping[str, Any]]]:
    """One verdict meta per night, oldest first, TONIGHT last.

    ``history`` is OLDEST first (``case_history``), so a later run of a night
    overwrites an earlier one: the night's LAST fallback-free run counts.
    Tonight is the latest run of its own night and replaces any earlier
    report from that night; a history night after tonight's (clock skew) is
    dropped rather than allowed to stand in for tonight.
    """
    tonight_night = night_of(tonight_at, tz)
    by_night: dict[date, Mapping[str, Any]] = {}
    for at, meta in history:
        night = night_of(run_start(at, meta), tz)
        if night < tonight_night:
            by_night[night] = meta
    by_night[tonight_night] = tonight
    return sorted(by_night.items())


def _trailing_reds(verdicts: Sequence[bool]) -> int:
    n = 0
    for passed in reversed(verdicts):
        if passed:
            break
        n += 1
    return n


def _whole(v: Any) -> bool:
    return _num(v) and int(v) == v


def carried(history: Sequence[Report], tonight_night: date, tz: tzinfo | None = None) -> Carry:
    """Sticky arming's carriers: ``{case: (night, entry)}`` — each case's NEWEST
    listing in a history report's ``eval_case_persistent`` (module docstring).

    Per CASE (fix-round review D1): a newer report that omits the case — an
    earlier run of tonight's own night that saw it green, an UNKNOWN run with
    no list — never drops it; whether it STILL counts is :func:`_sticky`'s
    check against each night's LAST-run verdict. A malformed entry (a non-whole
    case or nights) is not a listing; a report filed after tonight's night
    (clock skew) never stands in.
    """
    out: Carry = {}
    for at, meta in history:  # oldest first: a newer listing replaces an older one
        night = night_of(run_start(at, meta), tz)
        entries = meta.get("eval_case_persistent")
        if night > tonight_night or not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, Mapping) and all(_whole(entry.get(k)) for k in ("case", "nights")):
                out[int(entry["case"])] = (night, entry)
    return out


def _sticky(
    series: Sequence[tuple[date, Mapping[str, Any]]], carry: Carry | None
) -> tuple[dict, dict]:
    """``({idx: persistent entry}, {idx: first-pass entry})`` carried over the window.

    A listed case counts only when every night from its listing's on (that
    night included, its LAST run deciding) failed it; tonight then decides —
    red: persistent, nights = listed + the red nights after + tonight; green: a
    first pass. A listing from tonight's own night (an earlier run) already
    counted tonight's night; one from a later night (skew) is ignored.
    """
    persistent: dict[int, dict[str, Any]] = {}
    first_pass: dict[int, dict[str, Any]] = {}
    tonight_night, tonight = series[-1]
    keys = list(_passed(tonight))
    for idx, (night0, entry) in (carry or {}).items():
        if not 1 <= idx <= len(keys) or night0 > tonight_night:
            continue
        key, nights = keys[idx - 1], int(entry["nights"])
        own = 1 if tonight_night > night0 else 0
        later = [
            (n, bool(_passed(m)[key])) for n, m in series[:-1] if n >= night0 and key in _passed(m)
        ]
        if any(ok for _n, ok in later):
            continue  # it passed on or after the listing's night: the window decides
        reds = sum(1 for n, _ok in later if n > night0)
        if _passed(tonight)[key]:
            if nights - (1 - own) + reds >= PERSIST_NIGHTS:
                first_pass[idx] = {"case": idx, "satisfier": _satisfier(tonight, key)}
            continue
        sat, since = entry.get("satisfier"), entry.get("since")
        persistent[idx] = {
            "case": idx,
            "satisfier": sat if isinstance(sat, int) and not isinstance(sat, bool) else None,
            "since": since if isinstance(since, str) else None,
            "nights": nights + reds + own,
        }
    return persistent, first_pass


def fold(
    series: Sequence[tuple[date, Mapping[str, Any]]], carry: Carry | None = None
) -> dict[str, list[dict[str, Any]]]:
    """``{'eval_case_persistent', 'eval_case_first_pass'}`` over a night series.

    The LAST entry is tonight; a case's 1-based index is its position in
    tonight's ``eval_case_passed``. A night whose report lacks the case's key
    is not part of that case's series (it neither counts nor resets). ``carry``
    (:func:`carried`: each case's newest listing) adds the sticky entries; the
    window's own entry wins, and both lists come out in golden order.
    """
    tonight = series[-1][1]
    persistent, first_pass = _sticky(series, carry)
    for idx, key in enumerate(_passed(tonight), start=1):
        seq = [(night, meta) for night, meta in series if key in _passed(meta)]
        verdicts = [bool(_passed(meta)[key]) for _night, meta in seq]
        reds = _trailing_reds(verdicts)
        if reds >= PERSIST_NIGHTS and reds < len(verdicts):  # an earlier pass precedes the streak
            persistent[idx] = {
                "case": idx,
                "satisfier": _satisfier(seq[-reds - 1][1], key),
                "since": seq[-reds][0].isoformat(),
                "nights": reds,
            }
        if verdicts[-1] and _trailing_reds(verdicts[:-1]) >= PERSIST_NIGHTS:
            first_pass[idx] = {"case": idx, "satisfier": _satisfier(tonight, key)}
    return {
        "eval_case_persistent": [persistent[i] for i in sorted(persistent)],
        "eval_case_first_pass": [first_pass[i] for i in sorted(first_pass)],
    }


def case_trips(
    counts: Mapping[str, Any],
    history: Sequence[Report],
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Tonight's persistence + first-pass lists; ``{}`` when tonight carries no verdicts.

    ``counts`` is tonight's run (``eval_case_passed`` / ``eval_case_satisfier``
    from ``kbworker.eval_cases.replay_keys``); ``history`` the same-epoch
    fallback-free reports from ``kbcore.eval_baseline.case_history``.
    """
    passed = counts.get("eval_case_passed")
    if not isinstance(passed, Mapping) or not passed:
        return {}
    tonight = {"eval_case_passed": passed, "eval_case_satisfier": counts.get("eval_case_satisfier")}
    zone = tz if tz is not None else NIGHT_TZ
    start = tonight_start(counts, now or _now())
    series = night_series(history, start, tonight, zone)
    return fold(series, carried(history, series[-1][0], zone))

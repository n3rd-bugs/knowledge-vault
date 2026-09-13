"""kbworker.weekly_fleet — the weekly report's fleet-flush block and push line (wave S).

Pure rendering over the window's nightly reports (``(created_at, meta)``
tuples, oldest first), the :mod:`kbworker.weekly_render` posture: no DB, no
push, pure ASCII, positive-only — a window whose reports carry no fleet key
renders NOTHING (an empty list keeps the caller's ``lines += ...`` /
``lines[:PUSH_MAX_LINES]`` shapes intact).

The per-box table SUMS every report in the window that carries ``fleet_flush``
(first real week of KB 34161, weekly 2026-09-13): it used to read the LAST such
report, and a resumed run (battery pause -> AC-restore) reports a short tiled
window, so every box showed rows 0 / MB 0.0 for the week. ``last`` is the age
of the box's max ``last_seen`` relative to the weekly's ``now``; ``hook_ver``
the latest non-empty stamp; ``tails`` from the last report. ``sess*`` is the
per-night session counts summed — distinct sessions cannot be recovered from
summaries, so a session spanning nights counts each night. The same sums read
a double-run day twice over its overlap: a nightly's window is at least 12 h
(``fleet_health.window_start``), so a second run the same day re-counts rows
the first one already reported.

The resend figure is the PEAK night, never a sum: each nightly re-reports the
lifetime tally of every session still resending in its window (see
:func:`kbcore.fleet_flush.resend_summary`), so summing seven nights would
count one session's 100 refusals as 700. The peak is the largest "refused to
date" reading the week saw — an upper bound on what any one night showed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from kbworker.digest_fleet_lines import (
    FLEET_LINE_BOXES,
    age_label,
    age_of,
    is_num,
    silent_host,
)

Report = tuple[datetime, dict[str, Any]]

REPAIR_CMD = "kb repair resend-dups --all --rows-only"
_HEADER = (
    f"  {'box':<18} {'rows':>5} {'MB':>7} {'sess*':>5} {'sweepMB':>7} {'last':>6} "
    f"{'tails':>5} {'hook_ver':<12} {'unstamped':>9}  labels"
)
SESS_NOTE = (
    "  * sess = per-night session counts summed (a session spanning nights counts each night;"
    " a double-run day counts its 12h-minimum-window overlap twice)"
)
_SUMMED = ("rows", "mb", "sessions", "sweep_recovered_mb", "unstamped")


def _aware(dt: datetime) -> datetime:
    """UTC-aware; a naive instant is read as UTC (Invariant 7 — never naive)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _last_with(reports: list[Report], key: str) -> Mapping[str, Any] | None:
    for _at, meta in reversed(reports):
        v = meta.get(key) if isinstance(meta, Mapping) else None
        if isinstance(v, Mapping) and v:
            return v
    return None


def _last_seen(at: datetime, d: Mapping[str, Any]) -> datetime | None:
    """A box's ``last_seen`` (ISO UTC), else the report instant minus its
    ``age_hours``; None when the entry says neither."""
    raw = d.get("last_seen")
    if isinstance(raw, str):
        try:
            return _aware(datetime.fromisoformat(raw))
        except ValueError:
            pass
    if is_num(d.get("age_hours")):
        return _aware(at) - timedelta(hours=float(d["age_hours"]))
    return None


def _boxes(reports: list[Report], now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Per-box window totals over EVERY report carrying ``fleet_flush``."""
    agg: dict[str, dict[str, Any]] = {}
    latest_at: datetime | None = None
    for at, meta in reports:
        raw = meta.get("fleet_flush") if isinstance(meta, Mapping) else None
        if not isinstance(raw, Mapping) or not raw:
            continue
        latest_at = at
        for box, d in raw.items():
            if not isinstance(d, Mapping):
                continue
            a = agg.setdefault(
                str(box),
                {k: 0 for k in _SUMMED}
                | {"last_seen": None, "hook_version": None, "labels": set()},
            )
            for k in _SUMMED:
                if is_num(d.get(k)):
                    a[k] += d[k]
            seen = _last_seen(at, d)
            if seen and (a["last_seen"] is None or seen > a["last_seen"]):
                a["last_seen"] = seen
            if d.get("hook_version"):
                a["hook_version"] = str(d["hook_version"])
            if isinstance(d.get("labels"), list):
                a["labels"].update(str(x) for x in d["labels"])
            a["tails_open"] = d.get("tails_open") or 0  # the LAST report's reading
    ref = _aware(now) if now else (_aware(latest_at) if latest_at else None)
    for a in agg.values():
        if a["last_seen"] is not None and ref is not None:
            a["age_hours"] = round((ref - a["last_seen"]).total_seconds() / 3600.0, 1)
        a["labels"] = sorted(a["labels"])
    return agg


def _refused_peak(reports: list[Report]) -> tuple[int, float]:
    """The night with the largest ``refused_rows`` (its rows and MB together)."""
    rows, mb = 0, 0.0
    for _at, meta in reports:
        s = meta.get("resend_summary") if isinstance(meta, Mapping) else None
        if not isinstance(s, Mapping) or not is_num(s.get("refused_rows")):
            continue
        if int(s["refused_rows"]) > rows:
            rows = int(s["refused_rows"])
            mb = float(s["refused_mb"]) if is_num(s.get("refused_mb")) else 0.0
    return rows, mb


def _row(box: str, d: Mapping[str, Any]) -> str:
    labels = d.get("labels")
    lab = ",".join(str(x) for x in labels) if isinstance(labels, list) and labels else "-"
    return (
        f"  {box:<18.18} {int(d.get('rows') or 0):>5} {float(d.get('mb') or 0):>7.1f} "
        f"{int(d.get('sessions') or 0):>5} {float(d.get('sweep_recovered_mb') or 0):>7.1f} "
        f"{age_label(d.get('age_hours')):>6} {int(d.get('tails_open') or 0):>5} "
        f"{str(d.get('hook_version') or '-'):<12.12} {int(d.get('unstamped') or 0):>9}  {lab}"
    )


def fleet_block(reports: list[Report], now: datetime | None = None) -> list[str]:
    """The report-body block: per-box window totals, the week's peak
    "refused to date" reading, and the paste-ready repair line when duplicate
    rows exist. ``now`` is the weekly's display instant the ages read from
    (None: the last fleet-carrying report's instant)."""
    boxes = _boxes(reports, now)
    if not boxes:
        return []
    lines = ["", "fleet flush (window totals; last = age of last flush):", _HEADER]
    lines += [_row(b, d) for b, d in sorted(boxes.items())]
    lines.append(SESS_NOTE)
    rows, mb = _refused_peak(reports)
    if rows:
        lines.append(
            f"  resend refused to date (peak night of {len(reports)}): {rows} rows / {mb:.1f} MB"
        )
    summary = _last_with(reports, "resend_summary") or {}
    dup = summary.get("duplicate_rows")
    if is_num(dup) and dup > 0:
        dmb = float(summary.get("duplicate_mb") or 0)
        lines.append(f"  duplicate rows on record: {int(dup)} ({dmb:.1f} MB) -> {REPAIR_CMD}")
    return lines


def push_line(reports: list[Report], now: datetime | None = None) -> list[str]:
    """At most ONE push line: ``fleet: 3 boxes; silent: rm1 4.1d, usama-home
    3.2d; stale hooks: 1; resend refused 1204 to date`` — segments
    positive-only; nothing without fleet data. Silence names HOSTS from the
    last fleet-carrying report, OLDEST first (the digest's order, so the ``+N``
    never hides the quietest machine), at most :data:`FLEET_LINE_BOXES`."""
    boxes = _boxes(reports, now)
    if not boxes:
        return []
    parts = [f"fleet: {len(boxes)} boxes"]
    _at, last = next(
        ((at, m) for at, m in reversed(reports) if isinstance(m.get("fleet_flush"), Mapping)),
        (None, {}),
    )
    silent = last.get("fleet_silent")
    if isinstance(silent, list) and silent:
        hosts = [(h, s) for s in silent if (h := silent_host(s))]
        hosts.sort(key=lambda hs: -age_of(hs[1]))
        shown = hosts[:FLEET_LINE_BOXES]
        named = ", ".join(f"{h} {age_label(s.get('age_hours'))}" for h, s in shown)
        extra = len(hosts) - len(shown)
        if named:
            parts.append(f"silent: {named}" + (f" +{extra}" if extra else ""))
    stale = last.get("fleet_stale_hooks")
    if isinstance(stale, Mapping) and isinstance(stale.get("boxes"), Mapping) and stale["boxes"]:
        parts.append(f"stale hooks: {len(stale['boxes'])}")
    rows, _mb = _refused_peak(reports)
    if rows:
        parts.append(f"resend refused {rows} to date")
    return ["; ".join(parts)]

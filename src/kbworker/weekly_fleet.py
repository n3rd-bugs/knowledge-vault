"""kbworker.weekly_fleet — the weekly report's fleet-flush block and push line (wave S).

Pure rendering over the window's nightly reports (``(created_at, meta)``
tuples, oldest first), the :mod:`kbworker.weekly_render` posture: no DB, no
push, pure ASCII, positive-only — a window whose reports carry no fleet key
renders NOTHING (an empty list keeps the caller's ``lines += ...`` /
``lines[:PUSH_MAX_LINES]`` shapes intact). The per-box table reads the
window's LAST report that carries ``fleet_flush``.

The resend figure is the PEAK night, never a sum: each nightly re-reports the
lifetime tally of every session still resending in its window (see
:func:`kbcore.fleet_flush.resend_summary`), so summing seven nights would
count one session's 100 refusals as 700. The peak is the largest "refused to
date" reading the week saw — an upper bound on what any one night showed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from kbworker.digest_fleet_lines import age_label, is_num

Report = tuple[datetime, dict[str, Any]]

REPAIR_CMD = "kb repair resend-dups --all --rows-only"
_HEADER = (
    f"  {'box':<18} {'rows':>5} {'MB':>7} {'sess':>4} {'sweepMB':>7} {'last':>6} "
    f"{'tails':>5} {'hook_ver':<12} {'unstamped':>9}  labels"
)


def _last_with(reports: list[Report], key: str) -> Mapping[str, Any] | None:
    for _at, meta in reversed(reports):
        v = meta.get(key) if isinstance(meta, Mapping) else None
        if isinstance(v, Mapping) and v:
            return v
    return None


def _boxes(reports: list[Report]) -> dict[str, Mapping[str, Any]]:
    raw = _last_with(reports, "fleet_flush") or {}
    return {str(k): v for k, v in raw.items() if isinstance(v, Mapping)}


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
        f"{int(d.get('sessions') or 0):>4} {float(d.get('sweep_recovered_mb') or 0):>7.1f} "
        f"{age_label(d.get('age_hours')):>6} {int(d.get('tails_open') or 0):>5} "
        f"{str(d.get('hook_version') or '-'):<12.12} {int(d.get('unstamped') or 0):>9}  {lab}"
    )


def fleet_block(reports: list[Report]) -> list[str]:
    """The report-body block: per-box table from the last nightly, the week's
    peak "refused to date" reading, and the paste-ready repair line when
    duplicate rows exist."""
    boxes = _boxes(reports)
    if not boxes:
        return []
    lines = ["", "fleet flush (last nightly's tiled window; last = age of last flush):", _HEADER]
    lines += [_row(b, d) for b, d in sorted(boxes.items())]
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


def push_line(reports: list[Report]) -> list[str]:
    """At most ONE push line: ``fleet: 3 boxes; silent: rm1 4.1d; stale hooks: 1;
    resend refused 1204 to date`` — segments positive-only; nothing without
    fleet data."""
    boxes = _boxes(reports)
    if not boxes:
        return []
    parts = [f"fleet: {len(boxes)} boxes"]
    _at, last = next(
        ((at, m) for at, m in reversed(reports) if isinstance(m.get("fleet_flush"), Mapping)),
        (None, {}),
    )
    silent = last.get("fleet_silent")
    if isinstance(silent, list) and silent:
        named = ", ".join(
            f"{s.get('name', s.get('box'))} {age_label(s.get('age_hours'))}"
            for s in silent
            if isinstance(s, Mapping)
        )
        parts.append(f"silent: {named}")
    stale = last.get("fleet_stale_hooks")
    if isinstance(stale, Mapping) and isinstance(stale.get("boxes"), Mapping) and stale["boxes"]:
        parts.append(f"stale hooks: {len(stale['boxes'])}")
    rows, _mb = _refused_peak(reports)
    if rows:
        parts.append(f"resend refused {rows} to date")
    return ["; ".join(parts)]

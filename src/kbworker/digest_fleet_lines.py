"""kbworker.digest_fleet_lines — the nightly digest's fleet-flush lines (wave S, S4).

Pure rendering over the run's counts (``fleet_flush``, ``fleet_silent``,
``fleet_stale_hooks``, ``resend_summary``, ``ingest_queue_pending``), the
digest's own posture: positive-only (a key the run does not carry, or an empty
one, contributes NO line and never touches ``counts``), tolerant of absent or
odd shapes (an old report must cost a line, never the digest), pure ASCII
(ntfy to a phone). Knobs are read lazily; the two kbcore CONSTANTS the lines
depend on (the queue cap, the stale-projects rule) are imported at the top so
a relocation fails loudly at import time instead of silently deleting an alert.

ALERT tier (:func:`fleet_alert_lines`, rendered where the old stale-hook line
stood): ONE merged ``STALE HOOKS`` line, ``FLEET SILENT``, ``RESEND GUARD``
above ``KB_RESEND_ALERT_PER_NIGHT``, ``QUEUE DEPTH`` at 80% of the cap.
FACT tier (:func:`fleet_fact_lines`): the ``fleet:`` age line FIRST, then the
resend fact (when it is not already an alert), open tails, sweep recovery.

The resend figures are LIFETIME tallies of the sessions still resending in the
window (the ledger is one UPSERTed row per session, never a per-night delta),
so every rendering says "to date"; the knob's threshold is read the same way.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kbcore.fleet_health import stale_hook_projects
from kbcore.ingest_queue import MAX_PENDING

FLEET_LINE_BOXES = 4  # boxes named on the fleet line before "+N"
QUEUE_DEPTH_ALERT_SHARE = 0.8  # of ingest_queue.MAX_PENDING


def is_num(v: Any) -> bool:
    """A real number (bool excluded) — the shape gate for every rendered figure."""
    return isinstance(v, int | float) and not isinstance(v, bool)


def age_label(hours: Any) -> str:
    """``2.1h`` under three days, ``4.1d`` beyond — the line reads as an age."""
    h = float(hours) if is_num(hours) else 0.0
    return f"{h:.1f}h" if h < 72 else f"{h / 24:.1f}d"


def _by_box(counts: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = counts.get("fleet_flush")
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, Mapping)}


# --- alert tier -------------------------------------------------------------


def fleet_alert_lines(counts: Mapping[str, Any]) -> list[str]:
    """The fleet lines that must survive a truncated phone preview."""
    lines: list[str] = []
    for line in (
        stale_hooks_line(counts),
        fleet_silent_line(counts.get("fleet_silent")),
        resend_guard_alert(counts.get("resend_summary")),
        queue_depth_line(counts.get("ingest_queue_pending")),
    ):
        if line:
            lines.append(line)
    return lines


def stale_hooks_line(counts: Mapping[str, Any]) -> str | None:
    """ONE merged line: groups (``principal`` or ``principal/label``) whose
    stamped hook version is not the served manifest (``fleet_stale_hooks``) and
    projects still submitting rows with no session id (the pre-stamp signal,
    ``fleet_degenerate_by_project``). The legacy text is kept verbatim when
    only the legacy signal fires."""
    suspected = stale_hook_projects(counts.get("fleet_degenerate_by_project"))
    stale = counts.get("fleet_stale_hooks")
    boxes = stale.get("boxes") if isinstance(stale, Mapping) else None
    if isinstance(boxes, Mapping) and boxes:
        named = ", ".join(f"{b} {v}" for b, v in sorted(boxes.items()))
        line = f"STALE HOOKS: {named} -- server {stale.get('server', '?')}"
        if suspected:
            line += f"; suspected (no session id): {', '.join(suspected)}"
        return line
    if suspected:
        return f"STALE HOOKS suspected: {', '.join(suspected)}"
    return None


def fleet_silent_line(silent: Any) -> str | None:
    """``FLEET SILENT: rm1 (last flush 4.1d ago, 12 sessions/30d)``."""
    if not isinstance(silent, list):
        return None
    parts = [
        f"{s.get('name', s.get('box'))} (last flush {age_label(s.get('age_hours'))} ago, "
        f"{int(s.get('sessions_30d') or 0)} sessions/30d)"
        for s in silent
        if isinstance(s, Mapping) and (s.get("name") or s.get("box"))
    ]
    return f"FLEET SILENT: {', '.join(parts)}" if parts else None


def _refused(summary: Any) -> tuple[int, float, list[str]]:
    if not isinstance(summary, Mapping):
        return 0, 0.0, []
    rows = summary.get("refused_rows")
    mb = summary.get("refused_mb")
    boxes = summary.get("boxes")
    return (
        int(rows) if is_num(rows) else 0,
        float(mb) if is_num(mb) else 0.0,
        [str(b) for b in boxes] if isinstance(boxes, list) else [],
    )


def _alert_threshold() -> int:
    try:
        from kbcore.config import settings

        return int(settings.kb_resend_alert_per_night)
    except Exception:  # noqa: BLE001 — a knob may never cost the digest
        return 50


def resend_guard_alert(summary: Any) -> str | None:
    """``RESEND GUARD: refused 212 rows/13.4 MB to date from rm1 - a box is
    resending; check its offsets`` — above ``KB_RESEND_ALERT_PER_NIGHT`` only
    (the tally is lifetime-per-session, so the threshold reads "to date")."""
    rows, mb, boxes = _refused(summary)
    if rows <= _alert_threshold():
        return None
    src = ", ".join(boxes) if boxes else "(unknown)"
    return (
        f"RESEND GUARD: refused {rows} rows/{mb:.1f} MB to date from {src}"
        " - a box is resending; check its offsets"
    )


def queue_depth_line(pending: Any) -> str | None:
    """``QUEUE DEPTH: 4123 pending (82% of 5000 cap) - the drain is falling behind``."""
    if not is_num(pending):
        return None
    if not MAX_PENDING or pending < QUEUE_DEPTH_ALERT_SHARE * MAX_PENDING:
        return None
    pct = 100.0 * float(pending) / MAX_PENDING
    return (
        f"QUEUE DEPTH: {int(pending)} pending ({pct:.0f}% of {MAX_PENDING} cap)"
        " - the drain is falling behind"
    )


# --- fact tier --------------------------------------------------------------


def fleet_fact_lines(counts: Mapping[str, Any]) -> list[str]:
    """The routine fleet facts, ``fleet:`` first."""
    lines: list[str] = []
    for line in (
        fleet_line(counts),
        resend_guard_fact(counts.get("resend_summary")),
        tails_line(counts),
        sweep_line(counts),
    ):
        if line:
            lines.append(line)
    return lines


def fleet_line(counts: Mapping[str, Any]) -> str | None:
    """``fleet: usama-server 2.1h, rm1 9.4h, win-laptop 31.0h`` (freshest first,
    at most :data:`FLEET_LINE_BOXES`, then ``+N``)."""
    boxes = _by_box(counts)
    if not boxes:
        return None
    aged = sorted(boxes.items(), key=lambda kv: float(kv[1].get("age_hours") or 0))
    shown = [f"{b} {age_label(d.get('age_hours'))}" for b, d in aged[:FLEET_LINE_BOXES]]
    extra = len(aged) - len(shown)
    return f"fleet: {', '.join(shown)}" + (f" +{extra}" if extra else "")


def resend_guard_fact(summary: Any) -> str | None:
    """``resend guard: 41 refused to date (3 sessions; boxes: usama-server);
    overlap rows 1`` — the server deploy's proof of life while old hooks still
    resend. Rendered only when the same numbers did not already make the alert
    tier."""
    if not isinstance(summary, Mapping):
        return None
    rows, _mb, boxes = _refused(summary)
    bits: list[str] = []
    if rows and rows <= _alert_threshold():
        sessions = summary.get("sessions")
        s = f"{int(sessions)} sessions" if is_num(sessions) else "sessions ?"
        bits.append(f"{rows} refused to date ({s}; boxes: {', '.join(boxes) or '(unknown)'})")
    overlap = summary.get("overlap_rows")
    if is_num(overlap) and overlap > 0:
        bits.append(f"overlap rows {int(overlap)}")
    return f"resend guard: {'; '.join(bits)}" if bits else None


def tails_line(counts: Mapping[str, Any]) -> str | None:
    """``flush tails >24h: 2 sessions (largest 171.3 MB, aquamonix 7c26902a)``."""
    total, largest = 0, None
    for d in _by_box(counts).values():
        n = d.get("tails_open")
        if not (is_num(n) and n > 0):
            continue
        total += int(n)
        top = d.get("largest_tail")
        if isinstance(top, Mapping) and is_num(top.get("mb")):
            if largest is None or float(top["mb"]) > float(largest["mb"]):
                largest = top
    if not total:
        return None
    try:
        from kbcore.config import settings

        hours = int(settings.kb_fleet_tail_hours)
    except Exception:  # noqa: BLE001 — a knob may never cost the digest
        hours = 24
    line = f"flush tails >{hours}h: {total} sessions"
    if largest is not None:
        who = f"{largest.get('project')} {largest.get('sid8')}"
        line += f" (largest {float(largest['mb']):.1f} MB, {who})"
    return line


def sweep_line(counts: Mapping[str, Any]) -> str | None:
    """``sweep: 3 sessions / 12.4 MB recovered (usama-server 2, rm1 1)``."""
    per_box: list[tuple[str, int]] = []
    mb = 0.0
    for box, d in _by_box(counts).items():
        n = d.get("sweep_sessions")
        if is_num(n) and n > 0:
            per_box.append((box, int(n)))
            mb += float(d.get("sweep_recovered_mb") or 0)
    if not per_box:
        return None
    sessions = sum(n for _b, n in per_box)
    who = ", ".join(f"{b} {n}" for b, n in sorted(per_box, key=lambda x: (-x[1], x[0])))
    return f"sweep: {sessions} sessions / {mb:.1f} MB recovered ({who})"

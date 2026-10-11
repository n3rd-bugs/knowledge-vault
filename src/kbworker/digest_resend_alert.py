"""kbworker.digest_resend_alert — the nightly digest's RESEND ALERT lines (wave C.2).

Split from :mod:`kbworker.digest_fleet_lines` (ratchet: that file sits near the
300-line cap). Incident KB 49064: a session resumed from another directory leaves
a stale transcript copy the sweep re-sent from byte 0 — 282 MB that the hash guard
could not see (new chunk boundaries) and that rendered only as the quiet
``overlap rows N`` fact. :func:`kbcore.fleet_flush.overlap_alerts` now names, per
``(principal, session)``, the stamped rows whose bytes that principal had ALREADY
covered, and only those over ``KB_RESEND_ALERT_MB`` / ``KB_RESEND_ALERT_ROWS``
(either knob ``<= 0`` disables its criterion; both, the alert). Each renders as
ONE ALERT-class line (placed with the fleet alerts, so the 20-line cap never
trims it) that names the owner repair with ``--since`` = the session's FIRST
unrepaired full-overlap row ever (``since``; ``first_at`` — tonight's first — for
an older report), so every night's alert for one incident offers the same instant;
the repair's dry run prints W0 (its first re-sent row): a W0 later than ``--since``
means the dry run is re-run with ``--since <W0>`` before ``--apply``. Telemetry
only — proposal_v2 amendment 3: nothing refuses on a stamp.

The digest posture holds: pure rendering over the report's counts, positive-only
(no key, an empty list or an odd shape -> no line, never an exception), pure ASCII
(ntfy to a phone); every field is re-sanitised here so an old or odd report can
never put a non-ASCII byte or a shell metacharacter on the line — an unsafe
character becomes ``_``, never ``?``, ``*``, ``[`` or ``]`` (glob characters in a
command the owner copy-pastes).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

RESEND_ALERT_LINES = 3  # sessions named one line each; more collapse into a "+N" line
_PRINCIPAL_MAX = 64
_UNSAFE = re.compile(r"[^A-Za-z0-9@._:/+-]")  # each unsafe character becomes '_'
_SINCE_PLACEHOLDER = "FIRST_RESENT_ROW_CREATED_AT"  # an older report with no instant


def _safe(value: Any, limit: int = _PRINCIPAL_MAX) -> str:
    """ASCII, no whitespace, shell metacharacters or glob characters ('_'), clipped."""
    return _UNSAFE.sub("_", str(value))[:limit]


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _line(entry: Mapping[str, Any]) -> str | None:
    sid = _safe(entry.get("sid8") or "", 8)
    mb, rows = _num(entry.get("mb")), _num(entry.get("rows"))
    if not sid or mb is None or rows is None:
        return None
    principal = _safe(entry.get("principal") or "(unknown)")
    since = _safe(entry.get("since") or entry.get("first_at") or _SINCE_PLACEHOLDER, 40)
    return (
        f"RESEND ALERT: {principal} session {sid} re-sent {mb:.1f} MB already covered "
        f"({int(rows)} rows) -- check the box's flush log for 'shrank'/'stale copy'; "
        "owner repair (dry run first; --since is the session's first unrepaired full-overlap "
        "row; if the dry run's W0 is later than --since, re-run the dry run with --since <W0> "
        "before --apply): "
        f"kb repair resend-dups --session {sid} --replayed --since {since}"
    )


def resend_alert_lines(summary: Any) -> list[str]:
    """One ``RESEND ALERT`` line per session in ``resend_summary.overlap_alerts``
    (largest first, as kbcore orders them), at most :data:`RESEND_ALERT_LINES`,
    then ``RESEND ALERT: +N more ...`` when more crossed the threshold."""
    if not isinstance(summary, Mapping):
        return []
    raw = summary.get("overlap_alerts")
    if not isinstance(raw, list):
        return []
    lines = [ln for e in raw if isinstance(e, Mapping) and (ln := _line(e))]
    shown = lines[:RESEND_ALERT_LINES]
    if len(lines) > len(shown):
        shown.append(
            f"RESEND ALERT: +{len(lines) - len(shown)} more session(s) over the threshold "
            "-- see resend_summary.overlap_alerts in the run report"
        )
    return shown

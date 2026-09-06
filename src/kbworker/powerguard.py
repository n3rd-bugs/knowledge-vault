"""kbworker.powerguard — the house-power pause signal for the drain.

Split from ``kbworker.consolidate`` (mechanical move, 2026-08-21, review #5):
that file sat 3 lines under its ratchet ceiling and the nightly needed room for
host telemetry and the rerank probe. Both names are re-imported by
``consolidate`` (the established seam), so every call site and the test
suite's monkeypatches keep resolving through ``consolidate``'s globals —
``should_stop`` reads the global ``_on_battery``, so patching
``consolidate._on_battery`` still works exactly as before.

AC-RESTORE TRIGGER (owner request 2026-08-29, after the 02:02 battery pause
left 41 pending rows waiting a full day for the next nightly): this module now
also owns the pause MARKER (:func:`record_battery_pause`, called once from
``consolidate.main()``'s tail) and the three-way grid probe
(:func:`grid_state` / ``python -m kbworker.powerguard``) that
``scripts/ac_restore.sh`` — fired by ``kb-ac-restore.timer`` every 5 minutes —
uses to restart ``kb-consolidate.service`` once the grid has held steady.

SIGNAL FIX (review #8 H2, 2026-09-03): readings come from the BATTERY inverter
only (``monitor_inverter.has_battery = 1``, filtered BEFORE the window is cut)
and the verdict is :func:`kbworker.power_rules.window_verdict` — the any-row
voltage floor plus a >= 3 CONSECUTIVE-row off-grid rule (never any-row). Every
Wave G pause was inverter #2's 0 V night standby; the real ~237 V outages were
never seen. The measured populations and the rationale live in power_rules.

Same logger name as the parent on purpose: these lines have always appeared in
journald under ``kbworker.consolidate`` and grep recipes depend on that.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kbcore.config import settings
from kbworker.power_rules import window_verdict

logger = logging.getLogger("kbworker.consolidate")

# Basename of the pause marker under KB_STATE_DIR. scripts/ac_restore.sh derives
# the same path from the same knob — the filename is part of that contract.
BATTERY_PAUSE_MARKER = "battery-pause.json"


def _reading_age_seconds(ts: Any) -> float | None:
    """Age of one sungrow reading in seconds; None when the stamp is unparseable.

    INVARIANT 7 CARVE-OUT — the single foreign-DB boundary in this codebase.
    Everything the KB itself stores is TIMESTAMPTZ/UTC, but the sungrow monitor
    is another project's Django app and writes NAIVE LOCAL wall-clock
    (Asia/Karachi, UTC+5). Reading those stamps as UTC made ``now - seen`` about
    MINUS five hours, so the staleness check could not fire during the first five
    hours of genuine staleness: a poller that died at 03:00 still read as "fresh"
    at 07:00, which is exactly the last-known-good trap the freshness check
    exists to avoid.

    A naive stamp is therefore interpreted in the LOCAL zone — which is what
    :meth:`datetime.astimezone` does to a naive value — and an offset-carrying
    stamp is honoured as written, so a future sungrow schema that starts emitting
    UTC needs no change here. The comparison itself stays UTC-aware.
    """
    try:
        seen = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if seen.tzinfo is None:
        seen = seen.astimezone()  # naive == LOCAL time (Inv. 7 carve-out, above)
    return (datetime.now(UTC) - seen).total_seconds()


def _grid_rows() -> list[Any]:
    """The newest ``kb_power_window`` rows of the BATTERY inverter, newest first.

    Rows are ``(grid_voltage, timestamp, inverter_id, raw_data)``. The
    ``has_battery = 1`` filter (the same self-describing key sungrow's own
    ``_check_grid_state`` uses) is applied BEFORE the window is cut, so 5 rows
    are ~5 minutes of ONE poller (~60 s cadence) and never a blend; if that
    poller dies its newest row is simply stale, which both callers already
    treat as fail-safe. The single sungrow read, shared by :func:`_on_battery`
    and :func:`grid_state` so the two can never drift apart. Policy stays with
    the callers: this raises on an unreadable DB (a schema without the inverter
    table included — there is deliberately NO blend-everything fallback) and
    does not check that ``kb_power_db`` is configured at all — what ignorance
    MEANS (fail-open, fail-safe, or 'unknown') is the one thing the two callers
    decide differently, on purpose.
    """
    import sqlite3

    path = (settings.kb_power_db or "").strip()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
        return conn.execute(
            "SELECT r.grid_voltage, r.timestamp, r.inverter_id, r.raw_data "
            "FROM monitor_inverterreading AS r "
            "JOIN monitor_inverter AS i ON i.id = r.inverter_id "
            "WHERE i.has_battery = 1 "
            "ORDER BY r.timestamp DESC LIMIT ?",
            (max(1, int(settings.kb_power_window)),),
        ).fetchall()


def _on_battery() -> bool:
    """True when the house is running on battery, so the drain should stand down.

    The GPU is the largest load on this box and the UPS could not ride out a grid
    fluctuation (2026-07-30, power lost mid-run). Hours of inference during an
    outage is the wrong thing to be doing, so the drain stops between cohorts and
    resumes when mains returns — the same stop-and-resume path as the deadline,
    which needs no state of its own because unreached rows stay 'pending'.

    Signal is the sungrow monitor DB, read-only, BATTERY inverter only (the
    219,304-reading threshold table blended both inverters, and every Wave G
    pause was the battery-less #2's night standby). Two rules, shared with
    :func:`grid_state` via ``power_rules.window_verdict``: any row under
    ``kb_power_min_grid_volts`` (normal 213-256V, outages cluster below 50V, the
    50-150V band holds ~170 readings, so 200 sits in a sparse gap), or >= 3
    CONSECUTIVE off-grid rows (importing=0 exporting=0 discharging=1 at 50.0 Hz
    — the real ~237 V outages the floor never saw). The log line names the
    inverter and the rule that fired.

    FAIL-SAFE ON UNKNOWN-BUT-CONFIGURED STATE. A MISSING or unreadable power DB
    fails OPEN (returns False, warns): the feature is simply not configured on
    that box, and a pipeline that silently never runs is the worse failure there.
    But a DB that IS configured whose newest reading is STALE fails SAFE (returns
    True, ON BATTERY): a reading older than the freshness window right after a
    grid outage is exactly when the sungrow poller is most likely down, so the ONE
    time staleness matters the guard used to say 'mains' and kept the GPU running
    into the outage. A stale value is not evidence the grid is up; on a UPS that
    cannot ride out a fluctuation, pausing the GPU when house-power state is
    unknown-but-configured is the safe default. Both branches warn loudly.
    """
    if not (settings.kb_power_db or "").strip():
        return False
    try:
        rows = _grid_rows()
        row = rows[0] if rows else None
    except Exception as e:  # noqa: BLE001 — another project's DB; never fatal here
        logger.warning(
            "power check unavailable (%s: %s) — proceeding on mains assumption", type(e).__name__, e
        )
        return False
    if not row or row[0] is None:
        logger.warning("power check found no readings — proceeding on mains assumption")
        return False

    volts, ts, inverter = float(row[0]), row[1], row[2]
    age = _reading_age_seconds(ts)
    if age is None:
        logger.warning("power check: unparseable timestamp %r — ignoring freshness", ts)

    if age is not None and age > settings.kb_power_max_age_seconds:
        logger.warning(
            "power check STALE (%.0fs old, grid=%.1fV, inverter %s) — treating as ON BATTERY and "
            "pausing; the sungrow poller may be down, and a last-known-good value is not evidence "
            "the grid is up. Pausing the GPU is the safe default on a UPS that cannot ride out a "
            "fluctuation; rows stay 'pending' and the next run resumes here",
            age,
            volts,
            inverter,
        )
        return True

    # Any-row voltage floor, then >= 3 consecutive off-grid rows — the SAME
    # verdict grid_state() uses (kbworker.power_rules.window_verdict).
    verdict = window_verdict(rows, settings.kb_power_min_grid_volts)
    if verdict is None:
        return False
    rule, detail = verdict
    logger.warning(
        "ON BATTERY (inverter %s, rule=%s: %s) — drain pausing; rows stay 'pending' and the run "
        "resumes on grid restore (kb-ac-restore.timer) or the next scheduled run",
        inverter,
        rule,
        detail,
    )
    return True


def grid_state() -> str:
    """``'grid'`` / ``'battery'`` / ``'unknown'`` — the AC-restore trigger's probe.

    Where :func:`_on_battery` must fold ignorance into a drain verdict (fail
    OPEN when unconfigured, fail SAFE when stale), a trigger that RESTARTS the
    GPU must never act on ignorance at all: an unconfigured/unreadable power DB,
    no readings, or a reading older than ``kb_power_max_age_seconds`` (checked
    via :func:`_reading_age_seconds`, so the naive-local carve-out applies) all
    come back ``'unknown'``, and ``scripts/ac_restore.sh`` simply keeps waiting.
    ``'grid'`` requires the WHOLE window healthy under the guard's own verdict
    (``power_rules.window_verdict``: any-dip floor + consecutive off-grid rows),
    so the trigger will not resume into a flickering supply either.
    """
    if not (settings.kb_power_db or "").strip():
        return "unknown"
    try:
        rows = [r for r in _grid_rows() if r[0] is not None]
    except Exception as e:  # noqa: BLE001 — another project's DB; never fatal here
        logger.warning("power probe unreadable (%s: %s) — reporting unknown", type(e).__name__, e)
        return "unknown"
    if not rows:
        return "unknown"
    age = _reading_age_seconds(rows[0][1])
    if age is None or age > settings.kb_power_max_age_seconds:
        return "unknown"
    return "battery" if window_verdict(rows, settings.kb_power_min_grid_volts) else "grid"


def battery_pause_marker_path() -> Path:
    """Where the battery-pause marker lives: ``$KB_STATE_DIR/battery-pause.json``.

    ``scripts/ac_restore.sh`` derives the SAME path from ``KB_STATE_DIR`` in
    ``.env`` (default ``~/kb/state``) — the knob and the basename are the
    contract between the Python side that arms the trigger and the shell side
    that fires it.
    """
    return Path(settings.kb_state_dir).expanduser() / BATTERY_PAUSE_MARKER


def record_battery_pause(
    counts: Mapping[str, Any], marker: Path | None = None
) -> dict[str, Any] | None:
    """Arm (or disarm) the AC-restore trigger from a finished run's counts.

    Called ONCE from the tail of ``consolidate.main()``, right after
    ``counts['stopped']`` is recorded. If any step paused with reason
    ``'battery'``, a JSON marker (``tail_ops.pause_payload`` — ``'at'`` is the
    run's FIRST battery stand-down, review #9a INS-2) is written ATOMICALLY
    (tmp + rename — kb-ac-restore.timer polls this path
    every 5 minutes and must never read a half-written file). Otherwise the run
    completed with no battery pause, so the marker is removed if present: a
    clean finish means nothing is waiting, and it is the ONLY thing that
    disarms the trigger — ``ac_restore.sh`` deliberately never removes the
    marker itself, so a resume cut short by another outage stays armed.

    ``marker`` is an injectable seam for tests; reason strings are data
    (``'battery'`` verbatim, from the stop log) and never renamed. Fail-open
    both ways: a marker that cannot be written or removed logs and never fails
    the run whose report is about to be pushed.

    Returns the PRIOR marker's payload (``{'at', 'deferred', ...}``) when this
    run found one — i.e. this run is the RESUME of a battery pause — else ``None``
    (review #8 H7d: the digest names a resumed run as such). Read before the
    marker is rewritten or cleared; a malformed marker reads as ``None``.
    """
    marker = battery_pause_marker_path() if marker is None else marker
    prior: dict[str, Any] | None = None
    try:
        loaded = json.loads(marker.read_text(encoding="utf-8"))
        prior = loaded if isinstance(loaded, dict) else None
    except (OSError, ValueError):
        prior = None
    stopped = counts.get("stopped") or {}
    battery = [
        v for v in stopped.values() if isinstance(v, Mapping) and v.get("reason") == "battery"
    ]
    try:
        if not battery:
            marker.unlink(missing_ok=True)
            return prior
        total = 0
        for entry in battery:
            try:
                total += int(entry.get("deferred") or 0)
            except (TypeError, ValueError):
                pass  # a malformed count must not cost the marker
        from kbworker.stop_log import pause_payload  # lazy: the CLI probe stays light

        payload = pause_payload(total)
        marker.parent.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_name(marker.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(marker)  # atomic on POSIX — the poller never sees half a file
        logger.warning(
            "battery pause recorded (%d item(s) deferred) — kb-ac-restore.timer resumes the "
            "run once the grid holds, else the next scheduled run picks it up",
            total,
        )
    except Exception:  # noqa: BLE001 — the trigger may never fail the run's report
        logger.exception("battery-pause marker update failed (%s)", marker)
    return prior


def _cli() -> int:
    """``python -m kbworker.powerguard``: print the state word, exit by state.

    Exit codes 0 = grid, 1 = battery, 2 = unknown — but ``ac_restore.sh``
    parses the WORD on stdout (a crashed interpreter and 'battery' must not
    look alike), so both surfaces carry the same answer. Deliberately
    dependency-light: stdlib + kbcore.config only, no DB pool, no Ollama.
    """
    state = grid_state()
    print(state)
    return {"grid": 0, "battery": 1}.get(state, 2)


if __name__ == "__main__":
    raise SystemExit(_cli())

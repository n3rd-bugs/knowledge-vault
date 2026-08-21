"""kbworker.powerguard — the house-power stand-down signal for the drain.

Split from ``kbworker.consolidate`` (mechanical move, 2026-08-21, review #5):
that file sat 3 lines under its ratchet ceiling and the nightly needed room for
host telemetry and the rerank probe. Both names are re-imported by
``consolidate`` (the established seam), so every call site and the test
suite's monkeypatches keep resolving through ``consolidate``'s globals —
``should_stop`` reads the global ``_on_battery``, so patching
``consolidate._on_battery`` still works exactly as before.

Same logger name as the parent on purpose: these lines have always appeared in
journald under ``kbworker.consolidate`` and grep recipes depend on that.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from kbcore.config import settings

logger = logging.getLogger("kbworker.consolidate")


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


def _on_battery() -> bool:
    """True when the house is running on battery, so the drain should stand down.

    The GPU is the largest load on this box and the UPS could not ride out a grid
    fluctuation (2026-07-30, power lost mid-run). Hours of inference during an
    outage is the wrong thing to be doing, so the drain stops between cohorts and
    resumes when mains returns — the same stop-and-resume path as the deadline,
    which needs no state of its own because unreached rows stay 'pending'.

    Signal is grid_voltage from the sungrow monitor DB, read-only. Measured over
    219,304 readings: normal 213-256V, outages cluster below 50V, and the 50-150V
    band holds only ~170 readings — so the threshold sits in a sparse gap rather
    than near either mode.

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
    path = (settings.kb_power_db or "").strip()
    if not path:
        return False
    try:
        import sqlite3

        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
            rows = conn.execute(
                "SELECT grid_voltage, timestamp FROM monitor_inverterreading "
                "ORDER BY timestamp DESC LIMIT ?",
                (max(1, int(settings.kb_power_window)),),
            ).fetchall()
            row = rows[0] if rows else None
    except Exception as e:  # noqa: BLE001 — another project's DB; never fatal here
        logger.warning(
            "power check unavailable (%s: %s) — proceeding on mains assumption", type(e).__name__, e
        )
        return False
    if not row or row[0] is None:
        logger.warning("power check found no readings — proceeding on mains assumption")
        return False

    volts, ts = float(row[0]), row[1]
    age = _reading_age_seconds(ts)
    if age is None:
        logger.warning("power check: unparseable timestamp %r — ignoring freshness", ts)

    if age is not None and age > settings.kb_power_max_age_seconds:
        logger.warning(
            "power check STALE (%.0fs old, grid=%.1fV) — treating as ON BATTERY and standing "
            "down; the sungrow poller may be down, and a last-known-good value is not evidence "
            "the grid is up. Pausing the GPU is the safe default on a UPS that cannot ride out a "
            "fluctuation; rows stay 'pending' and the next run resumes here",
            age,
            volts,
        )
        return True

    # ANY dip in the window keeps the GPU down: resuming into a flickering supply
    # is worse than staying paused a few minutes longer, and a sagging grid is
    # exactly when the UPS can least absorb a 150W spike.
    floor = min(float(r[0]) for r in rows if r[0] is not None)
    if floor < settings.kb_power_min_grid_volts:
        volts = floor
        logger.warning(
            "ON BATTERY (grid=%.1fV < %.1fV) — drain standing down; rows stay 'pending' "
            "and the next run resumes here",
            volts,
            settings.kb_power_min_grid_volts,
        )
        return True
    return False

"""kbworker.power_rules — the pure ON-BATTERY rules over one inverter's window.

Split out of ``kbworker.powerguard`` (review #8 H2, 2026-09-03) so the guard
stays under the size ratchet and the rules can be unit-tested without a DB.
Everything here is a pure function over ``(grid_voltage, timestamp,
inverter_id, raw_data)`` rows of the BATTERY inverter, newest first, exactly as
``powerguard._grid_rows`` returns them. ``powerguard._on_battery`` and
``powerguard.grid_state`` both call :func:`window_verdict`, which is what makes
their semantics identical by construction rather than by discipline.

WHY TWO RULES. The '<200 V any-row' floor used to read BOTH inverters blended,
and every Wave G pause (08-29 01:59, 08-29 21:23, 08-30 00:58, 08-30 01:20,
09-01 01:46) was inverter #2 (6 kVA, no battery) parked at 0 V in night
standby. The REAL outages — ~200 run-minutes since 08-01, the daily 20:15-21:20
load-shedding span — hold ~237 V on the 10 kVA UPS inverter, because in
off-grid mode the inverter makes its own mains: the floor never saw one. What
those rows DO carry, in ``raw_data``, is the inverter's own status word:
importing=0, exporting=0, discharging=1 at EXACTLY 50.0 Hz (grid-tied rows
jitter 49.7-50.4).

NEVER ANY-ROW. Grid-tied evening discharge (the 19:00 hour) shows the same
signature for a row or two: 37 single and 14 double matches since 08-01, all
of them grid (incl. 09-01 19:00:36). Real off-grid runs are 32-282 rows long.
Requiring ``OFFGRID_CONSECUTIVE_ROWS`` (3) CONSECUTIVE matches separates the
two populations with no overlap in the measured history; an any-row rule would
mint a new phantom class. Real onsets DO dip (09-02 20:35-36, 08-28 23:11-12
read 0 V for a row or two), so the any-row voltage floor stays as the fast path.

Not knobs on purpose: ``kbcore.config`` sits at its ratchet ceiling, and these
are properties of THIS inverter's firmware. Re-derive the threshold table on
inverter 1 alone (config.py's 219,306-reading table blended both inverters)
before tuning either constant. Owner note (cross-project): if the sungrow
poller ever persists ``running_state`` (bit 0x1000 'Running in off-grid mode'),
that authoritative bit should replace this signature.
"""

from __future__ import annotations

import json
from typing import Any

OFFGRID_CONSECUTIVE_ROWS = 3
OFFGRID_FREQUENCY_HZ = 50.0
# The poller stores one decimal; 50.0 vs 50.1 IS the signal, so the tolerance
# only absorbs float noise and can never admit a neighbouring reading.
OFFGRID_FREQUENCY_TOLERANCE_HZ = 0.05

# Row layout shared with powerguard._grid_rows — positional on purpose: sqlite3
# rows are plain tuples and the CLI probe must stay dependency-light.
VOLTS, STAMP, INVERTER, RAW = 0, 1, 2, 3


def offgrid_match(raw: Any) -> bool:
    """One reading's off-grid signature, read from its ``raw_data`` JSON.

    ``raw`` is the poller's JSON text (or an already-decoded mapping). Absent,
    malformed or key-less data is ``False`` — a row that cannot PROVE off-grid
    never counts toward a run. Flags are accepted as JSON booleans or 0/1.
    """
    if not raw:
        return False
    try:
        d = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else dict(raw)
        freq = float(d["grid_frequency"])
    except (ValueError, TypeError, KeyError):
        return False
    return (
        d.get("status_importing_power_from_grid") in (False, 0)
        and d.get("status_exporting_power_to_grid") in (False, 0)
        and d.get("status_discharging") in (True, 1)
        and abs(freq - OFFGRID_FREQUENCY_HZ) < OFFGRID_FREQUENCY_TOLERANCE_HZ
    )


def longest_offgrid_run(rows: list[Any]) -> int:
    """Longest run of CONSECUTIVE off-grid rows (``rows`` are time-ordered)."""
    best = run = 0
    for r in rows:
        run = run + 1 if offgrid_match(r[RAW]) else 0
        best = max(best, run)
    return best


def window_verdict(rows: list[Any], min_grid_volts: float) -> tuple[str, str] | None:
    """``(rule, detail)`` when the window says ON BATTERY, else ``None``.

    Rule order: the any-row voltage floor first (a dip anywhere in the window
    keeps the GPU down — resuming into a flickering supply is worse than
    staying paused a few minutes longer, and a sagging grid is when the UPS can
    least absorb a 150 W spike), then the consecutive-row off-grid signature.
    ``rule`` is ``'voltage-floor'`` or ``'off-grid'`` and is data (never
    renamed); ``detail`` is the log-line body.
    """
    volts = [float(r[VOLTS]) for r in rows if r[VOLTS] is not None]
    floor = min(volts) if volts else None
    if floor is not None and floor < min_grid_volts:
        return "voltage-floor", f"grid={floor:.1f}V < {min_grid_volts:.1f}V"
    run = longest_offgrid_run(rows)
    if run >= OFFGRID_CONSECUTIVE_ROWS:
        held = f"{floor:.1f}V" if floor is not None else "n/a"
        return "off-grid", (
            f"{run} consecutive rows importing=0 exporting=0 discharging=1 at "
            f"{OFFGRID_FREQUENCY_HZ:.1f}Hz while grid={held} "
            f"(>= {OFFGRID_CONSECUTIVE_ROWS} rows required; never any-row)"
        )
    return None

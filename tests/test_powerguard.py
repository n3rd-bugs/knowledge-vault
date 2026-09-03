"""The battery guard's SIGNAL (review #8 H2): battery inverter only, two rules.

Every Wave G pause was a phantom: the guard read BOTH inverters' rows blended,
and inverter #2 (6 kVA, no battery) parks at 0 V in night standby. Real outages
hold ~237 V on the 10 kVA UPS inverter — invisible to a voltage floor — but carry
the inverter's own off-grid status word in raw_data. These tests pin:

* the has_battery=1 filter is applied BEFORE the window is cut (an inverter-2
  zero row is ignored, however fresh);
* the off-grid rule needs >= 3 CONSECUTIVE matching rows — never any-row: the
  09-01 18:47-19:05 grid-tied discharge window (copied from the real DB) must
  read GRID, the 07-30 04:03-04:16 outage (also copied) must read BATTERY;
* the ON BATTERY log names the inverter and the rule that fired;
* _on_battery and grid_state share ONE verdict;
* a battery inverter with no fresh rows stays fail-safe ('unknown' / STALE),
  however fresh inverter 2's rows are.

Fixtures write NAIVE LOCAL stamps exactly as the sungrow poller does (the
Invariant 7 carve-out in powerguard._reading_age_seconds).
"""

from __future__ import annotations

import ast
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kbcore.config import settings
from kbworker import power_rules, powerguard
from kbworker.power_rules import longest_offgrid_run, offgrid_match, window_verdict

REPO = Path(__file__).resolve().parents[1]


def _raw(importing: bool, exporting: bool, discharging: bool, freq: float) -> str:
    """The poller's raw_data JSON, reduced to the keys the rule reads (+ noise)."""
    return json.dumps(
        {
            "grid_frequency": freq,
            "status_importing_power_from_grid": importing,
            "status_exporting_power_to_grid": exporting,
            "status_discharging": discharging,
            "status_charging": False,
            "status_load_is_active": True,
        }
    )


OFFGRID = _raw(False, False, True, 50.0)  # the outage signature
GRID = _raw(True, False, False, 50.1)  # ordinary import at grid-tied jitter
GRID_DISCHARGE = _raw(True, False, True, 50.0)  # evening grid-tied discharge


def _db(tmp_path: Path, readings, inverters=((1, 1), (2, 0))) -> str:
    """A sungrow stand-in in the poller's real two-table shape.

    ``readings`` are ``(inverter_id, age_seconds, grid_voltage, raw_data)``;
    stamps are NAIVE LOCAL wall-clock, as the Django poller writes them.
    """
    path = tmp_path / "sungrow.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE monitor_inverter (id INTEGER PRIMARY KEY, name TEXT, has_battery BOOL)"
    )
    conn.executemany(
        "INSERT INTO monitor_inverter VALUES (?,?,?)", [(i, f"inv{i}", b) for i, b in inverters]
    )
    conn.execute(
        "CREATE TABLE monitor_inverterreading (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT, grid_voltage REAL, inverter_id INTEGER, raw_data TEXT)"
    )
    now = datetime.now().replace(microsecond=0)  # naive local, as the poller
    conn.executemany(
        "INSERT INTO monitor_inverterreading (timestamp, grid_voltage, inverter_id, raw_data) "
        "VALUES (?,?,?,?)",
        [
            ((now - timedelta(seconds=age)).isoformat(sep=" "), v, inv, raw)
            for inv, age, v, raw in readings
        ],
    )
    conn.commit()
    conn.close()
    return str(path)


def _use(monkeypatch, tmp_path, readings, **kw) -> None:
    monkeypatch.setattr(settings, "kb_power_db", _db(tmp_path, readings, **kw))


# 2026-07-30 04:03:34 -> 04:16:39, inverter 1: the outage the floor never saw
# (~237 V throughout, importing=0 exporting=0 discharging=1 at 50.0 Hz), with the
# battery-less inverter-2 rows and the grid-tied rows either side, copied from the
# real DB. Tuples: (inverter, seconds before the newest row, grid_voltage, raw).
OUTAGE_0730 = [
    (1, 0, 237.3, OFFGRID),  # 04:16:39
    (2, 11, 234.7, _raw(True, False, False, 50.0)),  # 04:16:28 inverter 2 — ignored
    (1, 60, 237.3, OFFGRID),  # 04:15:39
    (1, 121, 237.3, OFFGRID),  # 04:14:38
    (1, 181, 236.2, OFFGRID),  # 04:13:38
    (1, 242, 237.5, OFFGRID),  # 04:12:37
    (1, 302, 237.5, OFFGRID),  # 04:11:37
    (1, 362, 237.5, OFFGRID),  # 04:10:37
    (1, 423, 237.5, OFFGRID),  # 04:09:36
    (1, 483, 237.5, OFFGRID),  # 04:08:36
    (1, 544, 237.0, OFFGRID),  # 04:07:35
    (1, 604, 237.0, OFFGRID),  # 04:06:35
    (1, 665, 237.0, OFFGRID),  # 04:05:34
    (1, 725, 237.0, OFFGRID),  # 04:04:34
    (1, 785, 237.0, OFFGRID),  # 04:03:34 — onset
    (2, 917, 237.6, _raw(True, False, False, 50.4)),  # 04:01:22
    (1, 922, 237.6, _raw(True, False, False, 50.4)),  # 04:01:17 — grid-tied
]

# 2026-09-01 18:47:25 -> 19:05:38, inverter 1: grid-tied evening DISCHARGE. The
# 19:00:36 + 19:01:36 rows carry the full off-grid signature (a run of TWO) and
# 19:03:37 an isolated one; an any-row rule pauses the GPU here. Copied from the
# real DB, inverter 2 included.
GRID_0901 = [
    (1, 0, 236.2, _raw(False, False, True, 49.8)),  # 19:05:38 — 49.8 Hz
    (2, 16, 235.7, _raw(True, False, False, 49.8)),
    (1, 61, 238.1, GRID_DISCHARGE),  # 19:04:37 — importing
    (2, 76, 238.1, _raw(True, False, False, 50.0)),
    (1, 121, 240.0, OFFGRID),  # 19:03:37 — match (isolated)
    (2, 135, 238.5, _raw(True, False, False, 50.0)),
    (1, 181, 239.5, _raw(False, False, True, 50.1)),  # 19:02:37 — 50.1 Hz breaks the run
    (2, 195, 238.6, _raw(True, False, False, 50.1)),
    (1, 242, 239.0, OFFGRID),  # 19:01:36 — match
    (2, 256, 238.6, _raw(True, False, False, 50.1)),
    (1, 302, 239.0, OFFGRID),  # 19:00:36 — match (run of 2)
    (2, 316, 238.2, _raw(True, False, False, 50.0)),
    (1, 363, 239.0, _raw(False, True, True, 50.1)),  # 18:59:35 — exporting
    (2, 376, 238.2, _raw(True, False, False, 50.1)),
    (1, 423, 239.0, _raw(False, False, True, 50.1)),  # 18:58:35
    (2, 437, 238.3, _raw(True, False, False, 50.1)),
    (1, 483, 238.8, _raw(False, False, True, 50.2)),  # 18:57:35
    (2, 497, 238.2, _raw(True, False, False, 50.3)),
    (1, 545, 238.8, _raw(True, False, True, 50.3)),  # 18:56:33
    (1, 606, 238.8, _raw(True, False, True, 50.3)),  # 18:55:32
    (1, 667, 237.2, _raw(True, False, True, 50.3)),  # 18:54:31
    (1, 728, 237.2, _raw(True, False, True, 50.4)),  # 18:53:30
    (1, 789, 236.1, _raw(True, False, True, 50.4)),  # 18:52:29
    (1, 850, 234.8, _raw(True, False, True, 50.3)),  # 18:51:28
    (1, 911, 234.8, _raw(True, False, True, 50.4)),  # 18:50:27
    (1, 972, 233.7, _raw(True, False, True, 50.4)),  # 18:49:26
    (1, 1033, 232.6, _raw(True, False, True, 50.4)),  # 18:48:25
    (1, 1093, 232.6, _raw(True, False, True, 50.4)),  # 18:47:25
]


@pytest.fixture()
def wide_window(monkeypatch):
    """Cover the whole 18:57-19:05 stretch so the run-of-two sits inside the window."""
    monkeypatch.setattr(settings, "kb_power_window", 12)


# ------------------------------------------------ the has_battery=1 filter (H2a)


def test_an_inverter_two_zero_row_is_ignored(tmp_path, monkeypatch):
    """THE phantom: inverter 2 (no battery) parks at 0 V in night standby."""
    _use(monkeypatch, tmp_path, [(2, 5, 0.0, GRID), (1, 20, 231.0, GRID), (1, 80, 230.5, GRID)])
    assert powerguard._on_battery() is False
    assert powerguard.grid_state() == "grid"


def test_the_filter_is_applied_before_the_window_is_cut(tmp_path, monkeypatch):
    """Twenty fresh inverter-2 rows must not push inverter 1 out of a 5-row window."""
    readings = [(2, i, 0.0, GRID) for i in range(1, 21)]
    readings += [(1, 30 + 60 * i, 230.0, GRID) for i in range(7)]
    _use(monkeypatch, tmp_path, readings)
    rows = powerguard._grid_rows()
    assert len(rows) == 5 and {r[2] for r in rows} == {1}, "5 rows == ~5 min of ONE poller"
    assert powerguard.grid_state() == "grid" and powerguard._on_battery() is False


def test_a_stale_battery_inverter_is_unknown_even_with_a_fresh_second_inverter(
    tmp_path, monkeypatch, caplog
):
    """The battery poller died while inverter 2 keeps writing: fail-safe both ways."""
    _use(monkeypatch, tmp_path, [(2, 5, 231.0, GRID), (1, 7_200, 230.0, GRID)])
    assert powerguard.grid_state() == "unknown"
    with caplog.at_level(logging.WARNING, logger="kbworker.consolidate"):
        assert powerguard._on_battery() is True
    assert "STALE" in caplog.text and "inverter 1" in caplog.text


def test_no_battery_inverter_rows_at_all_is_unknown_not_grid(tmp_path, monkeypatch, caplog):
    _use(monkeypatch, tmp_path, [(2, 5, 231.0, GRID), (2, 65, 230.0, GRID)])
    assert powerguard.grid_state() == "unknown"
    with caplog.at_level(logging.WARNING, logger="kbworker.consolidate"):
        assert powerguard._on_battery() is False, "no readings == unconfigured (fail-open)"
    assert "no readings" in caplog.text


def test_a_schema_without_the_inverter_table_fails_open_and_unknown(tmp_path, monkeypatch, caplog):
    """No blend-everything fallback: an unexpected schema is 'unreadable', loudly."""
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE monitor_inverterreading (grid_voltage REAL, timestamp TEXT)")
    conn.execute(
        "INSERT INTO monitor_inverterreading VALUES (0.0, ?)", (datetime.now().isoformat(),)
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(settings, "kb_power_db", str(path))
    with caplog.at_level(logging.WARNING, logger="kbworker.consolidate"):
        assert powerguard._on_battery() is False
    assert "power check unavailable" in caplog.text
    assert powerguard.grid_state() == "unknown"


# ------------------------------------------------- the off-grid rule (H2b)


def test_the_0730_outage_reads_battery_by_the_off_grid_rule(tmp_path, monkeypatch, caplog):
    """~237 V throughout: the voltage floor is blind here; the signature is not."""
    _use(monkeypatch, tmp_path, OUTAGE_0730)
    with caplog.at_level(logging.WARNING, logger="kbworker.consolidate"):
        assert powerguard._on_battery() is True
    assert "ON BATTERY (inverter 1, rule=off-grid" in caplog.text
    assert "consecutive rows" in caplog.text
    assert powerguard.grid_state() == "battery"


def test_the_0901_grid_tied_discharge_reads_grid(tmp_path, monkeypatch, wide_window):
    """A run of TWO (19:00:36, 19:01:36) inside the window — any-row would pause."""
    _use(monkeypatch, tmp_path, GRID_0901)
    rows = powerguard._grid_rows()
    assert any(offgrid_match(r[3]) for r in rows), "the fixture must carry the isolated matches"
    assert longest_offgrid_run(rows) == 2
    assert powerguard._on_battery() is False
    assert powerguard.grid_state() == "grid"


def test_the_0901_window_also_reads_grid_at_the_default_window(tmp_path, monkeypatch):
    _use(monkeypatch, tmp_path, GRID_0901)
    assert powerguard.grid_state() == "grid" and powerguard._on_battery() is False


@pytest.mark.parametrize(
    ("run", "expected"), [(1, "grid"), (2, "grid"), (3, "battery"), (5, "battery")]
)
def test_three_consecutive_rows_fire_and_fewer_do_not(tmp_path, monkeypatch, run, expected):
    readings = [(1, 10 + 60 * i, 237.0, OFFGRID if i < run else GRID) for i in range(5)]
    _use(monkeypatch, tmp_path, readings)
    assert powerguard.grid_state() == expected
    assert powerguard._on_battery() is (expected == "battery")


def test_isolated_matches_never_fire_however_many(tmp_path, monkeypatch):
    """Three matches, none adjacent: the ~50 isolated matches since 08-01 were all grid."""
    pattern = [OFFGRID, GRID, OFFGRID, GRID, OFFGRID]
    _use(monkeypatch, tmp_path, [(1, 10 + 60 * i, 238.0, raw) for i, raw in enumerate(pattern)])
    assert powerguard.grid_state() == "grid" and powerguard._on_battery() is False


@pytest.mark.parametrize(
    "breaker",
    [
        _raw(False, False, True, 50.1),  # grid-tied jitter, not the inverter's own 50.0
        _raw(False, True, True, 50.0),  # exporting
        GRID_DISCHARGE,  # importing (grid-tied discharge)
        _raw(False, False, False, 50.0),  # not discharging
    ],
    ids=["50.1Hz", "exporting", "importing", "not-discharging"],
)
def test_each_leg_of_the_signature_is_required(tmp_path, monkeypatch, breaker):
    _use(monkeypatch, tmp_path, [(1, 10 + 60 * i, 237.0, breaker) for i in range(5)])
    assert powerguard.grid_state() == "grid"


def test_the_voltage_floor_still_fires_and_names_its_rule(tmp_path, monkeypatch, caplog):
    _use(monkeypatch, tmp_path, [(1, 10, 231.0, GRID), (1, 70, 45.0, GRID), (1, 130, 230.0, GRID)])
    with caplog.at_level(logging.WARNING, logger="kbworker.consolidate"):
        assert powerguard._on_battery() is True
    assert "ON BATTERY (inverter 1, rule=voltage-floor: grid=45.0V < 200.0V" in caplog.text
    assert powerguard.grid_state() == "battery"


# -------------------------------------------------- one verdict for both callers


@pytest.mark.parametrize(
    "shape",
    [
        [(230.0, GRID)] * 5,
        [(230.0, OFFGRID)] * 5,
        [(230.0, OFFGRID)] * 2 + [(230.0, GRID)] * 3,
        [(230.0, GRID), (150.0, GRID)],
        [(None, OFFGRID)] * 3,
    ],
    ids=["grid", "offgrid-5", "offgrid-2", "dip", "null-volts"],
)
def test_both_callers_share_one_verdict(monkeypatch, shape):
    stamp = datetime.now().isoformat(sep=" ")
    rows = [(v, stamp, 1, raw) for v, raw in shape]
    monkeypatch.setattr(settings, "kb_power_db", "/configured/but/patched")
    monkeypatch.setattr(powerguard, "_grid_rows", lambda: rows)
    on_battery = powerguard._on_battery()
    state = powerguard.grid_state()
    assert state in ("grid", "battery", "unknown")
    if state != "unknown":
        assert on_battery is (state == "battery")


# ------------------------------------------------------------ the pure rules


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (OFFGRID, True),
        (_raw(False, False, True, 50.04), True),  # float noise inside the tolerance
        (
            json.dumps(
                {
                    "grid_frequency": 50.0,
                    "status_importing_power_from_grid": 0,
                    "status_exporting_power_to_grid": 0,
                    "status_discharging": 1,
                }
            ),
            True,
        ),  # 0/1 flags
        (
            json.dumps(
                {
                    "status_importing_power_from_grid": False,
                    "status_exporting_power_to_grid": False,
                    "status_discharging": True,
                }
            ),
            False,
        ),  # no frequency: cannot prove off-grid
        (json.dumps({"grid_frequency": 50.0, "status_discharging": True}), False),  # keys missing
        ("not json", False),
        (None, False),
        ("", False),
        (OFFGRID.encode(), True),
    ],
)
def test_offgrid_match_reads_the_pollers_json_defensively(raw, expected):
    assert offgrid_match(raw) is expected


def test_longest_run_counts_consecutive_rows_only():
    rows = [(1, None, 1, r) for r in (OFFGRID, OFFGRID, GRID, OFFGRID, OFFGRID, OFFGRID, GRID)]
    assert longest_offgrid_run(rows) == 3
    assert longest_offgrid_run([]) == 0


def test_window_verdict_prefers_the_floor_and_names_both_rules():
    dip = [(230.0, "t", 1, OFFGRID), (40.0, "t", 1, OFFGRID), (230.0, "t", 1, OFFGRID)]
    assert window_verdict(dip, 200.0)[0] == "voltage-floor"
    rule, detail = window_verdict([(237.0, "t", 1, OFFGRID)] * 3, 200.0)
    assert rule == "off-grid" and "3 consecutive rows" in detail and "237.0V" in detail
    assert window_verdict([(230.0, "t", 1, GRID)] * 5, 200.0) is None
    assert power_rules.OFFGRID_CONSECUTIVE_ROWS == 3, "never any-row (review #8 guard)"


def test_power_rules_is_dependency_light():
    """The CLI probe imports it: stdlib only — no settings, no DB, no Ollama."""
    tree = ast.parse((REPO / "src" / "kbworker" / "power_rules.py").read_text())
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    assert mods <= {"__future__", "json", "typing"}, mods

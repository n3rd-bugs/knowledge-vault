"""The AC-restore trigger (owner request 2026-08-29).

Last night's 02:02 battery pause left 41 pending rows and the tail's deferred
work waiting a FULL DAY for the next nightly. The fix is a marker + probe +
timer loop: consolidate.main()'s tail arms ``$KB_STATE_DIR/battery-pause.json``
whenever a step paused with reason 'battery' (and disarms it on a clean
finish); ``python -m kbworker.powerguard`` answers grid|battery|unknown; and
``scripts/ac_restore.sh`` — fired by kb-ac-restore.timer every 5 minutes —
debounces the first healthy reading and restarts kb-consolidate.service.

These tests pin the marker lifecycle (written / cleared / atomic), the
three-way probe (including stale-reading 'unknown' — a trigger that RESTARTS
the GPU must never act on ignorance), the CLI exit codes, and the shell
script's decisions (via its documented seams: KB_ENV_FILE selects the env file
sourced, PYTHON_BIN the probe interpreter — both default to the repo's own).
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kbcore.config import settings
from kbworker import consolidate, powerguard

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ac_restore.sh"


# ------------------------------------------------------- the marker lifecycle


def test_battery_stop_counts_write_the_marker(tmp_path):
    marker = tmp_path / "state" / "battery-pause.json"
    counts = {
        "stopped": {
            "queued_drained": {"reason": "battery", "deferred": 41},
            "evidence_refreshed": {"reason": "battery", "deferred": 3},
            # A deadline stop is not the grid's business — it must not count.
            "summaries_written": {"reason": "deadline", "deferred": 5},
        }
    }
    before = datetime.now(UTC) - timedelta(seconds=5)
    powerguard.record_battery_pause(counts, marker=marker)
    data = json.loads(marker.read_text())
    assert data["deferred"] == 44
    at = datetime.fromisoformat(data["at"])
    assert at.tzinfo is not None, "the marker stamp must be TZ-aware UTC (Invariant 7)"
    assert before <= at <= datetime.now(UTC)
    assert list(marker.parent.glob("*.tmp")) == [], "no tmp litter after a clean write"


def test_a_clean_run_disarms_the_trigger(tmp_path):
    marker = tmp_path / "battery-pause.json"
    marker.write_text("{}")
    powerguard.record_battery_pause({"stopped": {}}, marker=marker)
    assert not marker.exists(), "a clean run means nothing is waiting"


def test_a_deadline_only_stop_also_disarms(tmp_path):
    """No battery pause => nothing is waiting on the grid; the nightly owns the
    deadline leftovers and the trigger must not resume for them."""
    marker = tmp_path / "battery-pause.json"
    marker.write_text("{}")
    counts = {"stopped": {"summaries_written": {"reason": "deadline", "deferred": 2}}}
    powerguard.record_battery_pause(counts, marker=marker)
    assert not marker.exists()


def test_an_absent_marker_stays_absent_on_a_clean_run(tmp_path):
    marker = tmp_path / "nowhere" / "battery-pause.json"
    powerguard.record_battery_pause({"stopped": {}}, marker=marker)  # must not raise
    assert not marker.exists()
    assert not marker.parent.exists(), "disarming must not create the state dir"


def test_the_marker_lands_via_tmp_plus_rename(tmp_path, monkeypatch):
    """kb-ac-restore.timer polls this path every 5 minutes: the write must be
    tmp + atomic rename so the poller can never read half a file."""
    seen: dict[str, Path] = {}
    real = pathlib.Path.replace

    def spy(self: pathlib.Path, target):  # noqa: ANN001 — mirror Path.replace
        seen["src"], seen["dst"] = Path(self), Path(target)
        return real(self, target)

    monkeypatch.setattr(pathlib.Path, "replace", spy)
    marker = tmp_path / "battery-pause.json"
    counts = {"stopped": {"x": {"reason": "battery", "deferred": 1}}}
    powerguard.record_battery_pause(counts, marker=marker)
    assert seen == {"src": tmp_path / "battery-pause.json.tmp", "dst": marker}
    assert json.loads(marker.read_text())["deferred"] == 1
    assert not seen["src"].exists()


def test_a_failed_write_never_corrupts_the_standing_marker(tmp_path, monkeypatch):
    """Atomicity's other half: a crash mid-write leaves the OLD marker intact
    (only the tmp is touched) and never fails the run's report (fail-open)."""
    marker = tmp_path / "battery-pause.json"
    marker.write_text('{"at": "old", "deferred": 7}')

    def boom(self: pathlib.Path, *a, **kw):  # noqa: ANN001
        raise OSError("disk full")

    monkeypatch.setattr(pathlib.Path, "write_text", boom)
    counts = {"stopped": {"x": {"reason": "battery", "deferred": 1}}}
    powerguard.record_battery_pause(counts, marker=marker)  # must not raise
    assert json.loads(marker.read_text()) == {"at": "old", "deferred": 7}


def test_marker_path_comes_from_the_state_dir_knob(monkeypatch):
    monkeypatch.setattr(settings, "kb_state_dir", Path("~/kb-ac-test-state"))
    expected = Path.home() / "kb-ac-test-state" / "battery-pause.json"
    assert powerguard.battery_pause_marker_path() == expected


# ------------------------------------------------ the consolidate.main() seam


def test_consolidate_re_exports_the_recorder():
    """The established seam: tests and knobs patch consolidate's global."""
    assert consolidate.record_battery_pause is powerguard.record_battery_pause


def test_mains_tail_arms_the_trigger_on_a_battery_run(kb, monkeypatch, tmp_path):
    # H6 (pkg-pipeline) counts only rows that existed at run start: queue_depth(before=...)
    monkeypatch.setattr(consolidate.ingest_queue, "queue_depth", lambda *a, **k: 41)
    monkeypatch.setattr(consolidate.eval_sentinel, "run_sentinel", lambda counts: None)
    state_dir = tmp_path / "state"
    monkeypatch.setattr(settings, "kb_state_dir", state_dir)
    result = consolidate.main(stop=lambda: "battery")
    assert result["skipped"] is False
    data = json.loads((state_dir / "battery-pause.json").read_text())
    expected = sum(v["deferred"] for v in result["stopped"].values() if v["reason"] == "battery")
    assert data["deferred"] == expected
    assert expected >= 41, "the drain's 41 deferred rows must reach the marker"


def test_mains_tail_disarms_the_trigger_on_a_clean_run(kb, monkeypatch, tmp_path):
    monkeypatch.setattr(consolidate.eval_sentinel, "run_sentinel", lambda counts: None)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "battery-pause.json").write_text('{"at": "x", "deferred": 41}')
    monkeypatch.setattr(settings, "kb_state_dir", state_dir)
    result = consolidate.main()
    assert result["stopped"] == {}
    assert not (state_dir / "battery-pause.json").exists(), (
        "the resumed run's clean finish is the ONLY thing that disarms the trigger"
    )


# ------------------------------------------------------- grid_state / the CLI


def _power_db(tmp_path, rows) -> str:
    """A stand-in sungrow DB in the poller's real two-table shape; rows are
    (grid_voltage, iso_timestamp) readings of the BATTERY inverter (id 1).
    tests/test_powerguard.py covers the multi-inverter and raw_data cases."""
    path = tmp_path / "sungrow.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE monitor_inverter (id INTEGER PRIMARY KEY, has_battery BOOL)")
    conn.executemany("INSERT INTO monitor_inverter VALUES (?,?)", [(1, 1), (2, 0)])
    conn.execute(
        "CREATE TABLE monitor_inverterreading (grid_voltage REAL, timestamp TEXT, "
        "inverter_id INTEGER, raw_data TEXT)"
    )
    conn.executemany("INSERT INTO monitor_inverterreading VALUES (?,?,1,NULL)", rows)
    conn.commit()
    conn.close()
    return str(path)


def _iso(age_seconds: int = 0) -> str:
    return (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()


def test_grid_state_fresh_healthy_reading_is_grid(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kb_power_db", _power_db(tmp_path, [(230.0, _iso(10))]))
    assert powerguard.grid_state() == "grid"


def test_grid_state_fresh_outage_is_battery(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kb_power_db", _power_db(tmp_path, [(0.0, _iso(10))]))
    assert powerguard.grid_state() == "battery"


def test_grid_state_any_dip_in_the_window_is_battery(tmp_path, monkeypatch):
    """Same any-dip floor as the drain guard: the trigger must not resume into a
    flickering supply just because the newest sample looks healthy."""
    db = _power_db(tmp_path, [(45.0, _iso(90)), (231.0, _iso(10))])
    monkeypatch.setattr(settings, "kb_power_db", db)
    assert powerguard.grid_state() == "battery"


def test_grid_state_stale_reading_is_unknown_even_at_healthy_voltage(tmp_path, monkeypatch):
    """The drain guard folds staleness into fail-safe 'battery'; the trigger
    must instead report ignorance — it may only ever RESUME on live evidence."""
    monkeypatch.setattr(settings, "kb_power_db", _power_db(tmp_path, [(230.0, _iso(7_200))]))
    assert powerguard.grid_state() == "unknown"


def test_grid_state_unparseable_stamp_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kb_power_db", _power_db(tmp_path, [(230.0, "not-a-time")]))
    assert powerguard.grid_state() == "unknown"


def test_grid_state_absent_signal_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kb_power_db", "")
    assert powerguard.grid_state() == "unknown", "unconfigured"
    monkeypatch.setattr(settings, "kb_power_db", str(tmp_path / "missing.sqlite3"))
    assert powerguard.grid_state() == "unknown", "unreadable"
    monkeypatch.setattr(settings, "kb_power_db", _power_db(tmp_path, []))
    assert powerguard.grid_state() == "unknown", "no readings"


@pytest.mark.parametrize(("state", "code"), [("grid", 0), ("battery", 1), ("unknown", 2)], ids=str)
def test_cli_prints_the_word_and_exits_by_state(monkeypatch, capsys, state, code):
    monkeypatch.setattr(powerguard, "grid_state", lambda: state)
    assert powerguard._cli() == code
    assert capsys.readouterr().out.strip() == state


def test_the_module_runs_standalone_and_reports_over_a_real_process(tmp_path):
    """`python -m kbworker.powerguard` is what ac_restore.sh execs — prove the
    module is executable and dependency-light in a REAL interpreter. KB_POWER_DB
    from the environment outranks .env (pydantic-settings precedence)."""
    db = _power_db(tmp_path, [(230.0, _iso(5))])
    proc = subprocess.run(
        [sys.executable, "-m", "kbworker.powerguard"],
        env={**os.environ, "KB_POWER_DB": db},
        capture_output=True,
        text=True,
        cwd=str(REPO),
        timeout=120,
    )
    assert (proc.stdout.strip(), proc.returncode) == ("grid", 0), proc.stderr


# ---------------------------------------------------------- scripts/ac_restore.sh


def test_the_script_is_executable_and_parses_clean():
    assert os.access(SCRIPT, os.X_OK), "systemd ExecStart needs +x"
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def _rig(
    tmp_path,
    state_word: str = "grid",
    debounce: int = 60,
    unit_state: str = "inactive",
    backup_state: str = "inactive",
):
    """A hermetic script rig: fake HOME + state dir, a stubbed probe via the
    script's documented PYTHON_BIN seam, a recording systemctl stub on PATH, and
    KB_ENV_FILE pointing at a test env file so the real .env is never sourced.

    ``unit_state`` is what the stub answers for `is-active kb-consolidate.service`
    and ``backup_state`` for either backup unit (review #8 H10b/d rigs)."""
    home = tmp_path / "home"
    state = tmp_path / "state"
    bindir = tmp_path / "bin"
    for d in (home, state, bindir):
        d.mkdir()
    calls = tmp_path / "systemctl.calls"
    pystub = bindir / "probe-python"
    pystub.write_text(f"#!/usr/bin/env bash\necho {state_word}\n")
    pystub.chmod(0o755)
    sysctl = bindir / "systemctl"
    # Record only MUTATING verbs: the already-running guard issues a read-only
    # `is-active` query on every pass (answered silently as inactive here), and
    # the tests' "no service was touched" assertions mean starts, not queries.
    sysctl.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        f'  *is-active*kb-consolidate*) echo "{unit_state}";;\n'
        f'  *is-active*kb-backup*) echo "{backup_state}";;\n'
        f'  *start*|*stop*) echo "$@" >> "{calls}";;\n'
        "esac\n"
    )
    sysctl.chmod(0o755)
    envfile = tmp_path / "test.env"
    envfile.write_text(
        f"KB_STATE_DIR={state}\nGRID_DEBOUNCE_SECONDS={debounce}\nPYTHON_BIN={pystub}\n"
    )
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(home),
        "KB_ENV_FILE": str(envfile),
    }
    return state, calls, env


def _run(env, *args):
    return subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)


def test_script_exits_silently_when_nothing_is_paused(tmp_path):
    state, calls, env = _rig(tmp_path)
    proc = _run(env)
    assert (proc.returncode, proc.stdout) == (0, "")
    assert not calls.exists() and not (state / "grid-ok.stamp").exists()


def test_script_on_battery_clears_the_debounce_stamp_and_waits(tmp_path):
    state, calls, env = _rig(tmp_path, state_word="battery")
    (state / "battery-pause.json").write_text("{}")
    (state / "grid-ok.stamp").touch()
    proc = _run(env)
    assert proc.returncode == 0
    assert not (state / "grid-ok.stamp").exists(), "a flap must restart the debounce clock"
    assert not calls.exists(), "never start the service on battery"
    assert (state / "battery-pause.json").exists()


def test_script_treats_probe_garbage_as_unknown_and_never_resumes(tmp_path):
    state, calls, env = _rig(tmp_path, state_word="Traceback")
    (state / "battery-pause.json").write_text("{}")
    (state / "grid-ok.stamp").touch()
    proc = _run(env)
    assert proc.returncode == 0
    assert not (state / "grid-ok.stamp").exists() and not calls.exists()


def test_script_debounces_then_resumes_and_keeps_the_marker(tmp_path):
    state, calls, env = _rig(tmp_path, state_word="grid", debounce=60)
    (state / "battery-pause.json").write_text("{}")
    stamp = state / "grid-ok.stamp"

    first = _run(env)  # first healthy observation: stamp only
    assert first.returncode == 0 and stamp.exists() and not calls.exists()

    second = _run(env)  # stamp too fresh: keep waiting
    assert second.returncode == 0 and stamp.exists() and not calls.exists()

    old = datetime.now(UTC).timestamp() - 120
    os.utime(stamp, (old, old))  # the grid has now held past the debounce
    third = _run(env)
    assert third.returncode == 0, third.stderr
    assert calls.read_text().strip() == "--user start --no-block kb-consolidate.service"
    assert "resuming the battery-paused nightly" in third.stdout
    assert not stamp.exists(), "the debounce stamp is spent"
    assert (state / "battery-pause.json").exists(), (
        "only a clean consolidation finish clears the marker — a flapped resume re-arms"
    )


def test_script_dry_run_prints_the_decision_and_changes_nothing(tmp_path):
    state, calls, env = _rig(tmp_path, state_word="grid", debounce=60)
    (state / "battery-pause.json").write_text("{}")
    stamp = state / "grid-ok.stamp"
    stamp.touch()
    old = datetime.now(UTC).timestamp() - 120
    os.utime(stamp, (old, old))
    proc = _run(env, "--dry-run")
    assert proc.returncode == 0
    assert "would start kb-consolidate.service" in proc.stdout
    assert not calls.exists(), "--dry-run must never start the service (starts are recorded)"
    assert stamp.exists(), "--dry-run must change nothing"
    # And with no marker, dry-run still says what it decided.
    (state / "battery-pause.json").unlink()
    proc = _run(env, "--dry-run")
    assert proc.returncode == 0 and "no pause marker" in proc.stdout


# ------------------------------ stand-down and the attempt cap (review #8 H10b/c/d)


def _armed(state, age: int = 120):
    """Marker armed + a debounce stamp already older than the 60 s rig debounce."""
    (state / "battery-pause.json").write_text("{}")
    stamp = state / "grid-ok.stamp"
    stamp.touch()
    old = datetime.now(UTC).timestamp() - age
    os.utime(stamp, (old, old))
    return stamp


@pytest.mark.parametrize("unit_state", ["active", "activating"])
def test_a_live_consolidation_stands_the_trigger_down(tmp_path, unit_state):
    """8afdc43's is-active branch, rigged: no start, stamp untouched, silent stdout."""
    state, calls, env = _rig(tmp_path, unit_state=unit_state)
    stamp = _armed(state)
    before = stamp.stat().st_mtime
    proc = _run(env)
    assert (proc.returncode, proc.stdout) == (0, "")
    assert not calls.exists(), "a live run must never be started again"
    assert stamp.exists() and stamp.stat().st_mtime == before, "the stamp is untouched"
    dry = _run(env, "--dry-run")
    assert dry.returncode == 0 and "standing down" in dry.stdout
    assert "kb-consolidate.service" in dry.stdout


@pytest.mark.parametrize("backup_state", ["active", "activating"])
def test_a_running_backup_stands_the_trigger_down(tmp_path, backup_state):
    """H10b: the Sep 1 ticks at boot+0:38 and boot+5:36 landed inside the boot-path
    backup while the guard only fires at boot+10 — a drain must not start under a
    dump or an offsite push, from either backup unit."""
    state, calls, env = _rig(tmp_path, backup_state=backup_state)
    stamp = _armed(state)
    proc = _run(env)
    assert (proc.returncode, proc.stdout) == (0, "")
    assert not calls.exists() and stamp.exists()
    dry = _run(env, "--dry-run")
    assert "standing down" in dry.stdout and "kb-backup" in dry.stdout


def test_a_failed_consolidation_stands_the_trigger_down(tmp_path):
    """H10c: kicking a failing unit every 5 minutes is not a resume — the nightly
    owns the retry (pre-trigger behaviour); OnFailure has already paged."""
    state, calls, env = _rig(tmp_path, unit_state="failed")
    _armed(state)
    proc = _run(env)
    assert (proc.returncode, proc.stdout) == (0, "")
    assert not calls.exists()
    assert "failed" in _run(env, "--dry-run").stdout


def test_each_resume_start_is_counted_and_the_count_is_swept_with_the_marker(tmp_path):
    state, calls, env = _rig(tmp_path)
    _armed(state)
    proc = _run(env)
    assert proc.returncode == 0 and calls.exists()
    assert (state / "resume-attempts").read_text().strip() == "1"
    assert "resume attempt 1/3" in proc.stdout
    (state / "battery-pause.json").unlink()  # a clean run cleared the marker
    _run(env)
    assert not (state / "resume-attempts").exists(), "the counter is swept with the stamp"


def test_the_attempt_cap_says_so_once_then_stands_down(tmp_path):
    """H10c: after 3 starts without the marker clearing, ONE line and then silence."""
    state, calls, env = _rig(tmp_path)
    _armed(state)
    (state / "resume-attempts").write_text("3\n")
    first = _run(env)
    assert first.returncode == 0 and not calls.exists()
    assert "leaving leftovers to the nightly" in first.stdout
    assert (state / "resume-attempts").read_text().strip() == "4"
    second = _run(env)
    assert (second.returncode, second.stdout) == (0, "") and not calls.exists()
    assert (state / "resume-attempts").read_text().strip() == "4"
    assert (state / "battery-pause.json").exists(), "the marker is never removed here"


def test_dry_run_at_the_cap_changes_nothing(tmp_path):
    state, calls, env = _rig(tmp_path)
    _armed(state)
    (state / "resume-attempts").write_text("3")
    proc = _run(env, "--dry-run")
    assert proc.returncode == 0 and "would stand down" in proc.stdout
    assert (state / "resume-attempts").read_text() == "3" and not calls.exists()


def test_record_battery_pause_returns_the_prior_marker_it_consumed(tmp_path):
    """Review #8 H7d: the digest names a RESUME. A clean run that found a
    marker returns its payload (then disarms); a run that found none returns
    None; a fresh pause both re-arms and reports the prior payload."""
    marker = tmp_path / "battery-pause.json"
    assert powerguard.record_battery_pause({"stopped": {}}, marker=marker) is None
    marker.write_text('{"at": "2026-08-30T00:59:56+00:00", "deferred": 3}')
    prior = powerguard.record_battery_pause({"stopped": {}}, marker=marker)
    assert prior == {"at": "2026-08-30T00:59:56+00:00", "deferred": 3}
    assert not marker.exists(), "a clean run still disarms after reporting"
    marker.write_text("not json at all")
    assert powerguard.record_battery_pause({"stopped": {}}, marker=marker) is None
    marker.write_text('{"at": "x", "deferred": 1}')
    counts = {"stopped": {"queued_drained": {"reason": "battery", "deferred": 9}}}
    assert powerguard.record_battery_pause(counts, marker=marker) == {"at": "x", "deferred": 1}
    assert json.loads(marker.read_text())["deferred"] == 9, "a fresh pause re-arms the marker"

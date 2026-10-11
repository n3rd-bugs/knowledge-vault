"""Wave C.2 review fixes: the sweep timer's anchors and its kb-flush.env OFF switch.

Everything here RUNS the shipped ``install.sh`` under a throwaway HOME with a
fake ``systemctl`` / ``loginctl`` first on PATH (the fake records every call and
answers ``is-enabled`` / ``is-active`` from two files in the sandbox HOME). No
real user manager is reached: the environment is built from scratch, without
``XDG_RUNTIME_DIR`` or a session bus, and the KB URL is a refused local port.

* the rendered ``kb-flush-sweep.timer`` carries OnBootSec, OnStartupSec,
  **OnActiveSec** (the anchor a long-running user manager still honours: with
  ``Persistent=true`` a past OnBootSec/OnStartupSec is a spent one-shot, and
  OnUnitActiveSec has no base until the service itself ran -- rm1, KB 49064),
  OnUnitActiveSec and Persistent; an installed unit WITHOUT OnActiveSec is a
  change, so an update re-renders it and issues the restart. (Whether systemd
  then re-arms the timer is systemd's behaviour; these tests observe only the
  rendered keys and the restart call.)
* ``KB_SWEEP_INTERVAL=0`` / ``KB_SWEEP_TIMER=0`` in kb-flush.env is honoured by
  the installer -- read by grep, never sourced -- so an update no longer
  re-enables a contained timer; an enabled/active timer is disabled; the
  environment always wins over the file, through ``update.sh`` too.
* round 2: an interval under 60 is named on EVERY ``not scheduled`` line (never
  "the piggyback still runs the sweep" -- it does not); a file KB_SWEEP_TIMER
  word (off/false/no) is OFF, as the same word exported is; with no systemctl
  answer an enablement link left on disk earns one ``hint:`` line.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.test_capture_funnel import INSTALL
from tests.test_wave_h_fleet import _CFG_SHARED, _run_update, _serve, _update_box

FAKE_KBG = "kbg_" + "0" * 43
UNIT = "kb-flush-sweep.timer"


def _sandbox(tmp_path: Path, enabled: str = "disabled", active: str = "inactive") -> Path:
    home = tmp_path / "home"
    assert str(home) != os.path.expanduser("~")
    (home / ".claude" / "hooks").mkdir(parents=True)
    b = tmp_path / "fakebin"
    b.mkdir()
    (b / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        'echo "systemctl $*" >>"$HOME/systemctl.calls"\n'
        'case "$*" in\n'
        "  *is-system-running*) echo running ;;\n"
        '  *is-enabled*) cat "$HOME/sc.enabled" ;;\n'
        '  *is-active*) cat "$HOME/sc.active" ;;\n'
        "esac\nexit 0\n"
    )
    (b / "loginctl").write_text("#!/usr/bin/env bash\necho yes\n")
    for f in b.iterdir():
        f.chmod(0o755)
    (home / "sc.enabled").write_text(enabled + "\n")
    (home / "sc.active").write_text(active + "\n")
    return home


def _cfg(home: Path, body: str) -> Path:
    cfg = home / ".claude" / "kb-flush.env"
    cfg.write_text(body)
    cfg.chmod(0o600)
    return cfg


def _install(tmp_path: Path, home: Path, extra: dict | None = None):
    env = {
        "HOME": str(home),
        "PATH": f"{tmp_path / 'fakebin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "LANG": "C.UTF-8",
        "USER": "kbtest",
        "TMPDIR": str(tmp_path),
        "KB_REST_URL": "http://127.0.0.1:9",
        **(extra or {}),
    }
    res = subprocess.run(
        ["bash", str(INSTALL)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    (line,) = [ln for ln in res.stdout.splitlines() if ln.startswith("sweep:")]
    return res, line


def _calls(home: Path) -> str:
    p = home / "systemctl.calls"
    return p.read_text() if p.exists() else ""


def _unitd(home: Path) -> Path:
    return home / ".config" / "systemd" / "user"


# ---------------------------------------------------------------------------
# MUST-FIX 6: OnActiveSec in the rendered timer; an old unit is a change
# ---------------------------------------------------------------------------


def test_rendered_timer_keys_and_restart_on_change(tmp_path: Path):
    home = _sandbox(tmp_path)
    _res, line = _install(tmp_path, home)
    assert line.startswith("sweep:    scheduled (kb-flush-sweep.timer every 1800s"), line
    assert "unit files rendered" in line, line
    timer = (_unitd(home) / UNIT).read_text()
    keys = timer.splitlines()
    for key in (
        "OnBootSec=10min",
        "OnStartupSec=10min",
        "OnActiveSec=10min",
        "OnUnitActiveSec=1800s",
        "Persistent=true",
    ):
        assert key in keys, (key, timer)
    assert f"--user enable --now {UNIT}" in _calls(home)
    assert f"--user restart {UNIT}" in _calls(home)

    # Nothing changed: unchanged, and no restart.
    (home / "systemctl.calls").write_text("")
    _res, line = _install(tmp_path, home)
    assert "unit files unchanged" in line, line
    assert "restart" not in _calls(home)

    # A unit rendered by the C.2 template (no OnActiveSec) is a change: the
    # update re-renders it and issues the restart.
    (_unitd(home) / UNIT).write_text(timer.replace("OnActiveSec=10min\n", ""))
    (home / "systemctl.calls").write_text("")
    _res, line = _install(tmp_path, home)
    assert "unit files rendered" in line, line
    assert (_unitd(home) / UNIT).read_text() == timer
    calls = _calls(home)
    assert f"--user enable --now {UNIT}" in calls
    assert f"--user restart {UNIT}" in calls
    assert "disable" not in calls


# ---------------------------------------------------------------------------
# SHOULD-FIX: the kb-flush.env OFF switch reaches install/update
# ---------------------------------------------------------------------------

_OFF_I = (
    "sweep:    not scheduled (KB_SWEEP_INTERVAL=0 in kb-flush.env; the sweep is off -- "
    "0 disables it, the floor is 60s; kb-flush-sweep.timer was enabled/active -- disabled now)"
)
_OFF_T = (
    "sweep:    not scheduled (KB_SWEEP_TIMER=0 in kb-flush.env; the hooks' piggyback "
    "trigger still runs the sweep; kb-flush-sweep.timer was enabled/active -- disabled now)"
)


@pytest.mark.parametrize(
    ("body", "want"),
    [
        ("KB_SWEEP_INTERVAL=0\n", _OFF_I),
        ("export KB_SWEEP_INTERVAL='0'\n", _OFF_I),
        ('KB_SWEEP_INTERVAL="0"   # contained 2026-10-11, KB 49064\n', _OFF_I),
        ("KB_SWEEP_INTERVAL=1800\nKB_SWEEP_INTERVAL=0\n", _OFF_I),
        ("KB_SWEEP_TIMER=0\n", _OFF_T),
        ("  export KB_SWEEP_TIMER=0\r\n", _OFF_T),
    ],
    ids=["plain", "export-single", "double-comment", "last-wins", "timer", "timer-export-crlf"],
)
def test_file_off_switch_disables_an_enabled_timer(tmp_path: Path, body: str, want: str):
    home = _sandbox(tmp_path, enabled="enabled", active="active")
    _cfg(home, f"KB_TOKEN={FAKE_KBG}\n{body}")
    _res, line = _install(tmp_path, home)
    assert line == want, line
    calls = _calls(home)
    assert "enable --now" not in calls.replace("disable --now", ""), calls
    assert f"--user disable --now {UNIT}" in calls
    assert not (_unitd(home) / UNIT).exists()


def test_file_off_switch_leaves_a_disabled_timer_alone(tmp_path: Path):
    home = _sandbox(tmp_path)
    _cfg(home, "KB_SWEEP_INTERVAL=0\n")
    _res, line = _install(tmp_path, home)
    assert line == (
        "sweep:    not scheduled (KB_SWEEP_INTERVAL=0 in kb-flush.env; the sweep is off -- "
        "0 disables it, the floor is 60s)"
    ), line
    calls = _calls(home)
    assert "enable --now" not in calls and "disable" not in calls, calls


def test_env_timer_off_without_a_file_line_is_todays_behaviour(tmp_path: Path):
    """The rm1 rollout's override: same line as before C.2, nothing rendered,
    nothing enabled; a timer already disabled by hand is not touched."""
    home = _sandbox(tmp_path)
    _cfg(home, f"KB_TOKEN={FAKE_KBG}\n")
    _res, line = _install(tmp_path, home, {"KB_SWEEP_TIMER": "0"})
    assert line == (
        "sweep:    not scheduled (KB_SWEEP_TIMER=0; the hooks' piggyback trigger "
        "still runs the sweep)"
    ), line
    calls = _calls(home)
    assert "enable --now" not in calls and "disable" not in calls, calls
    assert not (_unitd(home) / UNIT).exists()


def test_env_timer_off_disables_a_timer_left_running(tmp_path: Path):
    home = _sandbox(tmp_path, enabled="enabled", active="active")
    _res, line = _install(tmp_path, home, {"KB_SWEEP_TIMER": "0"})
    assert line == (
        "sweep:    not scheduled (KB_SWEEP_TIMER=0; the hooks' piggyback trigger still runs "
        "the sweep; kb-flush-sweep.timer was enabled/active -- disabled now)"
    ), line
    assert f"--user disable --now {UNIT}" in _calls(home)


def test_env_interval_overrides_a_file_zero(tmp_path: Path):
    home = _sandbox(tmp_path)
    _cfg(home, "KB_SWEEP_INTERVAL=0\nKB_SWEEP_TIMER=0\n")
    _res, line = _install(tmp_path, home, {"KB_SWEEP_INTERVAL": "1800", "KB_SWEEP_TIMER": "1"})
    assert line.startswith("sweep:    scheduled (kb-flush-sweep.timer every 1800s"), line
    calls = _calls(home)
    assert f"--user enable --now {UNIT}" in calls
    assert "disable" not in calls


def test_file_interval_sets_the_cadence(tmp_path: Path):
    home = _sandbox(tmp_path)
    _cfg(home, "export KB_SWEEP_INTERVAL=900\n")
    _res, line = _install(tmp_path, home)
    assert line.startswith("sweep:    scheduled (kb-flush-sweep.timer every 900s"), line
    assert "OnUnitActiveSec=900s" in (_unitd(home) / UNIT).read_text().splitlines()


def test_hostile_file_never_runs_and_never_leaks(tmp_path: Path):
    """kb-flush.env holds bearer tokens: it is GREPPED, never sourced. A command
    substitution in a knob must not run, a non-numeric value is ignored with a
    warning that does not echo it, and the token never reaches the output."""
    home = _sandbox(tmp_path)
    pwned = tmp_path / "pwned"
    _cfg(
        home,
        f"KB_TOKEN={FAKE_KBG}\n"
        f"KB_SWEEP_TIMER=`touch {pwned}`\n"
        f"KB_SWEEP_INTERVAL=$(touch {pwned})\n",
    )
    res, line = _install(tmp_path, home)
    assert not pwned.exists(), "a knob in kb-flush.env was EXECUTED"
    out = res.stdout + res.stderr
    assert FAKE_KBG not in out
    assert "touch" not in out and str(pwned) not in out, out
    assert (
        "warning:  KB_SWEEP_INTERVAL in kb-flush.env is not a whole number -- "
        "treated as OFF (0 is the off switch; seconds set the cadence)"
    ) in res.stderr
    assert (
        "warning:  KB_SWEEP_TIMER in kb-flush.env is not a whole number -- "
        "treated as OFF (only 1 turns the timer on)"
    ) in res.stderr
    # Both words are OFF (fail closed), named without echoing the file's value.
    assert line == (
        "sweep:    not scheduled (KB_SWEEP_TIMER=<non-numeric> in kb-flush.env; "
        "KB_SWEEP_INTERVAL=<non-numeric> in kb-flush.env; the sweep is off -- "
        "0 disables it, the floor is 60s)"
    ), line
    assert "enable --now" not in _calls(home)


# ---------------------------------------------------------------------------
# Round 2 should-fixes
# ---------------------------------------------------------------------------

_OFF_TAIL = "the sweep is off -- 0 disables it, the floor is 60s"


def test_rm1_step3_line_names_the_file_interval_not_the_piggyback(tmp_path: Path):
    """rm1 step 3: ``KB_SWEEP_TIMER=0 update.sh`` with KB_SWEEP_INTERVAL=0 in the
    file. The piggyback is OFF there, so the line must not say it runs."""
    home = _sandbox(tmp_path)
    _cfg(home, f"KB_TOKEN={FAKE_KBG}\nKB_SWEEP_INTERVAL=0\n")
    res, line = _install(tmp_path, home, {"KB_SWEEP_TIMER": "0"})
    assert line == (
        "sweep:    not scheduled (KB_SWEEP_TIMER=0; KB_SWEEP_INTERVAL=0 in kb-flush.env; "
        f"{_OFF_TAIL})"
    ), line
    assert "piggyback" not in line
    calls = _calls(home)
    assert "enable --now" not in calls and "disable" not in calls, calls
    assert not [ln for ln in res.stdout.splitlines() if ln.startswith("hint:")]


def test_timer_off_with_an_env_interval_zero_names_the_interval(tmp_path: Path):
    home = _sandbox(tmp_path, enabled="enabled", active="active")
    _cfg(home, "KB_SWEEP_TIMER=0\n")
    _res, line = _install(tmp_path, home, {"KB_SWEEP_INTERVAL": "0"})
    assert line == (
        f"sweep:    not scheduled (KB_SWEEP_TIMER=0 in kb-flush.env; KB_SWEEP_INTERVAL=0; "
        f"{_OFF_TAIL}; kb-flush-sweep.timer was enabled/active -- disabled now)"
    ), line
    assert f"--user disable --now {UNIT}" in _calls(home)


def test_windows_line_names_an_interval_zero(tmp_path: Path):
    home = _sandbox(tmp_path)
    (tmp_path / "fakebin" / "uname").write_text(
        '#!/bin/sh\n[ "$1" = -s ] && echo MINGW64_NT-10.0-22631 && exit 0\nexit 1\n'
    )
    (tmp_path / "fakebin" / "uname").chmod(0o755)
    _cfg(home, "KB_SWEEP_INTERVAL=0\n")
    _res, line = _install(tmp_path, home)
    assert line == (
        f"sweep:    not scheduled (Windows; KB_SWEEP_INTERVAL=0 in kb-flush.env; {_OFF_TAIL})"
    ), line
    # ...and with the sweep on, the Windows line is unchanged.
    _cfg(home, "\n")
    _res, line = _install(tmp_path, home)
    assert line == (
        "sweep:    not scheduled (Windows: the hooks' piggyback trigger runs the sweep; "
        "see SHARING.md for the optional schtasks line)"
    ), line


@pytest.mark.parametrize("word", ["off", "false", "no", "'off'", '"no"  # contained'])
def test_a_file_timer_word_is_off_like_the_environment(tmp_path: Path, word: str):
    home = _sandbox(tmp_path, enabled="enabled", active="active")
    _cfg(home, f"KB_TOKEN={FAKE_KBG}\nKB_SWEEP_TIMER={word}\n")
    res, line = _install(tmp_path, home)
    assert line == (
        "sweep:    not scheduled (KB_SWEEP_TIMER=<non-numeric> in kb-flush.env; the hooks' "
        "piggyback trigger still runs the sweep; kb-flush-sweep.timer was enabled/active "
        "-- disabled now)"
    ), line
    calls = _calls(home)
    assert f"--user disable --now {UNIT}" in calls
    assert "enable --now" not in calls.replace("disable --now", ""), calls
    assert not (_unitd(home) / UNIT).exists()
    assert "treated as OFF" in res.stderr


def _no_answer(home: Path) -> None:
    """A systemctl that cannot reach the user bus answers nothing on stdout."""
    (home / "sc.enabled").write_text("")
    (home / "sc.active").write_text("")


def test_no_bus_with_an_enablement_link_prints_a_disable_hint(tmp_path: Path):
    home = _sandbox(tmp_path)
    _no_answer(home)
    wants = _unitd(home) / "timers.target.wants"
    wants.mkdir(parents=True)
    (wants / UNIT).symlink_to(_unitd(home) / UNIT)  # dangling: the unit file is gone
    _cfg(home, "KB_SWEEP_INTERVAL=0\n")
    res, line = _install(tmp_path, home)
    assert line == f"sweep:    not scheduled (KB_SWEEP_INTERVAL=0 in kb-flush.env; {_OFF_TAIL})"
    hints = [ln for ln in res.stdout.splitlines() if ln.startswith("hint:")]
    assert hints == [
        f"hint:     {UNIT} is still enabled ({wants}) and systemctl --user did not answer "
        f"-- run 'systemctl --user disable --now {UNIT}' from a login session"
    ], res.stdout
    assert "disable --now" not in _calls(home)


def test_no_bus_without_a_link_and_an_answering_bus_print_no_hint(tmp_path: Path):
    home = _sandbox(tmp_path)
    _no_answer(home)
    _cfg(home, "KB_SWEEP_INTERVAL=0\n")
    res, _line = _install(tmp_path, home)
    assert "hint:" not in res.stdout
    # An answering manager that says 'disabled' is authoritative: no hint either.
    home2 = _sandbox(tmp_path / "b")
    wants = _unitd(home2) / "timers.target.wants"
    wants.mkdir(parents=True)
    (wants / UNIT).write_text("")
    _cfg(home2, "KB_SWEEP_INTERVAL=0\n")
    res, _line = _install(tmp_path / "b", home2)
    assert "hint:" not in res.stdout


# update.sh sources kb-flush.env before exec'ing install.sh: the caller's
# exported KB_SWEEP_* must still win, and a file line must not arrive as if it
# were the caller's (install.sh greps the file itself and labels it).
_PROBE_INSTALL = (
    "#!/usr/bin/env bash\n"
    'printf "I=%s T=%s\\n" "${KB_SWEEP_INTERVAL-unset}" "${KB_SWEEP_TIMER-unset}" '
    '>"$HOME/installer.env"\n'
)


@pytest.mark.parametrize(
    ("cfg", "env", "want"),
    [
        (
            "KB_SWEEP_INTERVAL=0\nKB_SWEEP_TIMER=0\n",
            {"KB_SWEEP_INTERVAL": "1800"},
            "I=1800 T=unset",
        ),
        ("export KB_SWEEP_INTERVAL=0\n", {"KB_SWEEP_TIMER": "1"}, "I=unset T=1"),
        ("KB_SWEEP_TIMER=0\n", {"KB_SWEEP_TIMER": "1", "KB_SWEEP_INTERVAL": "900"}, "I=900 T=1"),
        ("export KB_SWEEP_TIMER=0\nKB_SWEEP_INTERVAL=0\n", {}, "I=unset T=unset"),
    ],
    ids=["env-interval-beats-file", "file-export-not-forwarded", "env-both", "file-only"],
)
def test_update_sh_keeps_the_callers_sweep_switches(tmp_path: Path, cfg: str, env: dict, want: str):
    served = _serve(tmp_path, {"install.sh": _PROBE_INSTALL})
    home, uenv, _rec = _update_box(tmp_path, cfg=_CFG_SHARED + cfg)
    res = _run_update(home, {**uenv, **env}, served)
    assert res.returncode == 0, res.stdout + res.stderr
    assert (home / "installer.env").read_text().strip() == want


# ---------------------------------------------------------------------------
# Round 3: a non-numeric INTERVAL is OFF; 1-59 is "timer not scheduled", not off
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "word", ["off", "no", "false", "disabled", "'off'", '"no"  # contained', "30s", "-1"]
)
def test_a_file_interval_word_is_off_and_disables_the_timer(tmp_path: Path, word: str):
    """KB_SWEEP_INTERVAL=off in kb-flush.env stops the hooks' piggyback (``[ off
    -gt 0 ]`` fails there), so the installer must not fall back to 1800 and
    (re-)enable the timer: a contained box never fails OPEN on a word. The value
    is named ``<non-numeric>``, never echoed."""
    home = _sandbox(tmp_path, enabled="enabled", active="active")
    _cfg(home, f"KB_TOKEN={FAKE_KBG}\nKB_SWEEP_INTERVAL={word}\n")
    res, line = _install(tmp_path, home)
    assert line == (
        "sweep:    not scheduled (KB_SWEEP_INTERVAL=<non-numeric> in kb-flush.env; "
        f"{_OFF_TAIL}; kb-flush-sweep.timer was enabled/active -- disabled now)"
    ), line
    calls = _calls(home)
    assert f"--user disable --now {UNIT}" in calls
    assert "enable --now" not in calls.replace("disable --now", ""), calls
    assert not (_unitd(home) / UNIT).exists()
    assert "KB_SWEEP_INTERVAL in kb-flush.env is not a whole number -- treated as OFF" in (
        res.stderr
    )
    out = res.stdout + res.stderr
    assert FAKE_KBG not in out
    bare = word.split("#")[0].strip().strip("'\"")
    assert f"KB_SWEEP_INTERVAL={bare}" not in out, out


def test_an_exported_interval_word_is_off_too(tmp_path: Path):
    home = _sandbox(tmp_path, enabled="enabled", active="active")
    _res, line = _install(tmp_path, home, {"KB_SWEEP_INTERVAL": "off"})
    assert line == (
        "sweep:    not scheduled (KB_SWEEP_INTERVAL=<non-numeric>; "
        f"{_OFF_TAIL}; kb-flush-sweep.timer was enabled/active -- disabled now)"
    ), line
    assert "enable --now" not in _calls(home).replace("disable --now", "")


@pytest.mark.parametrize("src", ["file", "env"])
def test_an_interval_under_60_is_not_called_off(tmp_path: Path, src: str):
    """1-59: the timer is not scheduled (60 s is its floor) and is disabled if
    running, but the hooks' piggyback still sweeps every N s -- the line must not
    say 'the sweep is off' (only exactly 0 is)."""
    home = _sandbox(tmp_path, enabled="enabled", active="active")
    if src == "file":
        _cfg(home, "KB_SWEEP_INTERVAL=30\n")
        res, line = _install(tmp_path, home)
        where = " in kb-flush.env"
    else:
        res, line = _install(tmp_path, home, {"KB_SWEEP_INTERVAL": "30"})
        where = ""
    assert line == (
        f"sweep:    not scheduled (KB_SWEEP_INTERVAL=30{where} is under the timer's 60s "
        "floor; the hooks' piggyback trigger still runs the sweep every 30s; "
        "kb-flush-sweep.timer was enabled/active -- disabled now)"
    ), line
    assert "the sweep is off" not in line
    assert not (_unitd(home) / UNIT).exists()
    assert "not a whole number" not in res.stderr


def test_timer_off_with_an_interval_under_60_names_the_piggyback_cadence(tmp_path: Path):
    home = _sandbox(tmp_path)
    _cfg(home, "KB_SWEEP_TIMER=0\nKB_SWEEP_INTERVAL=45\n")
    _res, line = _install(tmp_path, home)
    assert line == (
        "sweep:    not scheduled (KB_SWEEP_TIMER=0 in kb-flush.env; KB_SWEEP_INTERVAL=45 in "
        "kb-flush.env is under the timer's 60s floor; the hooks' piggyback trigger still "
        "runs the sweep every 45s)"
    ), line


def test_interval_zero_spelled_00_is_still_off(tmp_path: Path):
    home = _sandbox(tmp_path)
    _cfg(home, "KB_SWEEP_INTERVAL=00\n")
    _res, line = _install(tmp_path, home)
    assert line == f"sweep:    not scheduled (KB_SWEEP_INTERVAL=00 in kb-flush.env; {_OFF_TAIL})"

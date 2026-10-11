"""install_systemd.sh reseats a timer whose OnCalendar changed (2026-10-11).

The incident shape: a calendar timer computes its next elapse from last_trigger,
which systemd loads from the Persistent stamp when the timer starts, and a
daemon-reload does not reset it. Moving kb-consolidate.timer from 01:00 to 01:30
and reloading after 01:30 (last run 01:00:12) would have started a full nightly
on the spot. The owner installed it by hand with stop -> install -> touch stamp
-> daemon-reload -> start; the installer now applies that sequence itself to an
ALREADY-INSTALLED timer whose OnCalendar= lines changed, and to nothing else.

These run a COPY of the real script inside a sandbox repo + sandbox HOME, with a
fake ``systemctl``/``loginctl`` first on PATH that only records argv — the real
user manager is never touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "scripts" / "install_systemd.sh"
OLD_MTIME = 1_700_000_000  # 2023: unmistakably "not touched by this run"

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
unit="${@: -1}"
printf 'CALL %s\n' "$*" >>"$FAKE_LOG"
case " $* " in
  *" start "* | *" enable --now "*)
    # What a (re)start reads: the stamp's mtime at that moment, or 'absent'.
    s="$FAKE_STAMPS/stamp-$unit"
    m=absent
    [ -e "$s" ] && m="$(stat -c %Y "$s" 2>/dev/null || stat -f %m "$s")"
    printf 'STAMP-AT %s %s\n' "$unit" "$m" >>"$FAKE_LOG"
    for u in ${FAKE_FAIL:-}; do [ "$u" = "$unit" ] && exit 1; done ;;
  *" show-environment "*)
    # Unset = no user bus: systemctl fails, as it does without one.
    [ -n "${FAKE_SHOWENV:-}" ] || exit 1
    printf '%s\n' "$FAKE_SHOWENV"
    exit 0 ;;
esac
case " $* " in
  *" is-active "*)
    for u in $FAKE_ACTIVE; do [ "$u" = "$unit" ] && exit 0; done
    exit 3 ;;
  *" stop "*)
    [ -n "${FAKE_STOP_FAIL:-}" ] && exit 1
    printf 'STOP-SAW %s %s\n' "$unit" \
      "$(grep -h '^OnCalendar=' "$HOME/.config/systemd/user/$unit" 2>/dev/null)" >>"$FAKE_LOG" ;;
  *" daemon-reload "*)
    for s in "$FAKE_STAMPS"/stamp-*; do
      [ -e "$s" ] && printf 'RELOAD-SAW %s\n' "${s##*/}" >>"$FAKE_LOG"
    done ;;
esac
exit 0
"""


def _timer(oncal: str, comment: str = "") -> str:
    return (
        f"# {comment}\n[Timer]\nOnCalendar={oncal}\nPersistent=true\n"
        "\n[Install]\nWantedBy=timers.target\n"
    )


def _sandbox(tmp: Path, repo_timers: dict[str, str], installed: dict[str, str]) -> dict:
    repo = tmp / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "systemd").mkdir()
    shutil.copy2(INSTALLER, repo / "scripts" / "install_systemd.sh")
    for helper in ("kb-notify-fail.sh", "kb-boot-check.sh"):
        (repo / "systemd" / helper).write_text("#!/bin/sh\nexit 0\n")
    for name, body in repo_timers.items():
        (repo / "systemd" / name).write_text(body)
    home = tmp / "home"
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    for name, body in installed.items():
        (unit_dir / name).write_text(body)
    fakebin = tmp / "fakebin"
    fakebin.mkdir()
    (fakebin / "systemctl").write_text(FAKE_SYSTEMCTL)
    (fakebin / "loginctl").write_text('#!/bin/sh\necho "CALL loginctl $*" >>"$FAKE_LOG"\n')
    for f in fakebin.iterdir():
        f.chmod(0o755)
    return {"repo": repo, "home": home, "unit_dir": unit_dir, "fakebin": fakebin}


def _run(
    sb: dict,
    tmp: Path,
    *,
    active: str = "",
    xdg: Path | None = None,
    manager_xdg: Path | None = None,
    showenv: str | None = None,
    stamp_base: Path | None = None,
    **extra: str,
):
    base = stamp_base or manager_xdg or xdg or sb["home"] / ".local" / "share"
    stamps = base / "systemd" / "timers"
    log = tmp / "calls.log"
    log.write_text("")
    env = {
        "HOME": str(sb["home"]),
        "USER": "tester",
        "PATH": f"{sb['fakebin']}:/usr/bin:/bin",
        "FAKE_LOG": str(log),
        "FAKE_ACTIVE": active,
        "FAKE_STAMPS": str(stamps),
        **extra,
    }
    if xdg:
        env["XDG_DATA_HOME"] = str(xdg)
    if manager_xdg:
        env["FAKE_SHOWENV"] = f"HOME={sb['home']}\nXDG_DATA_HOME={manager_xdg}\nLANG=C.UTF-8"
    if showenv is not None:
        env["FAKE_SHOWENV"] = showenv
    res = subprocess.run(
        ["bash", str(sb["repo"] / "scripts" / "install_systemd.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return res, log.read_text().splitlines(), stamps


def _old_stamp(stamps: Path, name: str) -> Path:
    stamps.mkdir(parents=True, exist_ok=True)
    p = stamps / f"stamp-{name}"
    p.touch()
    os.utime(p, (OLD_MTIME, OLD_MTIME))
    return p


def _idx(lines: list[str], needle: str) -> int:
    hits = [i for i, ln in enumerate(lines) if ln == needle]
    assert hits, f"{needle!r} never happened:\n" + "\n".join(lines)
    return hits[0]


def _first_start_saw_fresh_stamp(calls: list[str], name: str, before: int) -> None:
    """The FIRST start / enable --now of a reseated timer must read a stamp touched
    by this run: a start before the touch loads the OLD last_trigger, and that is
    the incident (the moved nightly fires at once)."""
    seen = [c.split()[-1] for c in calls if c.startswith(f"STAMP-AT {name} ")]
    assert seen, f"{name} was never started:\n" + "\n".join(calls)
    assert seen[0] != "absent" and int(seen[0]) >= before, (
        f"{name} first started on a stale stamp ({seen[0]} < {before})"
    )


def test_the_script_parses():
    assert subprocess.run(["bash", "-n", str(INSTALLER)]).returncode == 0


def test_an_unchanged_oncalendar_is_neither_stopped_nor_stamped(tmp_path):
    """Comments may differ; the schedule does not -> today's behaviour exactly."""
    name = "kb-restore-test.timer"
    sb = _sandbox(
        tmp_path,
        {name: _timer("*-*-01 04:30", "new comment")},
        {name: _timer("*-*-01 04:30", "old comment")},
    )
    stamp = _old_stamp(sb["home"] / ".local/share/systemd/timers", name)
    res, calls, _ = _run(sb, tmp_path, active=name)
    assert res.returncode == 0, res.stderr
    assert not any(" stop " in f" {c} " for c in calls), calls
    assert f"CALL --user start {name}" not in calls
    assert stamp.stat().st_mtime == OLD_MTIME
    assert "reseat:" not in res.stdout
    assert "new comment" in (sb["unit_dir"] / name).read_text(), "the copy still happens"


def test_a_changed_active_timer_is_stopped_copied_stamped_reloaded_restarted(tmp_path):
    name = "kb-restore-test.timer"
    sb = _sandbox(tmp_path, {name: _timer("*-*-01 05:00")}, {name: _timer("*-*-01 04:30")})
    stamp = _old_stamp(sb["home"] / ".local/share/systemd/timers", name)
    before = int(time.time())
    res, calls, _ = _run(sb, tmp_path, active=name)
    assert res.returncode == 0, res.stderr
    _first_start_saw_fresh_stamp(calls, name, before)
    stop = _idx(calls, f"CALL --user stop {name}")
    # stopped BEFORE the copy: the unit it saw still carried the old schedule
    assert f"STOP-SAW {name} OnCalendar=*-*-01 04:30" in calls
    reload_ = _idx(calls, "CALL --user daemon-reload")
    assert f"RELOAD-SAW stamp-{name}" in calls[reload_:], "stamp must exist before the reload"
    start = _idx(calls, f"CALL --user start {name}")
    assert stop < reload_ < start
    assert abs(stamp.stat().st_mtime - before) < 60, "stamp not touched to ~now"
    assert "OnCalendar=*-*-01 05:00" in (sb["unit_dir"] / name).read_text()
    assert f"reseat: {name} OnCalendar *-*-01 04:30 -> *-*-01 05:00" in res.stdout


def test_a_changed_inactive_timer_is_stamped_but_not_started(tmp_path):
    name = "kb-cloudflared-refresh.timer"
    sb = _sandbox(tmp_path, {name: _timer("*-*-01 06:00")}, {name: _timer("*-*-01 05:30")})
    res, calls, stamps = _run(sb, tmp_path, active="")
    assert res.returncode == 0, res.stderr
    _idx(calls, f"CALL --user stop {name}")
    assert (stamps / f"stamp-{name}").exists(), "the stamp dir is created when missing"
    assert not any(c.startswith(f"CALL --user start {name}") for c in calls), calls
    assert not any(c == f"CALL --user enable --now {name}" for c in calls)
    assert "was active: no" in res.stdout


def test_the_nightly_move_under_xdg_data_home(tmp_path):
    """The real incident: kb-consolidate.timer 01:00 -> 01:30, active. Its own
    `enable --now` line also runs; the extra start is a no-op, never a second
    stop, and -- with no user manager answering show-environment -- the stamp
    lands under this shell's $XDG_DATA_HOME."""
    name = "kb-consolidate.timer"
    sb = _sandbox(tmp_path, {name: _timer("*-*-* 01:30")}, {name: _timer("*-*-* 01:00")})
    xdg = tmp_path / "xdg-data"
    stamp = _old_stamp(xdg / "systemd" / "timers", name)
    before = int(time.time())
    res, calls, stamps = _run(sb, tmp_path, active=name, xdg=xdg)
    assert res.returncode == 0, res.stderr
    assert stamps == xdg / "systemd" / "timers"
    assert abs(stamp.stat().st_mtime - before) < 60
    _first_start_saw_fresh_stamp(calls, name, before)
    assert not (sb["home"] / ".local/share/systemd/timers").exists()
    stop = _idx(calls, f"CALL --user stop {name}")
    reload_ = _idx(calls, "CALL --user daemon-reload")
    assert stop < reload_ < _idx(calls, f"CALL --user enable --now {name}")
    assert reload_ < _idx(calls, f"CALL --user start {name}")
    assert sum(c == f"CALL --user stop {name}" for c in calls) == 1
    assert f"reseat: {name} OnCalendar *-*-* 01:00 -> *-*-* 01:30" in res.stdout


def test_a_first_install_is_not_reseated(tmp_path):
    name = "kb-backup.timer"
    sb = _sandbox(tmp_path, {name: _timer("*-*-* 00:30")}, {})
    res, calls, stamps = _run(sb, tmp_path, active="")
    assert res.returncode == 0, res.stderr
    assert not any(" stop " in f" {c} " for c in calls), calls
    assert not (stamps / f"stamp-{name}").exists()
    assert "reseat:" not in res.stdout
    assert (sb["unit_dir"] / name).exists()


def test_a_failed_stop_aborts_before_the_new_schedule_is_installed(tmp_path):
    """Copying a changed OnCalendar onto a still-running timer is the incident."""
    name = "kb-consolidate.timer"
    sb = _sandbox(tmp_path, {name: _timer("*-*-* 01:30")}, {name: _timer("*-*-* 01:00")})
    res, calls, _ = _run(sb, tmp_path, active=name, FAKE_STOP_FAIL="1")
    assert res.returncode != 0
    assert "refusing" in res.stderr
    assert "OnCalendar=*-*-* 01:00" in (sb["unit_dir"] / name).read_text()
    assert "CALL --user daemon-reload" not in calls


def test_a_later_failing_enable_still_leaves_the_reseated_timer_started(tmp_path):
    """set -e aborts the installer at the first failing enable (kb-worker during a
    DB outage); the reseated nightly must already be running again by then."""
    name = "kb-restore-test.timer"
    sb = _sandbox(tmp_path, {name: _timer("*-*-01 05:00")}, {name: _timer("*-*-01 04:30")})
    _old_stamp(sb["home"] / ".local/share/systemd/timers", name)
    before = int(time.time())
    res, calls, _ = _run(sb, tmp_path, active=name, FAKE_FAIL="kb-worker.service")
    assert res.returncode != 0, "the failing enable aborts the install"
    assert "CALL --user enable --now kb-worker.service" in calls
    start = _idx(calls, f"CALL --user start {name}")
    assert _idx(calls, "CALL --user daemon-reload") < start
    assert start < _idx(calls, "CALL --user enable --now kb-worker.service")
    _first_start_saw_fresh_stamp(calls, name, before)


def test_the_stamp_follows_the_user_managers_xdg_data_home(tmp_path):
    """systemd reads the stamp under the MANAGER's XDG_DATA_HOME; this shell's
    may differ (or be unset). The manager's wins; the shell's is untouched."""
    name = "kb-consolidate.timer"
    sb = _sandbox(tmp_path, {name: _timer("*-*-* 01:30")}, {name: _timer("*-*-* 01:00")})
    shell_xdg = tmp_path / "shell-xdg"
    manager_xdg = tmp_path / "manager-xdg"
    stamp = _old_stamp(manager_xdg / "systemd" / "timers", name)
    before = int(time.time())
    res, calls, stamps = _run(sb, tmp_path, active=name, xdg=shell_xdg, manager_xdg=manager_xdg)
    assert res.returncode == 0, res.stderr
    assert "CALL --user show-environment" in calls
    assert stamps == manager_xdg / "systemd" / "timers"
    assert stamp.stat().st_mtime >= before, "the manager's stamp was not touched"
    assert not (shell_xdg / "systemd" / "timers" / f"stamp-{name}").exists()
    assert not (sb["home"] / ".local/share/systemd/timers").exists()
    _first_start_saw_fresh_stamp(calls, name, before)


def test_an_unchanged_install_never_asks_the_manager_for_its_environment(tmp_path):
    name = "kb-backup.timer"
    sb = _sandbox(tmp_path, {name: _timer("*-*-* 00:30")}, {name: _timer("*-*-* 00:30")})
    res, calls, _ = _run(sb, tmp_path, active=name)
    assert res.returncode == 0, res.stderr
    assert "CALL --user show-environment" not in calls


def _sq(path: Path) -> str:
    """A value the way systemd's show-environment prints one with a special
    character in it: $'...' with backslash and quote escaped."""
    return "$'" + str(path).replace("\\", "\\\\").replace("'", "\\'") + "'"


def test_a_dollar_quoted_manager_xdg_data_home_is_decoded(tmp_path):
    """systemd 259 prints XDG_DATA_HOME=$'/path with space' -- the stamp must land
    at the DECODED path, never at a literal "$'..." or the shell's fallback."""
    name = "kb-consolidate.timer"
    sb = _sandbox(tmp_path, {name: _timer("*-*-* 01:30")}, {name: _timer("*-*-* 01:00")})
    manager_xdg = tmp_path / "manager data's"
    stamp = _old_stamp(manager_xdg / "systemd" / "timers", name)
    before = int(time.time())
    showenv = f"HOME={sb['home']}\nXDG_DATA_HOME={_sq(manager_xdg)}\nLANG=C.UTF-8"
    res, calls, stamps = _run(sb, tmp_path, active=name, showenv=showenv, stamp_base=manager_xdg)
    assert res.returncode == 0, res.stderr
    assert stamp.stat().st_mtime >= before, "the decoded manager stamp was not touched"
    assert not (sb["home"] / ".local/share/systemd/timers").exists()
    assert not list(tmp_path.glob("$*")) and not list(sb["repo"].glob("$*"))
    _first_start_saw_fresh_stamp(calls, name, before)


def test_a_manager_without_xdg_data_home_uses_its_own_home(tmp_path):
    """show-environment ANSWERS with no XDG_DATA_HOME line: systemd stamps under
    the manager's HOME/.local/share -- not under this shell's XDG_DATA_HOME, and
    the manager's HOME (quoted when it has a space) is the one that counts."""
    name = "kb-consolidate.timer"
    sb = _sandbox(tmp_path, {name: _timer("*-*-* 01:30")}, {name: _timer("*-*-* 01:00")})
    shell_xdg = tmp_path / "shell-xdg"
    manager_home = tmp_path / "manager home"
    base = manager_home / ".local" / "share"
    stamp = _old_stamp(base / "systemd" / "timers", name)
    before = int(time.time())
    showenv = f"HOME={_sq(manager_home)}\nLANG=C.UTF-8\nPATH=/usr/bin:/bin"
    res, calls, _ = _run(sb, tmp_path, active=name, xdg=shell_xdg, showenv=showenv, stamp_base=base)
    assert res.returncode == 0, res.stderr
    assert "CALL --user show-environment" in calls
    assert stamp.stat().st_mtime >= before, "the manager-HOME stamp was not touched"
    assert not (shell_xdg / "systemd" / "timers" / f"stamp-{name}").exists()
    assert not (sb["home"] / ".local/share/systemd/timers").exists()
    assert "show-environment failed" not in res.stderr
    _first_start_saw_fresh_stamp(calls, name, before)


def test_a_failing_show_environment_is_named_and_falls_back_to_the_shell(tmp_path):
    """No user bus: show-environment fails; its exit status is captured (never
    swallowed by `|| true`), one stderr line says so, and the stamp falls back to
    the shell's XDG_DATA_HOME."""
    name = "kb-consolidate.timer"
    sb = _sandbox(tmp_path, {name: _timer("*-*-* 01:30")}, {name: _timer("*-*-* 01:00")})
    shell_xdg = tmp_path / "shell-xdg"
    stamp = _old_stamp(shell_xdg / "systemd" / "timers", name)
    before = int(time.time())
    res, calls, _ = _run(sb, tmp_path, active=name, xdg=shell_xdg)
    assert res.returncode == 0, res.stderr
    assert "systemctl --user show-environment failed rc=1" in res.stderr
    assert stamp.stat().st_mtime >= before
    _first_start_saw_fresh_stamp(calls, name, before)

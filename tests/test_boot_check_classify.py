"""kb-boot-check.sh classifies how the PREVIOUS boot ended (review #8 H10j).

The 2026-09-01 20:58 reboot read as a mystery for a day: an owner-requested GNOME
reboot 12 minutes into a real 31-minute outage, with kb-boot-check's ExecStop
having run (so no BOOT push). PID 1 logs 'Reached target reboot.target' /
'poweroff.target' on every orderly stop and nothing on a power loss, so the
script now logs ONE line — 'boot-check: previous boot ended <class>' — on every
boot, and the unclean push carries the class.

Hermetic: KB_PYTHON is a stub standing in for `kbcore.boot check` (prints the
recovery line or nothing), KB_JOURNALCTL a stub printing canned PID-1 lines,
KB_NOTIFY_ENV a missing file (so no curl ever runs), KB_NOTIFY_LOG under tmp.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "systemd" / "kb-boot-check.sh"

REBOOT_LINES = (
    "Reached target shutdown.target - System Shutdown.\n"
    "Reached target reboot.target - System Reboot.\nShutting down.\n"
)
POWEROFF_LINES = (
    "Reached target shutdown.target - System Shutdown.\n"
    "Reached target poweroff.target - System Power-Off.\nPowering off.\n"
)
UNCLEAN_MSG = "BOOT: recovered from an unclean shutdown at 2026-09-01T15:58:00+00:00"


def _rig(tmp_path: Path, *, msg: str = "", journal: str | None = REBOOT_LINES):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "msg.txt").write_text(msg)
    py = bindir / "fake-python"
    py.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$*" != "-m kbcore.boot check" ]; then echo "fake-python: $*" >&2; exit 9; fi\n'
        f'cat "{tmp_path / "msg.txt"}"\n'
    )
    py.chmod(0o755)
    jcalls = tmp_path / "journalctl.calls"
    jstub = bindir / "fake-journalctl"
    if journal is None:
        body = (
            f'#!/usr/bin/env bash\necho "$*" >> "{jcalls}"\n'
            'echo "No journal boot entry" >&2\nexit 1\n'
        )
    else:
        (tmp_path / "journal.txt").write_text(journal)
        body = f'#!/usr/bin/env bash\necho "$*" >> "{jcalls}"\ncat "{tmp_path / "journal.txt"}"\n'
    jstub.write_text(body)
    jstub.chmod(0o755)
    log = tmp_path / "log" / "notify-fail.log"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "KB_PYTHON": str(py),
        "KB_JOURNALCTL": str(jstub),
        "KB_NOTIFY_ENV": str(tmp_path / "absent-notify.env"),
        "KB_NOTIFY_LOG": str(log),
    }
    return env, log, jcalls


def _run(env):
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )


def test_the_script_parses_clean_and_still_always_exits_zero():
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert SCRIPT.read_text().rstrip().endswith("exit 0")


def test_an_orderly_reboot_is_classified_and_logged_on_a_clean_boot(tmp_path):
    env, log, jcalls = _rig(tmp_path, msg="", journal=REBOOT_LINES)
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "boot-check: previous boot ended orderly-reboot\n"
    assert "previous boot ended orderly-reboot" in log.read_text()
    calls = jcalls.read_text()
    assert "-b -1" in calls and "_PID=1" in calls, "read only PID 1's lines of the PREVIOUS boot"


def test_an_orderly_poweroff_is_told_apart_from_a_reboot(tmp_path):
    env, _log, _ = _rig(tmp_path, msg="", journal=POWEROFF_LINES)
    proc = _run(env)
    assert proc.stdout == "boot-check: previous boot ended orderly-poweroff\n"


def test_an_unreadable_journal_degrades_to_orderly_with_no_detail(tmp_path):
    """First boot, rotated journal, or no adm group: never a failed unit."""
    env, _log, _ = _rig(tmp_path, msg="", journal=None)
    proc = _run(env)
    assert proc.returncode == 0
    assert proc.stdout == "boot-check: previous boot ended orderly (journal end: none)\n"


def test_a_power_loss_reads_unclean_with_no_shutdown_target(tmp_path):
    """The marker survived and PID 1 never reached a shutdown target: power loss.
    No notify config here, so the script logs the skip and exits 0 before curl."""
    env, log, _ = _rig(tmp_path, msg=UNCLEAN_MSG, journal="")
    proc = _run(env)
    assert proc.returncode == 0
    assert "previous boot ended unclean (journal end: none)" in proc.stdout
    text = log.read_text()
    assert "previous boot ended unclean (journal end: none)" in text
    assert "boot-check skipped: no config" in text


def test_an_unclean_marker_with_an_orderly_target_names_the_contradiction(tmp_path):
    """Marker says unclean but the journal saw reboot.target: an orderly stop where
    this unit's ExecStop never ran — a different bug from a power cut."""
    env, _log, _ = _rig(tmp_path, msg=UNCLEAN_MSG, journal=REBOOT_LINES)
    proc = _run(env)
    assert "previous boot ended unclean (journal end: reboot)" in proc.stdout


def test_the_classification_precedes_the_clean_exit_and_rides_the_push():
    body = SCRIPT.read_text()
    assert body.index("previous boot ended") < body.index('[ -n "$MSG" ] || exit 0'), (
        "a clean boot must still log its class before exiting"
    )
    assert 'BODY="${MSG} [${CLASS}] @ ${MACHINE}"' in body, "the unclean push carries the class"
    assert "2>/dev/null" in body.split("JOURNAL_END=")[1].split("\n")[0], (
        "a journal read failure must be silent, never a failed boot-time unit"
    )

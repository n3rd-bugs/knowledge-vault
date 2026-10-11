"""Boot-edge hardening (three verified edges from the ops review).

All three are the same class of latent bug — something at the boot/shutdown edge
that the repo did not encode, so a reinstall or a power cut silently did the wrong
thing:

1. PRE-BACKUP DRAIN GUARD. A boot in the 00:00-00:30 window fires the catch-up
   drain (OnBootSec=10min) before the 00:30 backup, so a 4.5-7h drain could start
   moments before — and push late — the day's only backup. The boot timer now
   triggers a guard unit that orders the backup ahead of the drain.
2. UNCLEAN-SHUTDOWN NOTIFY. A power loss left no signal it happened (four silent
   reboots on 2026-08-01). A boot-time oneshot now detects an unclean prior
   shutdown via a persistent dirty-flag marker and fires exactly one ntfy line.
3. INSTALLER FOLDING. The new units install/enable through install_systemd.sh,
   and the orphaned 'data-server_default' compose network is surfaced as a manual
   cleanup line (this installer runs no docker).

The unit/installer tests parse FILES and never call systemctl — what matters is
that the repo, replayed onto a machine, encodes the intent. The marker tests are
pure filesystem (tmp_path): at boot the DB and network may be down, which is why
the marker is not DB-backed, so neither is its test.
"""

from __future__ import annotations

import re
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kbcore.boot import consume_boot_marker, default_marker_path, record_clean_shutdown
from kbcore.config import settings

REPO = Path(__file__).resolve().parents[1]
UNIT_DIR = REPO / "systemd"
INSTALLER = REPO / "scripts" / "install_systemd.sh"

BOOT_TIMER = "kb-consolidate-boot.timer"
GUARD = "kb-consolidate-boot.service"
BOOT_CHECK = "kb-boot-check.service"
BOOT_CHECK_SCRIPT = UNIT_DIR / "kb-boot-check.sh"
NOTIFIER = "kb-notify-fail@%n.service"


def parse_unit(name: str) -> dict[str, dict[str, list[str]]]:
    """Minimal systemd unit parser: ``{section: {key: [values]}}`` (repeats kept)."""
    out: dict[str, dict[str, list[str]]] = {}
    section = ""
    pending = ""
    for raw in (UNIT_DIR / name).read_text().splitlines():
        line = pending + raw.strip()
        pending = ""
        if not line or line.startswith(("#", ";")):
            continue
        if line.endswith("\\"):
            pending = line[:-1]
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            out.setdefault(section, {})
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        out.setdefault(section, {}).setdefault(key.strip(), []).append(value.strip())
    return out


def _values(name: str, section: str, key: str) -> list[str]:
    return parse_unit(name).get(section, {}).get(key, [])


def _script_const(body: str, name: str) -> str:
    m = re.search(rf"^{name}=\"?([^\"\n#]+)\"?", body, re.MULTILINE)
    assert m, f"kb-boot-check.sh has no {name}="
    return m.group(1).strip()


# =========================================================== 1. pre-backup guard


def test_the_boot_timer_triggers_the_guard_not_the_drain_directly():
    """The whole edge: the catch-up must not reach kb-consolidate.service without
    passing through the guard that orders the backup first."""
    assert _values(BOOT_TIMER, "Timer", "Unit") == [GUARD]
    assert _values(BOOT_TIMER, "Timer", "OnBootSec"), "the boot catch-up lost its trigger"


def test_the_guard_orders_the_backup_before_the_catch_up_drain():
    """backup -> guard -> drain. Wants pulls the backup into the transaction (a
    bare After= would order against nothing at OnBootSec); After/Before place it.

    The backup it pulls is the LOCAL unit (review #8 H10a): kb-backup.service
    also pushes offsite, and on 2026-09-01 the boot catch-up waited 2h59m behind
    that push on modem WAN. The guard must neither Want nor order After the
    pushing unit (tests/test_backup_local.py has the mechanical check)."""
    unit = parse_unit(GUARD)["Unit"]
    assert "kb-backup-local.service" in unit.get("Wants", []), (
        "the guard must PULL the local backup in — at boot it is not otherwise in the "
        "transaction, so a bare After= would order against nothing"
    )
    assert "kb-backup-local.service" in unit.get("After", []), "guard must run after the backup"
    assert "kb-backup.service" not in unit.get("Wants", []) + unit.get("After", []), (
        "kb-backup.service pushes offsite; the boot path must not wait behind it"
    )
    assert "kb-consolidate.service" in unit.get("Wants", []), "guard must pull the drain in"
    assert "kb-consolidate.service" in unit.get("Before", []), (
        "the drain must run AFTER the guard (and therefore after the backup)"
    )


def test_the_guard_is_a_pure_ordering_barrier():
    """It must NOT duplicate the drain's work — the real run is still
    kb-consolidate.service, with all its readiness/tiktoken/backup handling."""
    svc = parse_unit(GUARD)["Service"]
    assert svc.get("Type") == ["oneshot"]
    assert svc.get("ExecStart") == ["/bin/true"], "the guard should do no work of its own"
    assert not svc.get("ExecStartPost"), "the guard must not run its own backup"


def test_the_guard_notifies_on_failure_like_every_unattended_unit():
    assert _values(GUARD, "Unit", "OnFailure") == [NOTIFIER]


def test_the_guard_is_started_only_by_the_boot_timer():
    """No [Install]: it must not be independently enable-able, so it fires only
    via kb-consolidate-boot.timer's Unit= and never on its own at boot."""
    assert "Install" not in parse_unit(GUARD), "the guard must be timer-triggered only"


def test_the_guard_does_not_disturb_the_nightly_path():
    """The nightly at 01:30 starts kb-consolidate.service directly; the guard is
    reachable only from the boot timer, so it can add no extra backup there."""
    assert _values("kb-consolidate.timer", "Timer", "Unit") == ["kb-consolidate.service"]


# ====================================================== 2a. marker logic (pure fs)


T = datetime(2026, 8, 4, 3, 15, 30, tzinfo=UTC)
EXPECTED = "BOOT: recovered from an unclean shutdown at 2026-08-04T03:15:30+00:00"


@pytest.fixture()
def marker(tmp_path):
    return tmp_path / "state" / "unclean-shutdown.marker"


def test_a_clean_history_is_silent_and_arms_the_flag(marker):
    """No marker == clean/first boot: no push, and the flag is armed for this boot."""
    assert consume_boot_marker(marker, boot_id="boot-1", now=T) is None
    assert marker.read_text().strip() == "boot-1"


def test_an_orderly_reboot_is_silent(marker):
    """Boot, clean stop (marker removed), next boot: no false alarm."""
    assert consume_boot_marker(marker, boot_id="boot-1") is None
    record_clean_shutdown(marker)
    assert not marker.exists()
    assert consume_boot_marker(marker, boot_id="boot-2") is None


def test_a_power_loss_is_reported_at_the_next_boot(marker):
    """The marker survives a crash still holding the PREVIOUS boot id, so the next
    boot (a different id) recognises the unclean shutdown."""
    assert consume_boot_marker(marker, boot_id="boot-1") is None
    # power loss: record_clean_shutdown never runs, the marker persists as boot-1
    assert consume_boot_marker(marker, boot_id="boot-2", now=T) == EXPECTED


def test_at_most_one_push_per_boot(marker):
    """Idempotency: whatever a boot's FIRST check returns, later checks in the same
    boot are silent — the unit can never emit two pushes for one recovery."""
    marker.parent.mkdir(parents=True)
    marker.write_text("crashed-boot")  # left behind by an unclean shutdown
    first = consume_boot_marker(marker, boot_id="this-boot", now=T)
    assert first == EXPECTED
    assert consume_boot_marker(marker, boot_id="this-boot", now=T) is None
    assert consume_boot_marker(marker, boot_id="this-boot", now=T) is None


def test_the_message_timestamp_is_utc_aware(marker):
    """Invariant 7: UTC, never a naive local time."""
    marker.parent.mkdir(parents=True)
    marker.write_text("old")
    msg = consume_boot_marker(marker, boot_id="new")
    stamp = msg.removeprefix("BOOT: recovered from an unclean shutdown at ")
    assert datetime.fromisoformat(stamp).tzinfo is not None


def test_record_clean_shutdown_is_a_noop_when_absent(marker):
    """A clean stop after another clean stop must not error on a missing marker."""
    record_clean_shutdown(marker)  # must not raise
    assert not marker.exists()


def test_the_default_marker_lives_on_persistent_disk_beside_raw(monkeypatch):
    """Never /tmp (tmpfs — wiped every boot would blind the check). It sits under
    the KB data root next to raw/ and backups/, and follows a KB_RAW override."""
    monkeypatch.setattr(settings, "kb_raw", Path("/srv/kb/raw"))
    assert default_marker_path() == Path("/srv/kb/state/unclean-shutdown.marker")


# ======================================================= 2b. the detector unit/script


def test_the_detector_arms_at_boot_and_clears_on_a_clean_stop():
    """RemainAfterExit keeps it active between boot and shutdown so ExecStop runs on
    an orderly stop (and not on a crash — which is the signal)."""
    svc = parse_unit(BOOT_CHECK)["Service"]
    assert svc.get("Type") == ["oneshot"]
    assert svc.get("RemainAfterExit") == ["yes"]
    assert any("kb-boot-check.sh" in v for v in svc.get("ExecStart", [])), "no arming ExecStart"
    stop = " ".join(svc.get("ExecStop", []))
    assert "kbcore.boot clean" in stop, "a clean stop must clear the marker"


def test_the_detector_notifies_on_failure_and_bounds_its_start():
    assert _values(BOOT_CHECK, "Unit", "OnFailure") == [NOTIFIER]
    assert int(_values(BOOT_CHECK, "Service", "TimeoutStartSec")[0]) <= 300


def test_the_detector_script_exists_and_is_executable():
    """A boot-time ExecStart that lost +x is a silent recovery every boot."""
    assert BOOT_CHECK_SCRIPT.exists()
    assert BOOT_CHECK_SCRIPT.stat().st_mode & stat.S_IXUSR


def test_the_script_only_pushes_when_the_marker_says_unclean():
    """It arms the marker via `kbcore.boot check` and bails on empty output —
    a clean boot must reach no curl at all."""
    body = BOOT_CHECK_SCRIPT.read_text()
    assert "kbcore.boot check" in body, "the script must consult the marker helper"
    assert re.search(r'\[ -n "\$MSG" \] \|\| exit 0', body), "a clean boot must not push"


def test_the_script_keeps_the_ntfy_credential_off_argv():
    """The same rule as kb-notify-fail.sh: auth via a curl config on stdin (-K -),
    never argv (world-readable in /proc; it leaked once through `bash -x`)."""
    body = BOOT_CHECK_SCRIPT.read_text()
    assert "-K -" in body, "auth must come from a curl config on stdin"
    for line in body.splitlines():
        if "curl " not in line or line.strip().startswith("#"):
            continue
        assert "NTFY_TOKEN" not in line and "NTFY_PASSWORD" not in line, line
        assert not re.search(r"-u\s*[\"']?\$", line), f"basic auth in argv: {line}"


def test_the_script_is_silent_without_a_channel_and_never_loops():
    """No notify.env == do nothing (not a second failure); always exit 0 so a
    boot-time unit can never become a boot loop."""
    body = BOOT_CHECK_SCRIPT.read_text()
    assert re.search(r'\[ -r "\$CFG" \] \|\| exit 0', body)
    assert re.search(r"NTFY_TOPIC.*\|\| exit 0", body)
    assert body.rstrip().endswith("exit 0")


def test_the_script_log_is_bounded():
    body = BOOT_CHECK_SCRIPT.read_text()
    assert re.search(r'tail -\d+ "\$LOG"', body), "the log never truncates"


def test_the_retry_budget_fits_inside_the_units_timeout():
    """A TimeoutStartSec below the retry budget kills the push mid-retry and the
    recovery goes unreported — the bug wearing a different hat."""
    body = BOOT_CHECK_SCRIPT.read_text()
    tries = int(_script_const(body, "NOTIFY_TRIES"))
    max_time = int(_script_const(body, "NOTIFY_MAX_TIME"))
    backoff = [int(x) for x in _script_const(body, "NOTIFY_BACKOFF").split()]
    worst_case = tries * max_time + sum(backoff)
    timeout = int(_values(BOOT_CHECK, "Service", "TimeoutStartSec")[0])

    assert len(backoff) == tries - 1, "there must be no sleep after the final attempt"
    assert worst_case < timeout, f"script can take {worst_case}s; unit kills it at {timeout}s"


# ============================================================= 3. installer folding


def test_the_installer_enables_the_unclean_shutdown_detector():
    """--now so the marker is armed immediately and a clean shutdown today is
    recorded — the first run then cannot false-positive."""
    assert re.search(r"enable --now kb-boot-check\.service", INSTALLER.read_text())


def test_the_installer_does_not_enable_the_boot_guard_directly():
    """The guard has no [Install]; only the timer starts it. Enabling it would try
    to start it (and its Wants= drain) at every boot outside the timer."""
    assert not re.search(r"enable (--now )?kb-consolidate-boot\.service", INSTALLER.read_text())


def test_the_installer_makes_the_detector_script_executable():
    assert "chmod 755" in INSTALLER.read_text()
    assert "kb-boot-check.sh" in INSTALLER.read_text()


def test_the_installer_runs_no_docker_but_flags_the_orphan_network():
    """Edge 3: surface the orphaned 'data-server_default' network as a MANUAL
    cleanup line — this installer must never execute docker itself."""
    text = INSTALLER.read_text()
    assert "data-server_default" in text, "the orphaned compose network is not mentioned"
    executed = [
        ln
        for ln in text.splitlines()
        if re.match(r"\s*docker\s", ln) and not ln.lstrip().startswith(("#", "echo"))
    ]
    assert not executed, f"the installer executes docker: {executed}"


def test_the_new_units_install_by_glob():
    """A hand-maintained install list is how kb-consolidate-boot.timer went
    live-only; the installer must cover the directory."""
    text = INSTALLER.read_text()
    assert "systemd/*.service" in text and "systemd/*.timer" in text


# --- stale comments the schedule move left behind -----------------------------


@pytest.mark.parametrize("name", [BOOT_TIMER, "kb-backup.timer"])
def test_no_boot_or_backup_timer_still_names_the_old_0300_window(name):
    """The timers moved to 00:30/01:00 on 2026-08-01 and the nightly to 01:30 on
    2026-10-11. A comment naming 03:00/03:30/01:00 is how the next reader gets
    misled (the notifier has the same guard). Neither file narrates dated history,
    so a bare dead time in either can only be a stale present-tense claim."""
    text = (UNIT_DIR / name).read_text()
    assert "03:30" not in text, f"{name} still names the dead 03:30 nightly"
    assert "03:00" not in text, f"{name} still names the dead 03:00 backup"
    assert "01:00" not in text, f"{name} still names the dead 01:00 nightly"

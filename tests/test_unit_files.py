"""The systemd units in this repo, asserted as FILES.

Every failure this file guards against was found by archaeology instead of by a
test, which is the actual complaint:

* the boot catch-up timer and ``network-online`` ordering on kb-consolidate
  existed ONLY on the live machine — the next ``install_systemd.sh`` run would
  have silently reverted both;
* backup ran only as kb-consolidate's ``ExecStartPost``, so an interrupted
  nightly meant no backup at all (there is none for 29 or 30 July, on a box where
  power cuts are the common case, not the edge case);
* nothing alerted when any of it failed;
* the cloudflared security refresh derived its compose project from a SYMLINK and
  had therefore never once worked.

So these tests parse the unit files themselves. They deliberately do NOT call
systemctl: what matters is that the repo — the thing an install replays onto a
machine — encodes the intent. Drift then shows up as a red test rather than as a
question someone has to think to ask.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest

from kbcore.scrub import scrub

REPO = Path(__file__).resolve().parents[1]
UNIT_DIR = REPO / "systemd"
INSTALLER = REPO / "scripts" / "install_systemd.sh"

NOTIFIER = "kb-notify-fail@.service"
NOTIFIER_SCRIPT = UNIT_DIR / "kb-notify-fail.sh"
ON_FAILURE = "kb-notify-fail@%n.service"

# Units whose failure nobody would otherwise notice: they run unattended, on a
# timer or as a daemon. The notifier template is excluded on purpose (see below).
SERVICES = sorted(p.name for p in UNIT_DIR.glob("*.service") if p.name != NOTIFIER)
TIMERS = sorted(p.name for p in UNIT_DIR.glob("*.timer"))


def parse_unit(name: str) -> dict[str, dict[str, list[str]]]:
    """Minimal systemd unit parser: ``{section: {key: [values]}}``.

    Values are LISTS because systemd allows a key to repeat and the repetition is
    meaningful (two ``ExecStart=`` lines run in order, several ``After=`` lines
    accumulate) — a parser that keeps only the last value would quietly hide half
    of what a unit does, which is the opposite of the point here.
    """
    out: dict[str, dict[str, list[str]]] = {}
    section = ""
    pending = ""
    for raw in (UNIT_DIR / name).read_text().splitlines():
        line = pending + raw.strip()
        pending = ""
        if not line or line.startswith(("#", ";")):
            continue
        if line.endswith("\\"):  # systemd line continuation
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


def values(name: str, section: str, key: str) -> list[str]:
    return parse_unit(name).get(section, {}).get(key, [])


def _daily_minute(oncalendar: str) -> int:
    """Minutes-past-midnight of a daily ``OnCalendar=*-*-* HH:MM`` expression."""
    m = re.search(r"(\d{1,2}):(\d{2})", oncalendar)
    assert m, f"not a daily HH:MM schedule: {oncalendar!r}"
    return int(m.group(1)) * 60 + int(m.group(2))


# --- every unit parses and is installed ---------------------------------------


def test_the_units_are_well_formed():
    assert SERVICES, "no service units found — wrong path?"
    for name in SERVICES + TIMERS + [NOTIFIER]:
        unit = parse_unit(name)
        assert unit.get("Unit", {}).get("Description"), f"{name} has no Description"
        expected = "Timer" if name.endswith(".timer") else "Service"
        assert expected in unit, f"{name} has no [{expected}] section"


def test_the_installer_installs_every_unit_in_the_repo():
    """A hand-maintained install list is how kb-consolidate-boot.timer ended up
    live-only, one install away from deletion. The installer must cover the
    DIRECTORY, not a list someone has to remember to extend."""
    text = INSTALLER.read_text()
    by_glob = "systemd/*.service" in text and "systemd/*.timer" in text
    missing = [n for n in SERVICES + TIMERS + [NOTIFIER] if n not in text]
    assert by_glob or not missing, f"installer never installs: {missing}"


def test_the_installer_enables_the_timers_that_must_not_wait_for_a_human():
    """Backup in particular: it was installed-but-disabled 'until the owner
    reviews the schedule', which is how the only backup path ended up being the
    nightly's ExecStartPost."""
    text = INSTALLER.read_text()
    for timer in ("kb-backup.timer", "kb-consolidate.timer", "kb-consolidate-boot.timer"):
        assert re.search(rf"enable (--now )?{re.escape(timer)}", text), f"{timer} not enabled"


def test_the_boot_catchup_timer_is_not_started_by_the_installer():
    """OnBootSec=10min is already in the PAST on a box that has been up a day, so
    `enable --now` would kick off a 4.5–7h consolidation the moment anyone
    reinstalls the units. It must arm at the next boot instead."""
    text = INSTALLER.read_text()
    assert "enable kb-consolidate-boot.timer" in text
    assert "enable --now kb-consolidate-boot.timer" not in text


def test_every_repo_script_a_unit_executes_exists_and_is_executable():
    """A typo'd ExecStart path is a unit that fails on its first real run — at
    03:00, unattended."""
    for name in SERVICES + [NOTIFIER]:
        unit = parse_unit(name)["Service"]
        for key in ("ExecStart", "ExecStartPre", "ExecStartPost"):
            for line in unit.get(key, []):
                m = re.search(r"%h/kb/repo/(\S+\.sh)", line)
                if not m:
                    continue
                path = REPO / m.group(1)
                assert path.exists(), f"{name} {key} points at missing {path}"
                assert path.stat().st_mode & stat.S_IXUSR, f"{path} is not executable"


# --- failures are never silent ------------------------------------------------


@pytest.mark.parametrize("name", SERVICES)
def test_every_long_running_unit_notifies_on_failure(name):
    """Nothing here runs where a human is watching. Without OnFailure, a failed
    backup is discovered by attempting a restore."""
    assert values(name, "Unit", "OnFailure") == [ON_FAILURE], (
        f"{name} fails silently; add OnFailure={ON_FAILURE}"
    )


def test_the_notifier_exists_as_a_template_taking_the_failed_unit():
    unit = parse_unit(NOTIFIER)
    assert (UNIT_DIR / NOTIFIER).name.endswith("@.service"), "must be a template unit"
    assert unit["Service"]["Type"] == ["oneshot"]
    exec_start = unit["Service"]["ExecStart"][0]
    assert exec_start.endswith(" %i"), "the notifier must be told WHICH unit failed"
    assert "kb-notify-fail.sh" in exec_start


def test_the_notifier_never_notifies_about_itself():
    """OnFailure on the notifier is a loop; the script exits 0 regardless so the
    failed unit's own status stays the one visible in --failed."""
    assert "OnFailure" not in parse_unit(NOTIFIER).get("Unit", {})
    assert NOTIFIER_SCRIPT.read_text().rstrip().endswith("exit 0")


def test_the_notifier_keeps_the_ntfy_credential_out_of_argv():
    """argv is world-readable through /proc and shows up in `bash -x` traces —
    which is exactly how an ntfy admin password leaked into a transcript on
    2026-07-31. curl must read its auth from a config on stdin (`-K -`)."""
    body = NOTIFIER_SCRIPT.read_text()
    assert "-K -" in body, "auth must come from a curl config on stdin"
    lines = body.splitlines()
    curl_lines = [ln for ln in lines if "curl " in ln and not ln.strip().startswith("#")]
    assert curl_lines
    for line in curl_lines:
        assert "NTFY_TOKEN" not in line, f"credential on the curl command line: {line}"
        assert "NTFY_PASSWORD" not in line, f"credential on the curl command line: {line}"
        assert not re.search(r"-u\s*[\"']?\$", line), f"basic auth in argv: {line}"


def test_the_notifier_is_silent_when_no_channel_is_configured():
    """A box without notify.env must not turn every failure into a second one."""
    body = NOTIFIER_SCRIPT.read_text()
    assert re.search(r"\[ -r \"\$CFG\" \] \|\| exit 0", body)
    assert re.search(r"NTFY_TOPIC.*\|\| exit 0", body)


# --- backup no longer rides on the nightly ------------------------------------


def test_backup_has_its_own_timer():
    """The defect: backup existed ONLY as kb-consolidate's ExecStartPost, so it
    could not happen at all unless a 4.5–7h pass ran to completion."""
    timer = parse_unit("kb-backup.timer")["Timer"]
    assert timer["Unit"] == ["kb-backup.service"]
    assert timer["OnCalendar"], "the backup timer has no schedule"
    assert timer["Persistent"] == ["true"], (
        "a window missed while the machine was off must be caught up — that is "
        "precisely the post-power-cut case"
    )


def test_backup_runs_BEFORE_the_nightly_and_cannot_slide_into_it():
    """pg_dump takes no application advisory lock (scripts/backup.sh shells out to
    `docker exec kb-db pg_dump`), so it does not need the drain to be idle — but
    running first still means a smaller dump and no disk contention with the 14B."""
    backup = parse_unit("kb-backup.timer")["Timer"]
    start = _daily_minute(backup["OnCalendar"][0])
    jitter = int(backup.get("RandomizedDelaySec", ["0"])[0]) // 60
    consolidate = _daily_minute(parse_unit("kb-consolidate.timer")["Timer"]["OnCalendar"][0])
    assert start < consolidate, "the backup timer must fire before the consolidation"
    assert start + jitter <= consolidate, (
        f"RandomizedDelaySec can push the backup to {start + jitter} min, "
        f"into the {consolidate} min consolidation window"
    )


def test_backup_pg_dump_needs_no_application_lock():
    """The claim the schedule rests on, asserted against the script itself."""
    body = (REPO / "scripts" / "backup.sh").read_text()
    assert "docker exec kb-db pg_dump" in body
    assert "advisory" not in body.lower()


def test_the_post_run_backup_is_kept_as_a_bonus():
    """Belt and braces: the timer is the strategy, the post-run dump captures the
    rows the nightly just rewrote."""
    assert any(
        "backup.sh" in line for line in values("kb-consolidate.service", "Service", "ExecStartPost")
    )


# --- live-only drift, folded back into the repo -------------------------------


def test_the_boot_catchup_timer_is_in_the_repo():
    """Persistent=true covers a MISSED window, not a run that started and was
    killed 32 minutes in (2026-07-30). This timer covers the second case; it
    existed only on the live machine until 2026-08-01."""
    timer = parse_unit("kb-consolidate-boot.timer")["Timer"]
    assert timer["OnBootSec"], "no OnBootSec — this timer's entire purpose"
    assert timer["Unit"] == ["kb-consolidate.service"]
    assert parse_unit("kb-consolidate-boot.timer")["Install"]["WantedBy"] == ["timers.target"]


def test_consolidate_waits_for_readiness_not_a_phantom_target():
    """network-online.target does not exist in a systemd --user manager (the repo
    documents this in kb-cloudflared-refresh.service), so ordering against it was
    a no-op — the 2026-07-31 post-power-cut failure would have recurred. Real
    readiness is a bounded ExecStartPre retry on the dockerized Postgres, which
    FAILS the unit (and fires OnFailure=) instead of silently starting early."""
    for name in ("kb-consolidate.service", "kb-backup.service"):
        after = " ".join(values(name, "Unit", "After"))
        wants = " ".join(values(name, "Unit", "Wants"))
        assert "network-online.target" not in after, name
        assert "network-online.target" not in wants, name
        pres = " ".join(values(name, "Service", "ExecStartPre"))
        assert "pg_isready" in pres, f"{name} has no readiness wait"
        assert "exit 1" in pres, f"{name}'s wait cannot fail the unit"


def test_the_tiktoken_cache_is_persistent_across_boots():
    """The default cache lives under /tmp — tmpfs here — so every boot wiped it
    and the first chunker call re-downloaded the BPE. A catch-up with no network
    yet hard-failed on exactly that."""
    env = values("kb-consolidate.service", "Service", "Environment")
    cache = [v.split("=", 1)[1] for v in env if v.startswith("TIKTOKEN_CACHE_DIR=")]
    assert cache, "TIKTOKEN_CACHE_DIR is unpinned — it defaults into tmpfs"
    path = cache[0]
    assert not re.match(r"(%T|%V|/tmp|/var/tmp|/run|/dev/shm)", path), (
        f"{path} is volatile — it does not survive a reboot"
    )
    assert path.startswith("%h/"), "must be under the user's home to be persistent"


def test_the_run_seeds_its_own_tiktoken_cache_from_the_vendored_copy():
    """Pinning TIKTOKEN_CACHE_DIR overrides kbcore.chunk's vendored-directory
    fallback, so the unit has to supply the fallback itself — otherwise an empty
    cache dir sends the first chunk of the night to the network, which is the
    failure being fixed, not a new safety net."""
    pre = " ".join(values("kb-consolidate.service", "Service", "ExecStartPre"))
    assert "vendor/tiktoken" in pre and "kb/cache/tiktoken" in pre
    assert "cp -n" in pre, "seeding must never overwrite a warm cache"
    assert "|| true" in pre, "a cold-cache copy must not fail the nightly"


def test_the_installer_prewarms_the_tiktoken_cache_from_the_vendored_copy():
    text = INSTALLER.read_text()
    assert "kb/cache/tiktoken" in text
    assert "vendor/tiktoken" in text, "pre-warm must not need the network"
    assert (REPO / "vendor" / "tiktoken").is_dir(), "the vendored fallback is missing"
    assert any((REPO / "vendor" / "tiktoken").iterdir()), "the vendored cache is empty"


# --- the refresh that had never worked ----------------------------------------


def test_the_edge_refresh_pins_the_compose_project():
    """`-f %h/kb/repo/docker-compose.yml` makes compose derive the project name
    from that directory — and %h/kb/repo is a SYMLINK, so it came out as "repo"
    while the running stack is the checkout's own name. Every invocation
    addressed an empty project: the pull fetched nothing and `up -d` would have
    created a SECOND edge container instead of refreshing the live one."""
    unit = parse_unit("kb-cloudflared-refresh.service")["Service"]
    compose_lines = [
        line
        for key in ("ExecStart", "ExecStartPre", "ExecStartPost")
        for line in unit.get(key, [])
        if "docker compose" in line
    ]
    assert compose_lines, "no compose invocations to check"
    env = unit.get("Environment", [])
    pinned = [v.split("=", 1)[1] for v in env if v.startswith("COMPOSE_PROJECT_NAME=")]
    if not pinned:
        assert all(re.search(r"\s-p\s+\S+", line) for line in compose_lines), (
            "neither COMPOSE_PROJECT_NAME nor -p: this addresses the wrong project"
        )
        return
    name = pinned[0]
    assert name and name != "repo", "'repo' is the SYMLINK-derived name that made this unit a no-op"
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name), f"not a valid compose project: {name!r}"


def test_the_compose_file_does_not_set_a_project_name_of_its_own():
    """If it did, the unit's pin would be redundant at best and contradictory at
    worst — so this is the assumption the pin rests on."""
    for line in (REPO / "docker-compose.yml").read_text().splitlines():
        assert not re.match(r"^name:", line), "compose declares its own project name"


# --- hygiene ------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", sorted(UNIT_DIR.iterdir(), key=lambda p: p.name), ids=lambda p: p.name
)
def test_no_unit_file_carries_a_credential(path):
    """Units are committed; a secret in one is a secret in git history. Checked
    with the project's own scrubber, so the two definitions of 'credential'
    cannot drift apart."""
    _clean, redactions = scrub(path.read_text())
    assert redactions == {}, f"{path.name} contains something the scrubber flags: {redactions}"

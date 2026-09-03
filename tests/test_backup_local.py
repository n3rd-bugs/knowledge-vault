"""The boot-path backup (review #8 H10a): backup.sh --local-only, its unit, and
the guard's rewiring away from the offsite-pushing unit.

On 2026-09-01 the boot catch-up drain waited 2h59m behind kb-backup.service's
offsite push on modem WAN, because the boot guard pulled that unit in. The fix
is a LOCAL-ONLY switch honoured AFTER backup.sh re-sources .env (a unit's
Environment= is overridden by EnvironmentFile= and by that re-source, so an env
override would be silently undone), a unit that passes it, and a guard that
Wants the local unit — never one that pushes offsite.

backup.sh runs hermetically here: a `docker` stub on PATH serves a tiny
pg_dump-shaped text, real zstd/awk/tar do the rest, KB_ENV_FILE points at a
test env (the repo .env is never read), and the offsite leg is proven to be
ATTEMPTED without the flag (it fails fast, offline, on the empty recipient) and
SKIPPED with it.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKUP = REPO / "scripts" / "backup.sh"
UNIT_DIR = REPO / "systemd"
LOCAL_UNIT = "kb-backup-local.service"
GUARD = "kb-consolidate-boot.service"

FAKE_DUMP = (
    "--\n-- PostgreSQL database dump\n--\n"
    "COPY public.items (id, title) FROM stdin;\n1\tone\n2\ttwo\n3\tthree\n\\.\n"
    "COPY public.chunks (id, item_id, embedding) FROM stdin;\n1\t1\t\\N\n2\t1\t[0.1,0.2]\n\\.\n"
    "COPY public.relations (src, dst) FROM stdin;\n1\t2\n\\.\n"
)


def parse_unit(name: str) -> dict[str, dict[str, list[str]]]:
    """Minimal systemd unit parser: ``{section: {key: [values]}}`` (repeats kept)."""
    out: dict[str, dict[str, list[str]]] = {}
    section = ""
    for raw in (UNIT_DIR / name).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            out.setdefault(section, {})
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            out.setdefault(section, {}).setdefault(key.strip(), []).append(value.strip())
    return out


def _rig(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    home, backups, raw, bindir = (tmp_path / n for n in ("home", "backups", "raw", "bin"))
    for d in (home, backups, raw, bindir):
        d.mkdir()
    (raw / "transcript.txt").write_text("original bytes\n")
    dump = tmp_path / "fake.sql"
    dump.write_text(FAKE_DUMP)
    docker = bindir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        f'  "exec kb-db pg_dump"*) cat "{dump}";;\n'
        '  *) echo "docker stub: unexpected: $*" >&2; exit 9;;\n'
        "esac\n"
    )
    docker.chmod(0o755)
    envfile = tmp_path / "test.env"
    envfile.write_text(
        f"KB_BACKUPS={backups}\nKB_RAW={raw}\n"
        "KB_OFFSITE_ENABLED=true\n"  # the env file SAYS push — the flag must win
        "KB_OFFSITE_CONF=/dev/null\n"  # not a regular file: the web overlay is skipped
        f"KB_RCLONE_CONF_DIR={tmp_path / 'no-rclone'}\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("KB_")}
    env.update(
        {
            "PATH": f"{bindir}{os.pathsep}{env.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(home),
            "KB_ENV_FILE": str(envfile),
        }
    )
    return backups, env


def _run(env, *args):
    return subprocess.run(
        ["bash", str(BACKUP), *args], env=env, capture_output=True, text=True, timeout=120
    )


def _dumps(backups: Path) -> list[Path]:
    return sorted(backups.glob("kb-*.sql.zst"))


def _code(script: Path) -> str:
    """The script minus comment lines — header prose mentions the words asserted on."""
    return "\n".join(ln for ln in script.read_text().splitlines() if not ln.strip().startswith("#"))


# ------------------------------------------------------------------ backup.sh


def test_the_script_parses_clean_and_is_executable():
    assert os.access(BACKUP, os.X_OK), "systemd ExecStart needs +x"
    proc = subprocess.run(["bash", "-n", str(BACKUP)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_local_only_skips_the_push_even_when_the_env_file_enables_it(tmp_path):
    backups, env = _rig(tmp_path)
    proc = _run(env, "--local-only")
    assert proc.returncode == 0, proc.stderr
    (dump,) = _dumps(backups)
    sidecar = Path(f"{dump}.counts").read_text()
    assert sidecar == "items=3\nchunks=2\nrelations=1\nchunks_null_embedding=1\n"
    assert list(backups.glob("kb-raw-*.tar.zst")), "the raw archive is part of the local leg"
    assert "offsite push skipped" in proc.stdout
    assert "OFFSITE" not in proc.stderr and "offsite push OK" not in proc.stdout
    assert oct(dump.stat().st_mode & 0o777) == "0o600"


def test_without_the_flag_the_same_env_file_drives_the_push(tmp_path):
    """The control: KB_OFFSITE_ENABLED=true from the env file IS honoured when the
    flag is absent — the leg is attempted (and fails fast, offline, on the empty
    recipient), so the skip above is the flag's doing, not the env's."""
    backups, env = _rig(tmp_path)
    proc = _run(env)
    assert proc.returncode == 1
    assert "OFFSITE PUSH FAILED" in proc.stderr
    assert re.search(r"after \d+s", proc.stderr), "the leg logs its wall time (H10a/H12)"
    assert "offsite leg took" in proc.stdout
    assert _dumps(backups), "the local backup is intact when the push fails"


def test_an_unknown_flag_is_refused_before_any_work(tmp_path):
    backups, env = _rig(tmp_path)
    proc = _run(env, "--offsite-only")
    assert proc.returncode == 2 and "usage" in proc.stderr
    assert not _dumps(backups)


def test_the_flag_is_parsed_after_both_config_sources():
    """The brief's trap: backup.sh re-sources .env under set -a, then overlays the
    web-written offsite config — anything decided BEFORE those lines can be
    clobbered by them, which is why this is a flag and not an env override."""
    body = _code(BACKUP)
    flag_at = body.index("--local-only)")
    assert body.index('. "${ENV_FILE}"') < flag_at
    assert body.index('done < "${KB_OFFSITE_CONF}"') < flag_at
    assert flag_at < body.index("pg_dump"), "an unknown flag must be refused before any work"


def test_concurrent_local_legs_take_turns_and_the_lock_is_released_before_the_push(tmp_path):
    """Two backup.sh (the boot-path unit + a Persistent catch-up of the nightly
    unit) write the same kb-<date>.sql.zst; the lock serialises them. It is
    released before the offsite leg so a boot dump never waits behind the WAN."""
    backups, env = _rig(tmp_path)
    lock = backups / ".backup.lock"
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        proc = _run({**env, "KB_BACKUP_LOCK_WAIT": "1"}, "--local-only")
    assert proc.returncode == 1 and "held the lock" in proc.stderr
    assert not _dumps(backups), "no dump is written while another instance holds the lock"
    body = _code(BACKUP)
    assert body.index("flock -w") < body.index("pg_dump")
    assert body.index("flock -u 9") < body.index('offsite.sh" push')
    assert _run(env, "--local-only").returncode == 0, "and once released the same rig succeeds"


# ----------------------------------------------------------------- the units


def test_the_local_unit_runs_backup_sh_with_the_flag_and_nothing_else():
    unit = parse_unit(LOCAL_UNIT)
    svc = unit["Service"]
    assert svc["Type"] == ["oneshot"]
    assert svc["ExecStart"] == ["%h/kb/repo/scripts/backup.sh --local-only"]
    pre = " ".join(svc.get("ExecStartPre", []))
    assert "pg_isready" in pre and "exit 1" in pre, "same bounded readiness wait as kb-backup"
    assert "EnvironmentFile" in svc
    assert not any(v.startswith("KB_OFFSITE") for v in svc.get("Environment", [])), (
        "Environment= cannot beat EnvironmentFile= + the re-source; the FLAG is the switch"
    )
    assert unit["Unit"]["OnFailure"] == ["kb-notify-fail@%n.service"]
    assert "Install" not in unit, "only the guard's Wants= starts it"
    assert "kb-backup.service" not in unit["Unit"].get("After", []), (
        "After=kb-backup.service would park the boot path behind a Persistent push"
    )


def test_the_guard_wants_the_local_unit_and_never_waits_on_a_unit_that_pushes_offsite():
    """Mechanical: every unit the guard orders After (i.e. WAITS for) must run
    backup.sh with --local-only if it runs backup.sh at all."""
    guard = parse_unit(GUARD)["Unit"]
    assert "kb-backup-local.service" in guard["Wants"]
    assert "kb-backup-local.service" in guard["After"]
    assert "kb-backup.service" not in guard.get("Wants", []) + guard.get("After", [])
    for name in set(guard.get("After", [])):
        if not (UNIT_DIR / name).exists():
            continue
        svc = parse_unit(name)["Service"]
        for line in svc.get("ExecStart", []) + svc.get("ExecStartPost", []):
            if "backup.sh" in line:
                assert "--local-only" in line, f"{GUARD} waits on {name}, which pushes: {line}"


def test_the_nightly_backup_unit_keeps_its_offsite_leg():
    svc = parse_unit("kb-backup.service")["Service"]
    assert svc["ExecStart"] == ["%h/kb/repo/scripts/backup.sh"], "the timer's run must still push"


def test_the_guard_comment_carries_the_correction():
    """'One extra pg_dump per boot — cheap' was only true without the offsite leg."""
    text = (UNIT_DIR / GUARD).read_text()
    assert "kb-backup-local.service" in text and "2h59m" in text and "offsite" in text

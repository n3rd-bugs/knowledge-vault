"""restore_test.sh (review #8 H12): local-calendar month parity and the drill-mode
visibility line.

The monthly drill fires 04:30 PKT on the 1st — 23:30 UTC on the LAST day of the
previous month — so `date -u +%m` read August on Sep 1 and demanded --offsite
('even month wanted --offsite but KB_AGE_IDENTITY is absent'). The month is now
the LOCAL calendar's, and every drill prints one visibility line so a local
fallback can never pass for an offsite proof (KB 28833: the 10-01 drill must run
with the identity supplied).

Runs the script's --dry-run path hermetically: KB_ENV_FILE points at a test env
(never the repo .env), KB_DRILL_MONTH pins the month, and a `docker` stub on PATH
fails loudly if the dry-run ever reaches it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "restore_test.sh"


def _rig(tmp_path: Path, *, identity: bool = False, month: str = "09") -> dict[str, str]:
    home, bindir = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(
        '#!/usr/bin/env bash\necho "docker stub: a dry-run must never reach docker: $*" >&2\n'
        "exit 9\n"
    )
    docker.chmod(0o755)
    lines = [f"KB_BACKUPS={tmp_path / 'backups'}"]
    if identity:
        key = tmp_path / "age-identity.txt"
        key.write_text("AGE-SECRET-KEY-TESTSTUB\n")
        lines.append(f"KB_AGE_IDENTITY={key}")
    (tmp_path / "test.env").write_text("\n".join(lines) + "\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("KB_")}
    env.update(
        {
            "PATH": f"{bindir}{os.pathsep}{env.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(home),
            "KB_ENV_FILE": str(tmp_path / "test.env"),
            "KB_DRILL_MONTH": month,
        }
    )
    return env


def _run(env, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60
    )


def test_the_script_parses_clean_and_is_executable():
    assert os.access(SCRIPT, os.X_OK), "systemd ExecStart needs +x"
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_an_odd_month_auto_drill_is_local(tmp_path):
    proc = _run(_rig(tmp_path, month="09"), "--auto", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "restore_test: drill mode=local identity=absent month=09" in proc.stdout
    assert "even month wanted" not in proc.stdout
    assert "docker stub" not in proc.stderr, "a dry-run must stop before docker"


def test_an_even_month_without_the_identity_falls_back_to_local_and_says_so(tmp_path):
    proc = _run(_rig(tmp_path, month="08"), "--auto", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "even month wanted --offsite but KB_AGE_IDENTITY is absent" in proc.stdout
    assert "drill mode=local identity=absent month=08" in proc.stdout


def test_an_even_month_with_the_identity_drills_offsite(tmp_path):
    proc = _run(_rig(tmp_path, month="08", identity=True), "--auto", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "drill mode=offsite identity=present month=08" in proc.stdout
    assert "even month wanted" not in proc.stdout


def test_explicit_offsite_and_plain_local_both_print_the_visibility_line(tmp_path):
    env = _rig(tmp_path, month="09", identity=True)
    assert "drill mode=offsite identity=present" in _run(env, "--offsite", "--dry-run").stdout
    assert "drill mode=local identity=present" in _run(env, "--dry-run").stdout


def test_an_unknown_argument_is_refused(tmp_path):
    proc = _run(_rig(tmp_path), "--bogus")
    assert proc.returncode == 2 and "usage" in proc.stderr


def test_a_malformed_month_override_falls_back_to_the_clock(tmp_path):
    proc = _run(_rig(tmp_path, month="nope"), "--auto", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "month=nope" not in proc.stdout and "drill mode=" in proc.stdout


def test_the_month_is_the_local_calendars():
    body = "\n".join(ln for ln in SCRIPT.read_text().splitlines() if not ln.strip().startswith("#"))
    assert "date -u +%m" not in body, "UTC month is off by one at 04:30 PKT on the 1st"
    assert 'DRILL_MONTH="${KB_DRILL_MONTH:-$(date +%m)}"' in body
    assert body.index("drill mode=") < body.index("docker run"), "visibility line comes first"

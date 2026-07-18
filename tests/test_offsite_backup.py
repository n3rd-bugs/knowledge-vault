"""Encrypted offsite backups (PLAN.md §6d): age encrypt -> rclone push -> fetch.

Runs WITHOUT credentials, network, docker, or the live DB:

- KB_RCLONE_REMOTE points at a temp directory (rclone's local backend semantics);
- when age/rclone are not installed, recording STUBS are injected through the
  scripts' KB_AGE_BIN / KB_RCLONE_BIN seams (the stub "encryption" is a
  reversible base64 wrap so round-trip plumbing is still proven byte-for-byte);
- the real-crypto round-trip test SKIPs loudly when age/age-keygen are missing;
- KB_ENV_FILE=/dev/null and stripping inherited KB_* keep every run hermetic —
  the repo's real .env is never read, no real key or remote is ever touched.

Only synthetic dump bytes are used; scripts/backup.sh itself (docker pg_dump)
is exercised via `bash -n` + static guard checks, never against the live DB.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
OFFSITE = REPO / "scripts" / "offsite.sh"
BACKUP = REPO / "scripts" / "backup.sh"
RESTORE = REPO / "scripts" / "restore_test.sh"
INSTALL = REPO / "scripts" / "install_systemd.sh"

HAVE = {tool: shutil.which(tool) is not None for tool in ("age", "age-keygen", "rclone")}

# High-entropy marker: if these raw bytes ever show up on the "remote", the
# plaintext leaked. (Both real age ciphertext and the stub's base64 wrap
# guarantee the raw byte sequence cannot appear in the encrypted artifact.)
MARKER = b"KB-PLAINTEXT-MARKER-8d1f4c0a"
STUB_RECIPIENT = "age1stubrecipient000000000000000000000000000000000000000000000"

STUB_AGE = """#!/usr/bin/env bash
set -euo pipefail
printf 'age %s\\n' "$*" >> "${STUB_LOG}"
mode=enc recip="" ident="" out="" inp=""
while (( $# )); do
  case "$1" in
    -d) mode=dec; shift ;;
    -r) recip="$2"; shift 2 ;;
    -i) ident="$2"; shift 2 ;;
    -o) out="$2"; shift 2 ;;
    *) inp="$1"; shift ;;
  esac
done
if [[ "${mode}" == enc ]]; then
  { printf 'AGESTUB %s\\n' "${recip}"; base64 "${inp}"; } > "${out}"
else
  want="$(awk 'NR==1 {print $2}' "${ident}")"
  have="$(awk 'NR==1 {print $2}' "${inp}")"
  if [[ -z "${want}" || "${have}" != "${want}" ]]; then
    echo "stub age: wrong identity" >&2; exit 1
  fi
  tail -n +2 "${inp}" | base64 -d > "${out}"
fi
"""

STUB_RCLONE = """#!/usr/bin/env bash
set -euo pipefail
printf 'rclone %s\\n' "$*" >> "${STUB_LOG}"
cmd="$1"; shift
if [[ "${STUB_FAIL_CMD:-}" == "${cmd}" ]]; then
  echo "stub rclone: injected failure for ${cmd}" >&2; exit 1
fi
args=()
for a in "$@"; do [[ "$a" == --* ]] || args+=("$a"); done
case "${cmd}" in
  copy)       mkdir -p "${args[1]}"; cp "${args[0]}" "${args[1]}/" ;;
  lsf)        ls -1 "${args[0]}" 2>/dev/null || true ;;
  deletefile) rm -f "${args[0]}" ;;
  *)          echo "stub rclone: unsupported ${cmd}" >&2; exit 9 ;;
esac
"""


def _write_stub(bindir: Path, name: str, body: str) -> Path:
    path = bindir / name
    path.write_text(body)
    path.chmod(0o755)
    return path


def _crypto(tmp_path: Path) -> dict:
    """age setup: real keypair when installed, else the reversible stub."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if HAVE["age"] and HAVE["age-keygen"]:
        ident = tmp_path / "age-test.key"
        res = subprocess.run(
            ["age-keygen", "-o", str(ident)], capture_output=True, text=True, check=True
        )
        # age-keygen prints "Public key: age1..." on stderr.
        recip = res.stderr.strip().split()[-1]
        return {"mode": "real", "env": {}, "recipient": recip, "identity": ident}
    _write_stub(bindir, "age", STUB_AGE)
    ident = tmp_path / "age-stub.key"
    ident.write_text(f"AGE-SECRET-KEY-STUB {STUB_RECIPIENT}\n")
    return {
        "mode": "stub",
        "env": {"KB_AGE_BIN": str(bindir / "age")},
        "recipient": STUB_RECIPIENT,
        "identity": ident,
    }


def _transport(tmp_path: Path) -> dict:
    """rclone setup: the real binary against a plain local path, else the stub."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if HAVE["rclone"]:
        return {"mode": "real", "env": {}}
    _write_stub(bindir, "rclone", STUB_RCLONE)
    return {"mode": "stub", "env": {"KB_RCLONE_BIN": str(bindir / "rclone")}}


def _run_offsite(args: list[str], overrides: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("KB_")}
    env["KB_ENV_FILE"] = "/dev/null"  # never read the repo's real .env
    env.setdefault("STUB_LOG", str(tmp_path / "stub.log"))
    env.update(overrides)
    return subprocess.run(
        ["bash", str(OFFSITE), *args], capture_output=True, text=True, env=env, timeout=60
    )


def _setup(tmp_path: Path) -> tuple[Path, Path, dict, dict]:
    """Synthetic dump + fake remote dir + tool setups + a ready env dict."""
    dump = tmp_path / "kb-2026-07-18.sql.zst"
    dump.write_bytes(MARKER + os.urandom(4096))
    remote = tmp_path / "remote"
    remote.mkdir()
    crypto, transport = _crypto(tmp_path), _transport(tmp_path)
    env = {
        "KB_OFFSITE_ENABLED": "true",
        "KB_RCLONE_REMOTE": str(remote),
        "KB_AGE_RECIPIENT": crypto["recipient"],
        "KB_AGE_IDENTITY": str(crypto["identity"]),
        "KB_OFFSITE_KEEP": "30",
        **crypto["env"],
        **transport["env"],
    }
    return dump, remote, crypto, env


def test_binary_presence_report():
    """Always passes; documents what the rest of the suite could exercise."""
    lines = [f"  {tool}: {'PRESENT' if ok else 'ABSENT'}" for tool, ok in HAVE.items()]
    mode = (
        "real age+rclone"
        if HAVE["age"] and HAVE["rclone"]
        else "STUBS via KB_AGE_BIN/KB_RCLONE_BIN seams"
    )
    print("\noffsite tool availability:\n" + "\n".join(lines), file=sys.stderr)
    print(f"  => flow tests run with: {mode}", file=sys.stderr)
    if not (HAVE["age"] and HAVE["age-keygen"]):
        print("  => SKIP: real-crypto round-trip NOT exercised (age missing)", file=sys.stderr)


def test_push_encrypts_before_copy_and_no_plaintext_leaves(tmp_path):
    dump, remote, crypto, env = _setup(tmp_path)
    before = dump.read_bytes()

    res = _run_offsite(["push", str(dump)], env, tmp_path)
    assert res.returncode == 0, res.stderr

    # Exactly one object remote-side, and it is the .age ciphertext.
    objs = sorted(p.name for p in remote.iterdir())
    assert objs == ["kb-2026-07-18.sql.zst.age"]
    cipher = (remote / objs[0]).read_bytes()
    assert MARKER not in cipher, "plaintext marker leaked to the remote"
    assert cipher != before
    header = b"age-encryption.org" if crypto["mode"] == "real" else b"AGESTUB"
    assert cipher.startswith(header)

    # Local dump untouched; no bare plaintext .sql.zst remote-side.
    assert dump.read_bytes() == before
    assert not any(p.name.endswith(".sql.zst") for p in remote.iterdir())

    # With stubs the recorded log proves ordering: encrypt BEFORE copy, and the
    # copy source is the .age artifact (never the plaintext dump path).
    if crypto["mode"] == "stub":
        log = (tmp_path / "stub.log").read_text().splitlines()
        age_i = next(i for i, ln in enumerate(log) if ln.startswith("age "))
        cp_i = next(i for i, ln in enumerate(log) if ln.startswith("rclone copy "))
        assert age_i < cp_i
        assert f"-r {crypto['recipient']}" in log[age_i]
        copy_src = log[cp_i].split()[2]
        assert copy_src.endswith(".sql.zst.age")
        assert copy_src != str(dump)
        assert not any(str(dump) in ln for ln in log if ln.startswith("rclone "))


def test_push_prunes_remote_to_offsite_keep(tmp_path):
    dump, remote, _, env = _setup(tmp_path)
    for day in ("01", "02", "03", "04", "05"):
        (remote / f"kb-2026-06-{day}.sql.zst.age").write_bytes(b"old")
    (remote / "unrelated.txt").write_bytes(b"keep me")  # never touched by prune
    env["KB_OFFSITE_KEEP"] = "3"

    res = _run_offsite(["push", str(dump)], env, tmp_path)
    assert res.returncode == 0, res.stderr

    objs = sorted(p.name for p in remote.iterdir())
    assert objs == [
        "kb-2026-06-04.sql.zst.age",
        "kb-2026-06-05.sql.zst.age",
        "kb-2026-07-18.sql.zst.age",
        "unrelated.txt",
    ]


def test_fetch_roundtrip_byte_identical(tmp_path):
    dump, _, crypto, env = _setup(tmp_path)
    assert _run_offsite(["push", str(dump)], env, tmp_path).returncode == 0

    dest = tmp_path / "restore"
    dest.mkdir()
    res = _run_offsite(["fetch", str(dest)], env, tmp_path)
    assert res.returncode == 0, res.stderr

    out = Path(res.stdout.strip())  # decrypted path is the ONLY stdout line
    assert out.name == "kb-2026-07-18.sql.zst"
    assert out.read_bytes() == dump.read_bytes(), "round-trip not byte-identical"
    if crypto["mode"] == "stub":
        print("NOTE: round-trip proven via stub (age absent) — plumbing only", file=sys.stderr)


def test_fetch_with_wrong_identity_fails(tmp_path):
    dump, _, crypto, env = _setup(tmp_path)
    assert _run_offsite(["push", str(dump)], env, tmp_path).returncode == 0

    wrong = tmp_path / "wrong.key"
    if crypto["mode"] == "real":
        subprocess.run(["age-keygen", "-o", str(wrong)], capture_output=True, check=True)
    else:
        wrong.write_text("AGE-SECRET-KEY-STUB age1someotherrecipient00000000000000000000000\n")
    env["KB_AGE_IDENTITY"] = str(wrong)

    dest = tmp_path / "restore"
    dest.mkdir()
    res = _run_offsite(["fetch", str(dest)], env, tmp_path)
    assert res.returncode != 0
    assert "decryption failed" in res.stderr


def test_missing_rclone_fails_loudly_but_local_dump_intact(tmp_path):
    dump, remote, _, env = _setup(tmp_path)
    before = dump.read_bytes()
    env["KB_RCLONE_BIN"] = str(tmp_path / "no-such-rclone")

    res = _run_offsite(["push", str(dump)], env, tmp_path)
    assert res.returncode != 0
    assert "rclone not found" in res.stderr
    assert dump.read_bytes() == before
    assert list(remote.iterdir()) == []


def test_copy_failure_exits_nonzero_and_leaves_local_intact(tmp_path):
    dump, remote, _, env = _setup(tmp_path)
    before = dump.read_bytes()
    if HAVE["rclone"]:
        # Real rclone: point the remote at a guaranteed-unwritable path.
        env["KB_RCLONE_REMOTE"] = "/proc/kb-no-such-remote"
    else:
        env["STUB_FAIL_CMD"] = "copy"

    res = _run_offsite(["push", str(dump)], env, tmp_path)
    assert res.returncode != 0
    assert "local backup is intact" in res.stderr
    assert dump.read_bytes() == before
    if not HAVE["rclone"]:
        assert list(remote.iterdir()) == []


def test_disabled_switch_is_a_noop(tmp_path):
    dump, remote, _, env = _setup(tmp_path)
    env["KB_OFFSITE_ENABLED"] = "false"
    # Binaries must not even be needed when the switch is off (additive design).
    env["KB_RCLONE_BIN"] = str(tmp_path / "absent")
    env["KB_AGE_BIN"] = str(tmp_path / "absent")

    res = _run_offsite(["push", str(dump)], env, tmp_path)
    assert res.returncode == 0, res.stderr
    assert "skipping offsite push" in res.stderr
    assert list(remote.iterdir()) == []


@pytest.mark.skipif(
    not (HAVE["age"] and HAVE["age-keygen"]),
    reason="SKIP: age/age-keygen not installed — real age crypto round-trip NOT "
    "exercised on this box (stub-based plumbing tests above still ran)",
)
def test_real_age_crypto_roundtrip(tmp_path):
    """Same flow as the stub round-trip but guaranteed to use REAL age."""
    dump, _, crypto, env = _setup(tmp_path)
    assert crypto["mode"] == "real"
    assert _run_offsite(["push", str(dump)], env, tmp_path).returncode == 0
    dest = tmp_path / "restore"
    dest.mkdir()
    res = _run_offsite(["fetch", str(dest)], env, tmp_path)
    assert res.returncode == 0, res.stderr
    assert Path(res.stdout.strip()).read_bytes() == dump.read_bytes()


def test_scripts_syntax_and_static_guards():
    for script in (OFFSITE, BACKUP, RESTORE, INSTALL):
        res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert res.returncode == 0, f"bash -n {script.name}: {res.stderr}"

    # backup.sh: offsite is guarded by the switch, delegated to offsite.sh, and
    # backup.sh itself never invokes rclone/age (encryption lives in one place).
    backup = BACKUP.read_text()
    assert 'offsite.sh" push' in backup
    assert "KB_OFFSITE_ENABLED" in backup
    code_lines = [ln for ln in backup.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert not any("rclone" in ln or "age " in ln for ln in code_lines)

    restore = RESTORE.read_text()
    assert "--offsite" in restore
    assert 'offsite.sh" fetch' in restore

    # systemd: units exist, timer is daily+persistent+jittered, install-only.
    service = (REPO / "systemd" / "kb-backup.service").read_text()
    assert "scripts/backup.sh" in service
    timer = (REPO / "systemd" / "kb-backup.timer").read_text()
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec" in timer
    install = INSTALL.read_text()
    assert "systemd/kb-backup.service" in install
    assert "systemd/kb-backup.timer" in install
    enable_lines = [
        ln
        for ln in install.splitlines()
        if ln.strip().startswith("systemctl") and "enable" in ln and "kb-backup" in ln
    ]
    assert enable_lines == [], f"kb-backup must be install-only, found: {enable_lines}"

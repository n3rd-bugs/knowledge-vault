"""kbcore.offsite_config — the owner web backup-setup config layer.

Filesystem + subprocess only (no DB): validation, atomic 0600 writes, the
four-key allowlist, the WRITE-ONLY rclone credential, and the age keypair via a
stub binary. The security invariants under test: no secret is ever returned or
written where it must not be — the age PRIVATE identity is NEVER persisted, the
rclone credential is write-only, and write_config never emits KB_AGE_IDENTITY.
"""

from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest

from kbcore import offsite_config
from kbcore.config import settings
from kbcore.errors import InvalidOperation

# A valid-shaped age keypair the stub emits (public: age1 + 58 [0-9a-z];
# private: AGE-SECRET-KEY- + [0-9A-Z]+). Distinct, greppable sentinels.
STUB_RECIPIENT = "age1" + "q" * 58
STUB_IDENTITY = "AGE-SECRET-KEY-1" + "A" * 58
GOOD_REMOTE = "gdrive:kb-backups"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Point every offsite path at tmp_path and the age binary at a stub script."""
    conf = tmp_path / "offsite.env"
    rclone_dir = tmp_path / "rclone"
    backups = tmp_path / "backups"
    stub = tmp_path / "age-keygen-stub.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "cat <<'EOF'\n"
        "# created: 2026-07-21T00:00:00Z\n"
        f"# public key: {STUB_RECIPIENT}\n"
        f"{STUB_IDENTITY}\n"
        "EOF\n"
    )
    stub.chmod(0o755)
    monkeypatch.setattr(settings, "kb_offsite_conf", str(conf))
    monkeypatch.setattr(settings, "kb_rclone_conf_dir", str(rclone_dir))
    monkeypatch.setattr(settings, "kb_backups", backups)
    monkeypatch.setattr(settings, "kb_age_bin", str(stub))
    return SimpleNamespace(conf=conf, rclone_dir=rclone_dir, backups=backups, stub=stub)


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- write_config validation --------------------------------------------------


def test_write_config_rejects_bad_age_recipient(env):
    with pytest.raises(InvalidOperation) as ei:
        offsite_config.write_config(
            enabled=True, rclone_remote=GOOD_REMOTE, age_recipient="not-an-age-key", keep=30
        )
    assert "age_recipient" in str(ei.value)
    assert not env.conf.exists(), "no file may be written on a validation failure"


def test_write_config_rejects_bad_rclone_remote(env):
    with pytest.raises(InvalidOperation) as ei:
        offsite_config.write_config(
            enabled=True, rclone_remote="bad remote; rm -rf /", age_recipient="", keep=30
        )
    assert "rclone_remote" in str(ei.value)
    assert not env.conf.exists()


@pytest.mark.parametrize("keep", [0, -1, 366, 1000, True])
def test_write_config_rejects_keep_out_of_range(env, keep):
    with pytest.raises(InvalidOperation) as ei:
        offsite_config.write_config(
            enabled=True, rclone_remote=GOOD_REMOTE, age_recipient="", keep=keep
        )
    assert "keep" in str(ei.value)
    assert not env.conf.exists()


def test_valid_write_produces_0600_file_with_exactly_four_keys(env):
    offsite_config.write_config(
        enabled=True, rclone_remote=GOOD_REMOTE, age_recipient=STUB_RECIPIENT, keep=14
    )
    assert env.conf.exists()
    assert _mode(env.conf) == 0o600, "conf file must be mode 0600"
    text = env.conf.read_text()
    keys = {
        line.split("=", 1)[0]
        for line in text.splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    assert keys == {
        "KB_OFFSITE_ENABLED",
        "KB_RCLONE_REMOTE",
        "KB_AGE_RECIPIENT",
        "KB_OFFSITE_KEEP",
    }, "exactly the four allowlisted keys, no others"
    # the private-key env var is NEVER written, nor is any secret-shaped material
    assert "KB_AGE_IDENTITY" not in text
    assert "AGE-SECRET-KEY" not in text


def test_write_config_allows_empty_recipient_and_empty_remote(env):
    # Empty = 'not configured yet' — permitted so a keypair generated before the
    # remote is set still persists its recipient (never lose a shown-once key).
    offsite_config.write_config(enabled=False, rclone_remote="", age_recipient="", keep=30)
    text = env.conf.read_text()
    assert "KB_RCLONE_REMOTE=\n" in text or "KB_RCLONE_REMOTE=" in text
    assert "KB_AGE_RECIPIENT=" in text


def test_write_config_is_atomic_no_partial_on_error(env):
    # A good write, then a bad one: the file must be UNCHANGED (validation runs
    # before the single atomic replace).
    offsite_config.write_config(
        enabled=True, rclone_remote=GOOD_REMOTE, age_recipient=STUB_RECIPIENT, keep=14
    )
    before = env.conf.read_text()
    with pytest.raises(InvalidOperation):
        offsite_config.write_config(
            enabled=True, rclone_remote="bad remote", age_recipient="", keep=14
        )
    assert env.conf.read_text() == before, "the prior valid file must survive a failed write"


# --- store_rclone_cred (write-only) -------------------------------------------


def test_store_rclone_cred_json_writes_0600_and_flips_presence(env):
    secret = json.dumps({"type": "service_account", "private_key": "SECRET-SA-KEY-VALUE"})
    assert offsite_config.rclone_cred_present() is False
    ret = offsite_config.store_rclone_cred(secret)
    assert ret is None, "store_rclone_cred must never return the credential"
    cred_file = env.rclone_dir / "rclone.conf"
    assert cred_file.exists() and _mode(cred_file) == 0o600
    assert "SECRET-SA-KEY-VALUE" in cred_file.read_text(), "stored write-only on disk"
    assert offsite_config.rclone_cred_present() is True


def test_store_rclone_cred_accepts_rclone_conf_block(env):
    offsite_config.store_rclone_cred("[gdrive]\ntype = drive\nscope = drive.file\n")
    assert offsite_config.rclone_cred_present() is True


def test_store_rclone_cred_rejects_garbage_and_empty(env):
    with pytest.raises(InvalidOperation):
        offsite_config.store_rclone_cred("just some pasted text, not json or a conf block")
    with pytest.raises(InvalidOperation):
        offsite_config.store_rclone_cred("   ")
    assert offsite_config.rclone_cred_present() is False


def test_clear_rclone_cred(env):
    offsite_config.store_rclone_cred("[gdrive]\ntype = drive\n")
    assert offsite_config.rclone_cred_present() is True
    offsite_config.clear_rclone_cred()
    assert offsite_config.rclone_cred_present() is False
    offsite_config.clear_rclone_cred()  # idempotent, no error


def test_read_status_reports_cred_presence_without_the_value(env):
    secret = json.dumps({"type": "service_account", "private_key": "TOP-SECRET-SA"})
    offsite_config.store_rclone_cred(secret)
    status = offsite_config.read_status()
    assert status["rclone_cred_present"] is True
    assert "TOP-SECRET-SA" not in str(status), "read_status must never surface the credential"
    assert "private_key" not in str(status)


# --- generate_age_keypair (stubbed) -------------------------------------------


def test_generate_age_keypair_returns_pair_and_stores_nothing(env):
    recipient, identity = offsite_config.generate_age_keypair()
    assert recipient == STUB_RECIPIENT
    assert identity == STUB_IDENTITY
    # The private identity is NOT written anywhere by generate_age_keypair.
    assert not env.conf.exists(), "generate_age_keypair must persist nothing"
    assert not (env.rclone_dir / "rclone.conf").exists()


def test_caller_stores_only_recipient_never_identity(env):
    # Mirrors the web keygen flow: generate, then write_config stores ONLY the
    # public recipient. The private identity must appear in NO stored file.
    recipient, identity = offsite_config.generate_age_keypair()
    offsite_config.write_config(enabled=False, rclone_remote="", age_recipient=recipient, keep=30)
    conf_text = env.conf.read_text()
    assert recipient in conf_text, "the public recipient is stored"
    assert identity not in conf_text, "the private identity must never be stored"
    assert "AGE-SECRET-KEY" not in conf_text
    # nothing under the rclone dir either
    for p in env.rclone_dir.rglob("*") if env.rclone_dir.exists() else []:
        if p.is_file():
            assert identity not in p.read_text()


def test_generate_age_keypair_missing_binary_is_clean_error(env, monkeypatch):
    monkeypatch.setattr(settings, "kb_age_bin", str(env.stub) + "-does-not-exist")
    with pytest.raises(InvalidOperation) as ei:
        offsite_config.generate_age_keypair()
    assert "age" in str(ei.value).lower()


# --- read_status local-backup reporting ---------------------------------------


def test_read_status_reports_local_backups(env):
    env.backups.mkdir(parents=True)
    (env.backups / "kb-2026-07-19.sql.zst").write_bytes(b"dump-a")
    (env.backups / "kb-2026-07-20.sql.zst").write_bytes(b"dump-b-newer")
    (env.backups / "kb-raw-2026-07-20.tar.zst").write_bytes(b"raw")
    status = offsite_config.read_status()
    assert status["backup_count"] == 2
    assert status["last_local_backup"]["name"] == "kb-2026-07-20.sql.zst"
    assert status["last_local_backup"]["size"] == len(b"dump-b-newer")
    assert status["last_raw_archive"]["name"] == "kb-raw-2026-07-20.tar.zst"


def test_read_status_missing_backups_dir_is_none(env):
    status = offsite_config.read_status()  # backups dir does not exist
    assert status["last_local_backup"] is None
    assert status["last_raw_archive"] is None
    assert status["backup_count"] == 0


def test_read_status_defaults_on_fresh_install(env):
    status = offsite_config.read_status()
    assert status["enabled"] is False
    assert status["rclone_remote"] == ""
    assert status["age_recipient"] == ""
    assert status["rclone_cred_present"] is False
    assert isinstance(status["keep"], int)

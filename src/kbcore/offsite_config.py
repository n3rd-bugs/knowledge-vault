"""kbcore.offsite_config — the offsite-backup config layer (owner web surface).

This is the kbcore chokepoint (Invariant 2) behind the owner-only web /backups
page (kbweb.views_backup): every host-config write goes through a service
function here, each validating BEFORE it touches the filesystem or shells out.

SECURITY POSTURE — the whole point of this module:
  * Secrets are WRITE-ONLY. The rclone credential is stored and never read back,
    never returned, never logged. read_status() reports only its PRESENCE.
  * The age PRIVATE identity is NEVER written here — write_config persists only
    the four non-secret config keys, and the private key stays off this box.
    generate_age_keypair() returns the identity to the caller to show ONCE; this
    module stores nothing.
  * write_config is NOT a general env writer: it touches ONLY the fixed
    allowlist {KB_OFFSITE_ENABLED, KB_RCLONE_REMOTE, KB_AGE_RECIPIENT,
    KB_OFFSITE_KEEP}, each hard-validated so no shell metacharacter can ride into
    a file the host scripts `source`.
  * Every write is atomic (temp + os.replace) and mode 0600. All paths come from
    kbcore.config.settings (no hardcoded paths). The age binary is invoked with a
    list argv, never shell=True.
  * store_drive_token (owner decision 2026-07-21, EMAIL-CENTRIC web surface)
    writes the owner's Google Drive OAuth token WRITE-ONLY into rclone.conf and
    AUTO-SETS the remote so the owner never sees rclone/retention internals. The
    refresh_token in that token is the crown-jewel backup secret: never returned,
    never logged, never surfaced by read_status (which reports only the connected
    email — non-secret — and a connected bool).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from kbcore.config import settings
from kbcore.errors import InvalidOperation

# --- validation regexes (validate BEFORE any file/shell use) ------------------
# age X25519 PUBLIC recipient: the "age1" HRP + 58 bech32 chars ([0-9a-z]).
_AGE_RECIPIENT_RE = re.compile(r"^age1[0-9a-z]{58}$")
# age PRIVATE identity, matched only to PARSE keygen output (never written).
_AGE_IDENTITY_RE = re.compile(r"AGE-SECRET-KEY-[0-9A-Z]+")
# rclone "remote:path": a remote name, ':' , then an optional path. The tight
# charset admits no space, quote, $, ;, |, &, *, ? or backtick — nothing that
# could inject when the host scripts `source` the resulting file.
_RCLONE_REMOTE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}:[A-Za-z0-9._/-]*$")
# Plain-address shape for the connected Google account email (non-secret, but
# rendered into the UI and written to a companion file, so validate it).
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+$")

# The ONLY keys write_config may ever emit (it is not a general env-write tool).
_KEY_ENABLED = "KB_OFFSITE_ENABLED"
_KEY_REMOTE = "KB_RCLONE_REMOTE"
_KEY_RECIPIENT = "KB_AGE_RECIPIENT"
_KEY_KEEP = "KB_OFFSITE_KEEP"
_ALLOWED_KEYS = (_KEY_ENABLED, _KEY_REMOTE, _KEY_RECIPIENT, _KEY_KEEP)

# Fixed filename for the rclone credential inside kb_rclone_conf_dir.
_RCLONE_CRED_NAME = "rclone.conf"
# Non-secret companion holding the connected Google account email (the UI shows
# it; drive_connected() keys off its presence — it NEVER reads the credential).
_DRIVE_EMAIL_NAME = "drive_account"

# The one-click Drive connect (owner decision 2026-07-21) pins a fixed remote +
# folder so the owner never sees KB_RCLONE_REMOTE. The rclone remote block written
# by store_drive_token is named _DRIVE_REMOTE_NAME; backups target _DRIVE_REMOTE.
_DRIVE_REMOTE_NAME = "gdrive"
_DRIVE_FOLDER = "kb-backups"
_DRIVE_REMOTE = f"{_DRIVE_REMOTE_NAME}:{_DRIVE_FOLDER}"
# The rclone token JSON carries ONLY these standard OAuth fields (email is stored
# separately in the companion — it must never leak into the credential file).
_RCLONE_TOKEN_KEYS = ("access_token", "token_type", "refresh_token", "expiry")

_KEEP_MIN, _KEEP_MAX = 1, 365


def _conf_path() -> Path:
    return Path(settings.kb_offsite_conf)


def _cred_path() -> Path:
    return Path(settings.kb_rclone_conf_dir) / _RCLONE_CRED_NAME


def _drive_email_path() -> Path:
    return Path(settings.kb_rclone_conf_dir) / _DRIVE_EMAIL_NAME


def _atomic_write(path: Path, content: str, *, mode: int = 0o600, dir_mode: int = 0o700) -> None:
    """Write ``content`` to ``path`` atomically and 0600 (never a partial file).

    Temp file in the SAME directory + os.replace, so a crash mid-write leaves the
    previous file intact (the atomicity the tests assert). dir_mode is applied
    only to a directory we CREATE, so an existing shared parent is never widened
    or narrowed."""
    parent = path.parent
    created = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    if created:
        with contextlib.suppress(OSError):
            os.chmod(parent, dir_mode)
    fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=".tmp-offsite-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _parse_conf() -> dict[str, str]:
    """Parse the conf file into {allowlisted key: value}. Missing/unreadable → {}.

    Only the four allowlisted keys are ever returned, so a hand-edited stray line
    can never surface through read_status()."""
    out: dict[str, str] = {}
    try:
        text = _conf_path().read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in _ALLOWED_KEYS:
            out[key] = value.strip().strip('"')
    return out


def _newest_backup(pattern: str) -> dict | None:
    """{name, mtime(UTC), size} of the newest match in settings.backups, or None.

    Dates sort lexically (mirrors backup.sh's prune), so newest-by-name is
    newest-by-date. Any OSError (dir absent/unreadable) collapses to None — the
    web page simply shows 'none' rather than erroring."""
    try:
        files = sorted(Path(settings.backups).glob(pattern), key=lambda p: p.name, reverse=True)
    except OSError:
        return None
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        return {
            "name": f.name,
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=UTC),
            "size": st.st_size,
        }
    return None


def _backup_count(pattern: str) -> int:
    try:
        return sum(1 for _ in Path(settings.backups).glob(pattern))
    except OSError:
        return 0


def rclone_cred_present() -> bool:
    """True iff a non-empty rclone credential is stored. NEVER reads the value —
    the credential is write-only; presence is the only thing the UI may learn."""
    p = _cred_path()
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def drive_connected() -> bool:
    """True iff a Google Drive is connected as the backup target.

    Keys off the non-secret email companion (written LAST by store_drive_token, so
    its presence implies the token + remote are already in place) — it NEVER reads
    the write-only rclone credential."""
    p = _drive_email_path()
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def drive_email() -> str:
    """The connected Google account email (non-secret; shown in the UI), or ''.

    Reads ONLY the companion file — never the rclone credential / refresh token."""
    try:
        return _drive_email_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def read_status() -> dict:
    """NON-SECRET offsite status for the web page. Returns NO secret, ever.

    enabled / rclone_remote (name) / age_recipient (the PUBLIC key — safe to
    show) / keep are read from the conf file; drive_connected + drive_email report
    the one-click Drive connect (the refresh token is NEVER read back); the
    last_* / backup_count fields describe the local dump directory. The rclone
    credential and the age private key are absent by construction."""
    conf = _parse_conf()
    enabled = conf.get(_KEY_ENABLED, "false").strip().lower() in ("true", "1", "yes")
    try:
        keep = int(conf.get(_KEY_KEEP, str(settings.kb_offsite_keep)))
    except ValueError:
        keep = settings.kb_offsite_keep
    return {
        "enabled": enabled,
        "rclone_remote": conf.get(_KEY_REMOTE, ""),
        "age_recipient": conf.get(_KEY_RECIPIENT, ""),  # PUBLIC key — may show
        "keep": keep,
        "rclone_cred_present": rclone_cred_present(),
        "drive_connected": drive_connected(),
        "drive_email": drive_email(),  # non-secret; NEVER the token/refresh_token
        "last_local_backup": _newest_backup("kb-*.sql.zst"),
        "last_raw_archive": _newest_backup("kb-raw-*.tar.zst"),
        "backup_count": _backup_count("kb-*.sql.zst"),
    }


def write_config(*, enabled: bool, rclone_remote: str, age_recipient: str, keep: int) -> None:
    """Validate + persist the FOUR non-secret offsite keys atomically (mode 0600).

    Every value is hard-validated first (below), so nothing that could inject into
    a sourced file is ever written; a validation failure raises InvalidOperation
    naming the FIELD (never echoing a secret — none of these four is one) and
    leaves any existing conf file untouched (validation precedes the single
    atomic replace). KB_AGE_IDENTITY (the private key) is DELIBERATELY not a
    writable key — it stays off this box."""
    rclone_remote = (rclone_remote or "").strip()
    age_recipient = (age_recipient or "").strip()
    if age_recipient and not _AGE_RECIPIENT_RE.fullmatch(age_recipient):
        raise InvalidOperation(
            "age_recipient must be an age X25519 public recipient ('age1' + 58 chars), or empty"
        )
    # Empty remote = 'not configured yet' (permitted, so a keypair generated
    # before the remote is set still persists its recipient); a NON-empty remote
    # must match remote:path exactly.
    if rclone_remote and not _RCLONE_REMOTE_RE.fullmatch(rclone_remote):
        raise InvalidOperation(
            "rclone_remote must be 'remote:path' (letters, digits, '_' or '-' in the "
            "remote name, then ':' and an optional path — no spaces or shell metacharacters)"
        )
    if isinstance(keep, bool) or not isinstance(keep, int) or not (_KEEP_MIN <= keep <= _KEEP_MAX):
        raise InvalidOperation(f"keep must be an integer between {_KEEP_MIN} and {_KEEP_MAX}")
    body = (
        "# Knowledge Vault offsite backup config — written by the owner-only web\n"
        "# /backups surface (kbcore.offsite_config). Sourced by scripts/backup.sh +\n"
        "# scripts/offsite.sh. NEVER contains the age private key or an rclone secret.\n"
        f"{_KEY_ENABLED}={'true' if enabled else 'false'}\n"
        f"{_KEY_REMOTE}={rclone_remote}\n"
        f"{_KEY_RECIPIENT}={age_recipient}\n"
        f"{_KEY_KEEP}={keep}\n"
    )
    _atomic_write(_conf_path(), body)


def generate_age_keypair() -> tuple[str, str]:
    """Run age-keygen and return (public recipient, PRIVATE identity).

    Shells out via a LIST argv (never shell=True) to the settings.kb_age_bin seam.
    This function STORES NOTHING and LOGS NOTHING: the caller persists only the
    public recipient (via write_config) and shows the private identity exactly
    once. A missing binary or unparseable output raises a clean InvalidOperation."""
    try:
        proc = subprocess.run(
            [settings.kb_age_bin],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError) as e:
        raise InvalidOperation(
            f"age key generator not found or not runnable ({settings.kb_age_bin!r}); "
            "install age (provides age-keygen)"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise InvalidOperation("age key generation timed out") from e
    if proc.returncode != 0:
        raise InvalidOperation("age key generation failed")
    rec = re.search(r"(age1[0-9a-z]{58})", proc.stdout)
    ident = _AGE_IDENTITY_RE.search(proc.stdout)
    if not rec or not ident:
        raise InvalidOperation("could not parse the generated age keypair")
    recipient = rec.group(1)
    if not _AGE_RECIPIENT_RE.fullmatch(recipient):  # defense: never emit a bad recipient
        raise InvalidOperation("generated age recipient failed validation")
    return recipient, ident.group(0)


def store_rclone_cred(cred: str) -> None:
    """Store a pasted rclone credential WRITE-ONLY (mode 0600, atomic).

    The value is a service-account JSON object or an rclone.conf remote block; it
    is validated to be one of those shapes, then written and never read back,
    returned, or logged (the error path names neither the value nor a secret).
    This is the ONLY write path for the credential; read_status reports presence
    only."""
    cred = (cred or "").strip()
    if not cred:
        raise InvalidOperation("rclone credential is empty")
    if not _looks_like_rclone_cred(cred):
        raise InvalidOperation(
            "rclone credential must be a service-account JSON object or an "
            "rclone.conf remote block (starting with '[')"
        )
    _atomic_write(_cred_path(), cred + "\n")


def _looks_like_rclone_cred(cred: str) -> bool:
    """A service-account JSON (parses as JSON) or an rclone.conf block ('[...]')."""
    if cred.startswith("["):
        return True
    try:
        json.loads(cred)
    except ValueError:
        return False
    return True


def store_drive_token(token: dict, email: str) -> None:
    """Store the owner's Google Drive OAuth token as the WRITE-ONLY rclone
    credential and AUTO-WIRE the offsite remote to it (owner decision 2026-07-21,
    the one-click email-centric web surface).

    SECURITY: the refresh_token inside ``token`` is the crown-jewel backup secret.
    It is written into rclone.conf (atomic, mode 0600) and is NEVER returned,
    logged, or surfaced by read_status — which reports only the connected email
    (non-secret) and a connected bool. The token dict is VALIDATED to carry a
    refresh_token before ANY file is touched (a re-consent that returned none is
    rejected). Least-privilege: the remote is pinned to the drive.file scope.

    The rclone remote (``gdrive:kb-backups``) + a sensible retention are set here
    (via write_config) so the owner never sees KB_RCLONE_REMOTE/KB_OFFSITE_KEEP;
    the owner's enabled flag + any age recipient are preserved untouched. The
    non-secret email is written to a companion file LAST, so drive_connected()
    (which keys off it) can never report connected before the token is in place."""
    if not isinstance(token, dict):
        raise InvalidOperation("drive token must be an object")
    if not str(token.get("refresh_token") or "").strip():
        raise InvalidOperation("drive token is missing a refresh_token (re-consent required)")
    email = (email or "").strip().lower()
    if not _EMAIL_RE.fullmatch(email):
        raise InvalidOperation("connected account email is not a valid address")
    # rclone.conf: the drive.file remote + the offline token — write-only, 0600.
    # The token JSON carries ONLY standard OAuth fields (email is NOT one of them).
    token_json = json.dumps(
        {k: token[k] for k in _RCLONE_TOKEN_KEYS if token.get(k)}, separators=(",", ":")
    )
    conf = (
        f"[{_DRIVE_REMOTE_NAME}]\n"
        "type = drive\n"
        "scope = drive.file\n"
        f"client_id = {settings.google_client_id}\n"
        f"client_secret = {settings.google_client_secret}\n"
        f"token = {token_json}\n"
    )
    _atomic_write(_cred_path(), conf)
    # Auto-set the remote + retention, preserving enabled + any age recipient.
    current = _parse_conf()
    enabled = current.get(_KEY_ENABLED, "false").strip().lower() in ("true", "1", "yes")
    age_recipient = current.get(_KEY_RECIPIENT, "")
    try:
        keep = int(current.get(_KEY_KEEP, str(settings.kb_offsite_keep)))
    except ValueError:
        keep = settings.kb_offsite_keep
    write_config(
        enabled=enabled, rclone_remote=_DRIVE_REMOTE, age_recipient=age_recipient, keep=keep
    )
    _atomic_write(_drive_email_path(), email + "\n")


def clear_rclone_cred() -> None:
    """Remove the stored rclone credential, if any (idempotent)."""
    with contextlib.suppress(FileNotFoundError):
        _cred_path().unlink()

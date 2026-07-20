#!/usr/bin/env bash
# offsite.sh — encrypted offsite backup transport (PLAN.md §6d).
#
# Subcommands:
#   push <dump.sql.zst>   age-encrypt the dump LOCALLY (recipient =
#                         KB_AGE_RECIPIENT public key), rclone-copy ONLY the
#                         resulting *.age ciphertext to KB_RCLONE_REMOTE, then
#                         prune the remote to the KB_OFFSITE_KEEP newest.
#                         The plaintext dump never leaves the box.
#   fetch <dest-dir>      pull the newest kb-*.sql.zst.age from the remote,
#                         decrypt with the KB_AGE_IDENTITY key file, print the
#                         decrypted dump's path on stdout (all logs on stderr).
#
# Config comes from .env like backup.sh: KB_OFFSITE_ENABLED, KB_RCLONE_REMOTE,
# KB_AGE_RECIPIENT, KB_OFFSITE_KEEP, KB_AGE_IDENTITY (fetch only; keep the
# private key OFF this box in production). Failures exit nonzero and never
# touch the local backups. Never prints secrets.
#
# Test seams (used by tests/test_offsite_backup.py; harmless in production):
#   KB_AGE_BIN / KB_RCLONE_BIN  override the binaries (stubs when not installed)
#   KB_ENV_FILE                 override the .env path (/dev/null in tests)
set -euo pipefail
umask 077

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${KB_ENV_FILE:-${REPO_DIR}/.env}"

# Pull config from .env without echoing it (secrets live here too).
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
  set +a
fi

KB_OFFSITE_ENABLED="${KB_OFFSITE_ENABLED:-false}"
KB_RCLONE_REMOTE="${KB_RCLONE_REMOTE:-}"
KB_AGE_RECIPIENT="${KB_AGE_RECIPIENT:-}"
KB_OFFSITE_KEEP="${KB_OFFSITE_KEEP:-30}"
KB_AGE_IDENTITY="${KB_AGE_IDENTITY:-}"
KB_AGE_IDENTITY="${KB_AGE_IDENTITY/#\~/${HOME}}"   # expand a leading ~
AGE_BIN="${KB_AGE_BIN:-age}"
RCLONE_BIN="${KB_RCLONE_BIN:-rclone}"

# Remote object naming. The DB dump family (matches backup.sh's
# kb-YYYY-MM-DD.sql.zst + .age) is the DEFAULT fetch target so restore-test and
# existing tooling are unchanged. push/prune are ARTIFACT-FAMILY-AWARE: the raw
# archive (kb-raw-YYYY-MM-DD.tar.zst.age) rides the SAME encrypt+push+prune
# path, pruned independently of the dumps so neither family evicts the other.
NAME_RE='^kb-[0-9]{4}-[0-9]{2}-[0-9]{2}\.sql\.zst\.age$'

# Derive a family regex from an artifact basename by replacing its date with the
# date pattern and anchoring +.age — e.g. kb-2026-07-18.sql.zst -> the dump
# family, kb-raw-2026-07-18.tar.zst -> the raw family. Files of different
# families never match each other, so prune_remote counts each family alone.
family_re_for() {
  local base="$1"
  if [[ "${base}" =~ ^(.+)-[0-9]{4}-[0-9]{2}-[0-9]{2}(\..+)$ ]]; then
    local prefix="${BASH_REMATCH[1]}" suffix="${BASH_REMATCH[2]}"
    printf '^%s-[0-9]{4}-[0-9]{2}-[0-9]{2}%s\\.age$' \
      "${prefix}" "${suffix//./\\.}"
  else
    die "cannot derive backup family from name: ${base}"
  fi
}

log() { echo "offsite: $*" >&2; }
die() { log "FAIL — $*"; exit 1; }

STAGE=""
cleanup() { if [[ -n "${STAGE}" ]]; then rm -rf "${STAGE}"; fi; }
trap cleanup EXIT

require_tools() {
  command -v "${AGE_BIN}" >/dev/null 2>&1 \
    || die "age not found (install age, or set KB_AGE_BIN)"
  command -v "${RCLONE_BIN}" >/dev/null 2>&1 \
    || die "rclone not found (install rclone, or set KB_RCLONE_BIN)"
}

require_remote_config() {
  [[ -n "${KB_RCLONE_REMOTE}" ]] || die "KB_RCLONE_REMOTE is empty (set it in .env)"
  [[ "${KB_OFFSITE_KEEP}" =~ ^[0-9]+$ ]] || die "KB_OFFSITE_KEEP must be an integer"
}

# Prune the remote to the KB_OFFSITE_KEEP newest kb-*.sql.zst.age objects
# (dates sort lexically, mirroring the local prune in backup.sh). Only objects
# matching NAME_RE are ever deleted — anything else on the remote is untouched.
prune_remote() {
  local family_re="${1:-${NAME_RE}}"
  local listing f n=0
  listing="$("${RCLONE_BIN}" lsf --files-only "${KB_RCLONE_REMOTE}")" \
    || die "rclone lsf failed during remote prune"
  while IFS= read -r f; do
    [[ -n "${f}" ]] || continue
    [[ "${f}" =~ ${family_re} ]] || continue
    n=$((n + 1))
    if (( n > KB_OFFSITE_KEEP )); then
      "${RCLONE_BIN}" deletefile "${KB_RCLONE_REMOTE}/${f}" \
        || die "rclone deletefile ${f} failed during remote prune"
      log "pruned remote ${f}"
    fi
  done < <(printf '%s\n' "${listing}" | sort -r)
  log "remote retention: $(( n < KB_OFFSITE_KEEP ? n : KB_OFFSITE_KEEP )) object(s) kept (KB_OFFSITE_KEEP=${KB_OFFSITE_KEEP})"
}

cmd_push() {
  local dump="$1"
  # Defense in depth: backup.sh gates on KB_OFFSITE_ENABLED before calling us,
  # but an explicit invocation with the switch off is a loud, safe no-op.
  if [[ ! "${KB_OFFSITE_ENABLED,,}" =~ ^(true|1|yes)$ ]]; then
    log "KB_OFFSITE_ENABLED is not true — skipping offsite push"
    return 0
  fi
  [[ -s "${dump}" ]] || die "dump missing or empty: ${dump}"
  [[ -n "${KB_AGE_RECIPIENT}" ]] || die "KB_AGE_RECIPIENT is empty (age public key, set it in .env)"
  require_remote_config
  require_tools

  STAGE="$(mktemp -d)"
  local name enc
  name="$(basename "${dump}").age"
  enc="${STAGE}/${name}"

  # ENCRYPT FIRST, LOCALLY: only the .age ciphertext is ever handed to rclone.
  "${AGE_BIN}" -r "${KB_AGE_RECIPIENT}" -o "${enc}" "${dump}" \
    || die "age encryption failed (local backup is intact)"
  chmod 600 "${enc}"
  [[ -s "${enc}" ]] || die "encrypted artifact is empty (local backup is intact)"

  "${RCLONE_BIN}" copy "${enc}" "${KB_RCLONE_REMOTE}" \
    || die "rclone copy failed (local backup is intact)"
  log "pushed ${name} -> ${KB_RCLONE_REMOTE}"

  # Prune only THIS artifact's family (dumps and raw archives retain separately).
  prune_remote "$(family_re_for "$(basename "${dump}")")"
}

cmd_fetch() {
  local dest="$1"
  # Optional family regex — defaults to the DB dump (restore-test path unchanged).
  # Pass 'raw' to fetch the newest raw archive instead.
  local family_re="${NAME_RE}"
  case "${2:-dump}" in
    dump) family_re="${NAME_RE}" ;;
    raw) family_re='^kb-raw-[0-9]{4}-[0-9]{2}-[0-9]{2}\.tar\.zst\.age$' ;;
    *) family_re="$2" ;;  # an explicit regex, for forward-compat
  esac
  [[ -d "${dest}" ]] || die "destination directory missing: ${dest}"
  [[ -n "${KB_AGE_IDENTITY}" ]] || die "KB_AGE_IDENTITY is empty (path to the age identity file)"
  [[ -f "${KB_AGE_IDENTITY}" ]] || die "age identity file not found: ${KB_AGE_IDENTITY}"
  require_remote_config
  require_tools

  local listing newest
  listing="$("${RCLONE_BIN}" lsf --files-only "${KB_RCLONE_REMOTE}")" \
    || die "rclone lsf failed for ${KB_RCLONE_REMOTE}"
  newest="$(printf '%s\n' "${listing}" | grep -E "${family_re}" | sort -r | head -1 || true)"
  [[ -n "${newest}" ]] || die "no objects matching ${family_re} on ${KB_RCLONE_REMOTE}"

  "${RCLONE_BIN}" copy "${KB_RCLONE_REMOTE}/${newest}" "${dest}" \
    || die "rclone copy (fetch) failed"
  [[ -s "${dest}/${newest}" ]] || die "fetched object missing or empty"

  local out="${dest}/${newest%.age}"
  "${AGE_BIN}" -d -i "${KB_AGE_IDENTITY}" -o "${out}" "${dest}/${newest}" \
    || die "age decryption failed (wrong identity?)"
  [[ -s "${out}" ]] || die "decrypted dump is empty"
  chmod 600 "${out}"
  log "fetched + decrypted ${newest}"
  printf '%s\n' "${out}"
}

case "${1:-}" in
  push)
    [[ $# -eq 2 ]] || die "usage: offsite.sh push <dump.sql.zst>"
    cmd_push "$2"
    ;;
  fetch)
    [[ $# -ge 2 && $# -le 3 ]] || die "usage: offsite.sh fetch <dest-dir> [dump|raw]"
    cmd_fetch "$2" "${3:-dump}"
    ;;
  *)
    die "usage: offsite.sh {push <artifact> | fetch <dest-dir> [dump|raw]}"
    ;;
esac

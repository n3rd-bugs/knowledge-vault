#!/usr/bin/env bash
# backup.sh — nightly pg_dump → zstd, chmod 600, pruned (PLAN.md §9, SEC-4).
#
# Keeps 14 daily + 8 weekly (Monday) dumps. Reads KB_BACKUPS from .env (default
# ~/kb/backups). Never prints secrets. Same-day re-runs overwrite (idempotent-ish).
#
# Offsite (PLAN.md §6d, opt-in): when KB_OFFSITE_ENABLED=true the finished dump
# is age-ENCRYPTED locally and only the ciphertext is pushed via rclone — see
# scripts/offsite.sh. Unset/false keeps this script purely local (no new deps).
set -euo pipefail
umask 077

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_DIR}/.env"

# Pull config from .env without echoing it (secrets live here too).
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
  set +a
fi

KB_BACKUPS="${KB_BACKUPS:-${HOME}/kb/backups}"
KB_BACKUPS="${KB_BACKUPS/#\~/${HOME}}"   # expand a leading ~
mkdir -p "${KB_BACKUPS}"

STAMP="$(date -u +%F)"
OUT="${KB_BACKUPS}/kb-${STAMP}.sql.zst"

# pg_dump runs inside the db container over its local socket (PLAN §9 command).
docker exec kb-db pg_dump -U kb kb | zstd -9 -q -o "${OUT}.tmp" -f
mv -f "${OUT}.tmp" "${OUT}"
chmod 600 "${OUT}"

if [[ ! -s "${OUT}" ]]; then
  echo "backup.sh: FAILED — ${OUT} is empty" >&2
  exit 1
fi

# --- prune: keep the 14 newest daily + the 8 newest Monday dumps -------------
cd "${KB_BACKUPS}"
declare -A keep=()
daily=0
weekly=0
# ls newest-first by filename (dates sort lexically)
while IFS= read -r f; do
  [[ -n "${f}" ]] || continue
  d="${f#kb-}"; d="${d%.sql.zst}"
  [[ "${d}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || continue
  daily=$((daily + 1))
  if (( daily <= 14 )); then keep["${f}"]=1; fi
  if [[ "$(date -u -d "${d}" +%u 2>/dev/null || echo 0)" == "1" ]]; then
    weekly=$((weekly + 1))
    if (( weekly <= 8 )); then keep["${f}"]=1; fi
  fi
done < <(ls -1 kb-*.sql.zst 2>/dev/null | sort -r)

for f in kb-*.sql.zst; do
  [[ -e "${f}" ]] || continue
  [[ -n "${keep[${f}]:-}" ]] || rm -f -- "${f}"
done

echo "backup.sh: wrote ${OUT} ($(du -h "${OUT}" | cut -f1)); $(ls -1 kb-*.sql.zst | wc -l) dumps retained"

# --- offsite push (PLAN.md §6d) ----------------------------------------------
# Additive: with KB_OFFSITE_ENABLED unset/false nothing below runs and this
# script behaves exactly as before. The local backup above is already complete;
# an offsite failure logs + exits nonzero but never touches the local dumps.
# offsite.sh encrypts LOCALLY with age (recipient KB_AGE_RECIPIENT) and pushes
# ONLY the .age ciphertext to KB_RCLONE_REMOTE — plaintext never leaves the box.
KB_OFFSITE_ENABLED="${KB_OFFSITE_ENABLED:-false}"
if [[ "${KB_OFFSITE_ENABLED,,}" =~ ^(true|1|yes)$ ]]; then
  if bash "${REPO_DIR}/scripts/offsite.sh" push "${OUT}"; then
    echo "backup.sh: offsite push OK"
  else
    echo "backup.sh: local backup OK but OFFSITE PUSH FAILED (see offsite: lines above)" >&2
    exit 1
  fi
fi

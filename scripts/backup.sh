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

# Overlay the WEB-WRITTEN offsite config by strict ALLOWLIST PARSE — never
# `source` it (2026-07-21 security review). kb-web bind-mounts this file and can
# write it, so a malformed/injected line must be pure inert DATA here: we read
# line-by-line, split on the FIRST '=', accept ONLY the four known keys, and
# RE-VALIDATE each value (identical to kbcore.offsite_config.write_config), so no
# newline/metachar/extra-var can ever execute or set anything unexpected.
KB_OFFSITE_CONF="${KB_OFFSITE_CONF:-${HOME}/kb/webconf/offsite.env}"
KB_OFFSITE_CONF="${KB_OFFSITE_CONF/#\~/${HOME}}"
if [[ -f "${KB_OFFSITE_CONF}" ]]; then
  while IFS='=' read -r _k _v; do
    case "${_k}" in
      KB_OFFSITE_ENABLED) [[ "${_v}" =~ ^(true|false|1|0|yes|no)$ ]] && KB_OFFSITE_ENABLED="${_v}" ;;
      KB_RCLONE_REMOTE)   [[ "${_v}" =~ ^[A-Za-z0-9_-]{1,64}:[A-Za-z0-9._/-]*$ ]] && KB_RCLONE_REMOTE="${_v}" ;;
      KB_AGE_RECIPIENT)   [[ "${_v}" =~ ^age1[0-9a-z]{58}$ ]] && KB_AGE_RECIPIENT="${_v}" ;;
      KB_OFFSITE_KEEP)    [[ "${_v}" =~ ^[0-9]{1,3}$ ]] && KB_OFFSITE_KEEP="${_v}" ;;
    esac
  done < "${KB_OFFSITE_CONF}"
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

# Archive INTEGRITY gate (review #5 ops-6): non-emptiness alone lets a
# truncated-but-nonempty dump sit undetected until the monthly restore drill,
# while daily retention is only 14 days. `zstd -t` decodes the whole frame and
# verifies its checksum in seconds; a failure exits nonzero, which fails the
# systemd unit and trips the already-wired OnFailure notifier. Catches
# write-time truncation/corruption only — the monthly drill stays the backstop.
if ! zstd -t -q "${OUT}"; then
  echo "backup.sh: FAILED — ${OUT} did not pass the zstd integrity check" >&2
  exit 1
fi

# --- count sidecar (review #6 Wave G): the restore drill's ground truth -------
# The 04:30 drill races the 01:00 drain (~190-280 items/h), so comparing the
# RESTORED counts against the LIVE DB blew the 1% budget on 5 of 8 recent
# nights — a false "backup broken" page waiting to happen. The truth about a
# dump is the dump: one decode pass counts the COPY rows the artifact actually
# carries (items/chunks/relations + chunks with a NULL embedding, which proves
# the vector column round-trips), and restore_test.sh exact-matches against
# THIS file. Counting the artifact cannot race anything.
if ! zstd -dc "${OUT}" | awk '
  /^COPY public\.(items|chunks|relations) \(/ {
    tbl = $2; sub(/^public\./, "", tbl); incopy = 1
    embidx = 0
    if (tbl == "chunks") {
      # column list between the parens, e.g. COPY public.chunks (id, item_id, embedding, ...)
      cols = $0; sub(/^[^(]*\(/, "", cols); sub(/\).*$/, "", cols)
      n = split(cols, a, /, */)
      for (i = 1; i <= n; i++) if (a[i] == "embedding") embidx = i
    }
    next
  }
  incopy && /^\\\.$/ { incopy = 0; next }
  incopy {
    rows[tbl]++
    if (tbl == "chunks" && embidx > 0) { split($0, f, "\t"); if (f[embidx] == "\\N") nullemb++ }
  }
  END {
    printf "items=%d\nchunks=%d\nrelations=%d\nchunks_null_embedding=%d\n", \
      rows["items"], rows["chunks"], rows["relations"], nullemb
  }' > "${OUT}.counts.tmp"; then
  echo "backup.sh: FAILED — could not derive the count sidecar from ${OUT}" >&2
  exit 1
fi
mv -f "${OUT}.counts.tmp" "${OUT}.counts"
chmod 600 "${OUT}.counts"
echo "backup.sh: sidecar $(tr '\n' ' ' < "${OUT}.counts")"

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
  # the .counts sidecar lives and dies with its dump
  [[ -n "${keep[${f}]:-}" ]] || rm -f -- "${f}" "${f}.counts"
done

echo "backup.sh: wrote ${OUT} ($(du -h "${OUT}" | cut -f1)); $(ls -1 kb-*.sql.zst | wc -l) dumps retained"

# --- raw archive: the retained ORIGINAL bytes under KB_RAW ---------------------
# The pg_dump above captures the DB (including ingest_queue.content) but NOT the
# content-addressed transcript/doc originals under KB_RAW, which are what
# `kb reprocess` re-runs the extractor over. Without this they live on one disk
# with no backup. Archived as a dated tar.zst so it rides the same retention +
# offsite-encryption path as the dump; a distinct kb-raw-* family so the two
# prune independently (14 daily + 8 weekly each).
KB_RAW="${KB_RAW:-${HOME}/kb/raw}"
KB_RAW="${KB_RAW/#\~/${HOME}}"
RAW_OUT="${KB_BACKUPS}/kb-raw-${STAMP}.tar.zst"
if [[ -d "${KB_RAW}" ]]; then
  # -C so paths are relative to KB_RAW (portable restore); zstd via tar's filter.
  tar -C "${KB_RAW}" -cf - . | zstd -9 -q -o "${RAW_OUT}.tmp" -f
  mv -f "${RAW_OUT}.tmp" "${RAW_OUT}"
  chmod 600 "${RAW_OUT}"
  # Same integrity gate as the dump (ops-6): a bad raw archive fails the unit.
  if ! zstd -t -q "${RAW_OUT}"; then
    echo "backup.sh: FAILED — ${RAW_OUT} did not pass the zstd integrity check" >&2
    exit 1
  fi
  # prune the kb-raw-* family with the SAME 14-daily + 8-weekly rule as dumps
  declare -A rkeep=()
  rdaily=0
  rweekly=0
  while IFS= read -r f; do
    [[ -n "${f}" ]] || continue
    d="${f#kb-raw-}"; d="${d%.tar.zst}"
    [[ "${d}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || continue
    rdaily=$((rdaily + 1))
    if (( rdaily <= 14 )); then rkeep["${f}"]=1; fi
    if [[ "$(date -u -d "${d}" +%u 2>/dev/null || echo 0)" == "1" ]]; then
      rweekly=$((rweekly + 1))
      if (( rweekly <= 8 )); then rkeep["${f}"]=1; fi
    fi
  done < <(ls -1 kb-raw-*.tar.zst 2>/dev/null | sort -r)
  for f in kb-raw-*.tar.zst; do
    [[ -e "${f}" ]] || continue
    [[ -n "${rkeep[${f}]:-}" ]] || rm -f -- "${f}"
  done
  echo "backup.sh: wrote ${RAW_OUT} ($(du -h "${RAW_OUT}" | cut -f1)); $(ls -1 kb-raw-*.tar.zst | wc -l) raw archives retained"
else
  echo "backup.sh: KB_RAW ${KB_RAW} absent — skipping raw archive"
  RAW_OUT=""
fi

# --- offsite push (PLAN.md §6d) ----------------------------------------------
# Additive: with KB_OFFSITE_ENABLED unset/false nothing below runs and this
# script behaves exactly as before. The local backup above is already complete;
# an offsite failure logs + exits nonzero but never touches the local dumps.
# offsite.sh encrypts LOCALLY with age (recipient KB_AGE_RECIPIENT) and pushes
# ONLY the .age ciphertext to KB_RCLONE_REMOTE — plaintext never leaves the box.
KB_OFFSITE_ENABLED="${KB_OFFSITE_ENABLED:-false}"
if [[ "${KB_OFFSITE_ENABLED,,}" =~ ^(true|1|yes)$ ]]; then
  offsite_fail=0
  if bash "${REPO_DIR}/scripts/offsite.sh" push "${OUT}"; then
    echo "backup.sh: offsite push OK (dump)"
  else
    echo "backup.sh: local backup OK but OFFSITE PUSH FAILED for the dump" >&2
    offsite_fail=1
  fi
  if [[ -n "${RAW_OUT}" ]]; then
    if bash "${REPO_DIR}/scripts/offsite.sh" push "${RAW_OUT}"; then
      echo "backup.sh: offsite push OK (raw)"
    else
      echo "backup.sh: local backup OK but OFFSITE PUSH FAILED for the raw archive" >&2
      offsite_fail=1
    fi
  fi
  (( offsite_fail == 0 )) || exit 1
fi

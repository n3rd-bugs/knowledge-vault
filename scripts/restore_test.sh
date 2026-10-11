#!/usr/bin/env bash
# restore_test.sh — prove backups actually restore (PLAN.md §9, OPS-2).
#
# Restores the newest ~/kb/backups/*.sql.zst into a THROWAWAY pgvector container
# on 127.0.0.1:5599, asserts items+chunks counts are within 1% of the LIVE DB,
# then destroys the container+volume. Prints PASS/FAIL; exits nonzero on FAIL.
# Never echoes secrets.
#
# --offsite (PLAN.md §6d): instead of the local dump, fetch the newest encrypted
# kb-*.sql.zst.age from KB_RCLONE_REMOTE and age-decrypt it with KB_AGE_IDENTITY
# (private-key file — kept off-box in production, supplied only for the drill),
# then run the same throwaway restore + 1% row-count check. Default (no flag)
# stays the pure-local drill.
#
# --auto (timer mode, review #6 Wave G): EVEN months drill the offsite path so
# age-decrypt + rclone stay machine-proven — but ONLY when the off-box age
# identity has been supplied; otherwise fall back to local rather than page a
# false FAIL from an unattended timer.
#   Review #8 H12: the month is the LOCAL calendar month. It used to be
#   `date -u +%m`; the timer fires 04:30 PKT on the 1st, which is 23:30 UTC on
#   the LAST day of the previous month, so Sep 1 read "August", demanded
#   --offsite, and printed the fallback line for the wrong month. The KB stores
#   UTC (Invariant 7), but "the 1st of an even month" is a calendar statement
#   in the same LOCAL clock the timer's OnCalendar uses.
#
# --dry-run (any mode): print the mode + visibility line and exit 0 before
# touching docker or the remote — the test seam, and a cheap rehearsal before
# the 10-01 drill (which MUST run with the identity supplied: KB 28833).
#
# Every drill prints ONE visibility line first, so the journal (and the digest
# that reads it) can tell a local drill from an offsite one at a glance — an
# offsite drill that silently fell back to local proves nothing about the
# offsite path:
#   restore_test: drill mode=<local|offsite> identity=<present|absent> month=<MM> (...)
#
# Test seams: KB_ENV_FILE overrides the env file sourced (default: the repo
# .env, as in backup.sh/offsite.sh); KB_DRILL_MONTH overrides the month --auto
# reads (default: local `date +%m`).
set -uo pipefail
umask 077

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${KB_ENV_FILE:-${REPO_DIR}/.env}"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
  set +a
fi

KB_BACKUPS="${KB_BACKUPS:-${HOME}/kb/backups}"
KB_BACKUPS="${KB_BACKUPS/#\~/${HOME}}"
CONT="kb-restore-test"
IMAGE="pgvector/pgvector:pg16"
DBPASS="${KB_DB_PASSWORD:-restore-test}"

# LOCAL month (see the --auto header note); a malformed override falls back.
DRILL_MONTH="${KB_DRILL_MONTH:-$(date +%m)}"
[[ "${DRILL_MONTH}" =~ ^[0-9]{1,2}$ ]] || DRILL_MONTH="$(date +%m)"
IDENTITY="${KB_AGE_IDENTITY:-}"
IDENTITY="${IDENTITY/#\~/${HOME}}"
IDENTITY_STATE="absent"
if [[ -n "${IDENTITY}" && -r "${IDENTITY}" ]]; then IDENTITY_STATE="present"; fi

MODE="local"
DRY_RUN=0
for arg in "$@"; do
  case "${arg}" in
    --offsite) MODE="offsite" ;;
    --dry-run) DRY_RUN=1 ;;
    --auto)
      if (( 10#${DRILL_MONTH} % 2 == 0 )) && [[ "${IDENTITY_STATE}" == "present" ]]; then
        MODE="offsite"
      else
        MODE="local"
        if (( 10#${DRILL_MONTH} % 2 == 0 )); then
          echo "restore_test: even month wanted --offsite but KB_AGE_IDENTITY is absent — local drill (supply the identity to prove the offsite path)"
        fi
      fi
      ;;
    *) echo "restore_test: unknown argument '${arg}' (usage: restore_test.sh [--offsite|--auto] [--dry-run])" >&2; exit 2 ;;
  esac
done

echo "restore_test: drill mode=${MODE} identity=${IDENTITY_STATE} month=${DRILL_MONTH} (local calendar; even months drill offsite when the identity is supplied)"
if (( DRY_RUN == 1 )); then
  echo "restore_test: dry-run — decided only; no container, no fetch"
  exit 0
fi

TMP_FETCH=""
fail() { echo "restore_test: FAIL — $*" >&2; cleanup; exit 1; }
# Two scopes on purpose. The mid-script call below must clear only a LEFTOVER
# CONTAINER from a previous run — the first offsite drill (2026-08-03) failed
# because the single cleanup() also removed TMP_FETCH, deleting the freshly
# fetched+decrypted dump moments before zstd read it. Local mode never noticed:
# local dumps live in backups/, outside the temp dir. The temp dir is the EXIT
# trap's job alone.
cleanup_container() {
  docker rm -fv "${CONT}" >/dev/null 2>&1 || true
}
cleanup() {
  cleanup_container
  if [[ -n "${TMP_FETCH}" ]]; then rm -rf "${TMP_FETCH}"; fi
}
trap cleanup EXIT

if [[ "${MODE}" == "offsite" ]]; then
  # offsite.sh fetch: rclone-pull the newest .age object + age-decrypt it; the
  # decrypted dump path arrives on stdout (its logs go to stderr).
  TMP_FETCH="$(mktemp -d)"
  NEWEST="$(bash "${REPO_DIR}/scripts/offsite.sh" fetch "${TMP_FETCH}")" \
    || fail "offsite fetch/decrypt failed"
  [[ -s "${NEWEST}" ]] || fail "offsite fetch produced no dump"
  echo "restore_test: using OFFSITE $(basename "${NEWEST}")"
else
  NEWEST="$(ls -1t "${KB_BACKUPS}"/kb-*.sql.zst 2>/dev/null | head -1)"
  [[ -n "${NEWEST}" ]] || fail "no backup found under ${KB_BACKUPS}"
  echo "restore_test: using $(basename "${NEWEST}")"
fi

cleanup_container
docker run -d --name "${CONT}" \
  -e POSTGRES_DB=kb -e POSTGRES_USER=kb -e POSTGRES_PASSWORD="${DBPASS}" \
  -p 127.0.0.1:5599:5432 "${IMAGE}" >/dev/null

# Wait for the throwaway server to accept connections.
for _ in $(seq 1 40); do
  if docker exec "${CONT}" pg_isready -U kb -d kb >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "${CONT}" pg_isready -U kb -d kb >/dev/null 2>&1 || fail "restore container never became ready"

docker exec "${CONT}" psql -U kb -d kb -q -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null \
  || fail "could not create vector extension"

if ! zstd -dc "${NEWEST}" | docker exec -i "${CONT}" psql -U kb -d kb -v ON_ERROR_STOP=1 -q >/dev/null; then
  fail "loading the dump failed"
fi

count() { docker exec "$1" psql -U kb -d kb -tAc "SELECT count(*) FROM $2" 2>/dev/null | tr -d '[:space:]'; }
R_ITEMS="$(count "${CONT}" items)";  R_CHUNKS="$(count "${CONT}" chunks)"
R_RELS="$(count "${CONT}" relations)"
R_NULLEMB="$(docker exec "${CONT}" psql -U kb -d kb -tAc \
  "SELECT count(*) FROM chunks WHERE embedding IS NULL" 2>/dev/null | tr -d '[:space:]')"
[[ "${R_ITEMS}" =~ ^[0-9]+$ && "${R_CHUNKS}" =~ ^[0-9]+$ ]] || fail "could not read restored row counts"

# --- sidecar exact-match (review #6 Wave G): the drill's ground truth ---------
# backup.sh derives ${dump}.counts from the ARTIFACT (one decode pass over the
# COPY blocks), so restored-vs-sidecar is exact by construction. The old
# restored-vs-LIVE 1% check raced the 01:30 drain (~190-280 items/h) and would
# have false-FAILED 5 of 8 recent nights at 04:30 — it is now ADVISORY only.
SIDECAR="${KB_BACKUPS}/$(basename "${NEWEST}").counts"
[[ "${MODE}" == "local" ]] && SIDECAR="${NEWEST}.counts"
if [[ -r "${SIDECAR}" ]]; then
  S_ITEMS="$(sed -n 's/^items=//p' "${SIDECAR}")"
  S_CHUNKS="$(sed -n 's/^chunks=//p' "${SIDECAR}")"
  S_RELS="$(sed -n 's/^relations=//p' "${SIDECAR}")"
  S_NULLEMB="$(sed -n 's/^chunks_null_embedding=//p' "${SIDECAR}")"
  [[ "${S_ITEMS}" =~ ^[0-9]+$ && "${S_CHUNKS}" =~ ^[0-9]+$ ]] || fail "sidecar ${SIDECAR} is malformed"
  echo "restore_test: sidecar items=${S_ITEMS} chunks=${S_CHUNKS} relations=${S_RELS} null_emb=${S_NULLEMB}"
  echo "restore_test: restored items=${R_ITEMS} chunks=${R_CHUNKS} relations=${R_RELS} null_emb=${R_NULLEMB}"
  [[ "${R_ITEMS}"  == "${S_ITEMS}"  ]] || fail "items restored=${R_ITEMS} != sidecar=${S_ITEMS}"
  [[ "${R_CHUNKS}" == "${S_CHUNKS}" ]] || fail "chunks restored=${R_CHUNKS} != sidecar=${S_CHUNKS}"
  [[ -z "${S_RELS}" || "${R_RELS}" == "${S_RELS}" ]] || fail "relations restored=${R_RELS} != sidecar=${S_RELS}"
  # NULL-embedding equality proves the vector column round-tripped: a dump or
  # restore that silently dropped embeddings would inflate the restored side.
  [[ -z "${S_NULLEMB}" || "${R_NULLEMB}" == "${S_NULLEMB}" ]] \
    || fail "chunks with NULL embedding restored=${R_NULLEMB} != sidecar=${S_NULLEMB}"
  L_ITEMS="$(count kb-db items)"; L_CHUNKS="$(count kb-db chunks)"
  echo "restore_test: advisory vs live: items ${R_ITEMS}/${L_ITEMS:-?}, chunks ${R_CHUNKS}/${L_CHUNKS:-?} (drift = writes since dump)"
  echo "restore_test: PASS (exact match against the dump's count sidecar; mode=${MODE})"
else
  # Pre-sidecar dump (or offsite object without a local sidecar): the old
  # advisory-grade 1% live comparison is the only available check.
  L_ITEMS="$(count kb-db items)"; L_CHUNKS="$(count kb-db chunks)"
  [[ "${L_ITEMS}" =~ ^[0-9]+$ ]] || fail "could not read live row counts"
  within_1pct() {
    local r="$1" l="$2" d
    if (( l == 0 )); then (( r == 0 )); return; fi
    d=$(( r > l ? r - l : l - r ))
    (( d * 100 <= l ))
  }
  echo "restore_test: no sidecar at ${SIDECAR} — falling back to the 1% live comparison"
  echo "restore_test: items restored=${R_ITEMS} live=${L_ITEMS} | chunks restored=${R_CHUNKS} live=${L_CHUNKS}"
  within_1pct "${R_ITEMS}"  "${L_ITEMS}"  || fail "items count ${R_ITEMS} not within 1% of live ${L_ITEMS}"
  within_1pct "${R_CHUNKS}" "${L_CHUNKS}" || fail "chunks count ${R_CHUNKS} not within 1% of live ${L_CHUNKS}"
  echo "restore_test: PASS (items & chunks within 1% of live; sidecar unavailable; mode=${MODE})"
fi
# cleanup runs via the EXIT trap

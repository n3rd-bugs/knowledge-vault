#!/usr/bin/env bash
# restore_test.sh — prove backups actually restore (PLAN.md §9, OPS-2).
#
# Restores the newest ~/kb/backups/*.sql.zst into a THROWAWAY pgvector container
# on 127.0.0.1:5599, asserts items+chunks counts are within 1% of the LIVE DB,
# then destroys the container+volume. Prints PASS/FAIL; exits nonzero on FAIL.
# Never echoes secrets.
set -uo pipefail
umask 077

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${REPO_DIR}/.env"
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

fail() { echo "restore_test: FAIL — $*" >&2; cleanup; exit 1; }
cleanup() { docker rm -fv "${CONT}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

NEWEST="$(ls -1t "${KB_BACKUPS}"/kb-*.sql.zst 2>/dev/null | head -1)"
[[ -n "${NEWEST}" ]] || fail "no backup found under ${KB_BACKUPS}"
echo "restore_test: using $(basename "${NEWEST}")"

cleanup
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
L_ITEMS="$(count kb-db items)";      L_CHUNKS="$(count kb-db chunks)"
[[ "${R_ITEMS}" =~ ^[0-9]+$ && "${L_ITEMS}" =~ ^[0-9]+$ ]] || fail "could not read row counts"

within_1pct() {  # restored live  ->  |restored-live|*100 <= live  (0 vs 0 passes)
  local r="$1" l="$2" d
  if (( l == 0 )); then (( r == 0 )); return; fi
  d=$(( r > l ? r - l : l - r ))
  (( d * 100 <= l ))
}

echo "restore_test: items restored=${R_ITEMS} live=${L_ITEMS} | chunks restored=${R_CHUNKS} live=${L_CHUNKS}"
within_1pct "${R_ITEMS}"  "${L_ITEMS}"  || fail "items count ${R_ITEMS} not within 1% of live ${L_ITEMS}"
within_1pct "${R_CHUNKS}" "${L_CHUNKS}" || fail "chunks count ${R_CHUNKS} not within 1% of live ${L_CHUNKS}"

echo "restore_test: PASS (items & chunks within 1% of live)"
# cleanup runs via the EXIT trap

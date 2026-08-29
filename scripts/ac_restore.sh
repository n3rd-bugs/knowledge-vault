#!/usr/bin/env bash
# ac_restore.sh — AC-restore trigger for a battery-PAUSED nightly (owner 2026-08-29).
#
# The nightly pauses between items when the house is on battery (kbworker.powerguard);
# its tail writes $KB_STATE_DIR/battery-pause.json whenever a step paused with reason
# 'battery'. kb-ac-restore.timer runs this script every 5 minutes:
#
#   no marker            -> exit 0 silently (the common case; costs one stat).
#   marker + battery     -> clear any grid-ok stamp (a flap restarts the debounce), wait.
#   marker + unknown     -> same as battery: a trigger that RESTARTS the GPU never acts
#                           on ignorance (stale/absent sungrow reading).
#   marker + grid        -> DEBOUNCE: the first healthy observation writes
#                           $KB_STATE_DIR/grid-ok.stamp and exits; a later run finding
#                           the stamp >= GRID_DEBOUNCE_SECONDS old starts
#                           kb-consolidate.service (--no-block) and removes the stamp.
#
# The pause MARKER is deliberately NOT removed here: only a consolidation run that
# finishes with no battery pause clears it (powerguard.record_battery_pause), so a
# resume cut short by another outage re-arms this trigger by itself.
#
# --dry-run prints the decision and changes nothing (no stamp writes/removals, never
# starts the service).
#
# Test seams (tests/test_ac_restore.py): KB_ENV_FILE overrides which env file is
# sourced (default: the repo .env, same pattern as backup.sh), and PYTHON_BIN
# overrides the interpreter used for the probe (default: the repo venv). Never
# prints secrets — only state words, ages, and paths.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${KB_ENV_FILE:-${REPO_DIR}/.env}"

# Pull config from .env without echoing it (secrets live here too).
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
  set +a
fi

KB_STATE_DIR="${KB_STATE_DIR:-${HOME}/kb/state}"
KB_STATE_DIR="${KB_STATE_DIR/#\~/${HOME}}"   # expand a leading ~
MARKER="${KB_STATE_DIR}/battery-pause.json"
STAMP="${KB_STATE_DIR}/grid-ok.stamp"
GRID_DEBOUNCE_SECONDS="${GRID_DEBOUNCE_SECONDS:-240}"
PYTHON_BIN="${PYTHON_BIN:-${REPO_DIR}/.venv/bin/python}"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

# A live run owns both the GPU and the marker's fate (its clean finish clears
# it; a fresh pause rewrites it). Starting an active unit is a systemd no-op,
# but the first night in production (2026-08-30 00:05-00:45) showed this script
# logging a bogus "resuming" every ten minutes against a running consolidation
# — stand down until the unit is idle so the log tells the truth.
UNIT_STATE="$(systemctl --user is-active kb-consolidate.service 2>/dev/null || true)"
if [[ "${UNIT_STATE}" == "active" || "${UNIT_STATE}" == "activating" ]]; then
  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "ac_restore: dry-run: kb-consolidate is ${UNIT_STATE} — standing down"
  fi
  exit 0
fi

if [[ ! -f "${MARKER}" ]]; then
  # Nothing is paused — the every-5-minutes common case stays silent. A leftover
  # debounce stamp is swept here (verifier wfcd8e1248: a clean nightly clears
  # the MARKER but not the stamp, and an ancient stamp would let the NEXT pause
  # skip the debounce entirely on its first grid tick).
  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "ac_restore: dry-run: no pause marker at ${MARKER} — nothing to do"
  else
    rm -f "${STAMP}"
  fi
  exit 0
fi

# The probe prints grid|battery|unknown on stdout (exit 0/1/2 mirrors it); the
# WORD is authoritative so a crashed interpreter can never read as a state.
STATE="$("${PYTHON_BIN}" -m kbworker.powerguard || true)"
case "${STATE}" in
  grid|battery) ;;
  *) STATE="unknown" ;;
esac

if [[ "${STATE}" != "grid" ]]; then
  # battery or unknown: not safe to resume. Drop any half-built debounce stamp
  # so a flapping grid restarts the clock from zero.
  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "ac_restore: dry-run: state=${STATE} — would clear the grid-ok stamp and keep waiting"
  else
    rm -f -- "${STAMP}"
  fi
  exit 0
fi

if [[ ! -f "${STAMP}" ]]; then
  # First healthy observation: start the debounce clock, resume on a LATER run.
  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "ac_restore: dry-run: state=grid, no stamp — would start the ${GRID_DEBOUNCE_SECONDS}s debounce"
  else
    mkdir -p "${KB_STATE_DIR}"
    touch "${STAMP}"
  fi
  exit 0
fi

AGE="$(( $(date +%s) - $(stat -c %Y "${STAMP}") ))"
if (( AGE < GRID_DEBOUNCE_SECONDS )); then
  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "ac_restore: dry-run: state=grid, stamp ${AGE}s old (< ${GRID_DEBOUNCE_SECONDS}s) — would keep waiting"
  fi
  exit 0
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  echo "ac_restore: dry-run: state=grid, stamp ${AGE}s old (>= ${GRID_DEBOUNCE_SECONDS}s) — would start kb-consolidate.service"
  exit 0
fi

systemctl --user start --no-block kb-consolidate.service
echo "ac_restore: grid stable for ${AGE}s (>= ${GRID_DEBOUNCE_SECONDS}s) — resuming the battery-paused nightly (kb-consolidate.service)"
rm -f -- "${STAMP}"
# The marker stays: the resumed run's clean finish clears it (record_battery_pause),
# so a resume that pauses again on battery leaves this trigger armed.
exit 0

#!/usr/bin/env bash
# ac_restore.sh — AC-restore trigger for a battery-PAUSED nightly (owner 2026-08-29).
#
# The nightly pauses between items when the house is on battery (kbworker.powerguard);
# its tail writes $KB_STATE_DIR/battery-pause.json whenever a step paused with reason
# 'battery'. kb-ac-restore.timer runs this script every 5 minutes:
#
#   busy unit            -> exit 0, stand down (see STAND-DOWN below).
#   no marker            -> exit 0 silently (the common case; costs one stat), sweeping
#                           the debounce stamp and the resume-attempt counter.
#   marker + battery     -> clear any grid-ok stamp (a flap restarts the debounce), wait.
#   marker + unknown     -> same as battery: a trigger that RESTARTS the GPU never acts
#                           on ignorance (stale/absent sungrow reading).
#   marker + grid        -> DEBOUNCE: the first healthy observation writes
#                           $KB_STATE_DIR/grid-ok.stamp and exits; a later run finding
#                           the stamp >= GRID_DEBOUNCE_SECONDS old starts
#                           kb-consolidate.service (--no-block) and removes the stamp —
#                           unless the ATTEMPT CAP below says stop.
#
# STAND-DOWN (8afdc43; review #8 H10b/c). The trigger yields while
#   * kb-consolidate.service is active/activating — a live run owns the GPU and the
#     marker's fate (its clean finish clears it; a fresh pause rewrites it). Starting
#     an active unit is a systemd no-op, but the first production window
#     (2026-08-29 10:45 -> 2026-08-30 00:45) showed this script logging a bogus
#     "resuming" every ten minutes against a running consolidation;
#   * kb-consolidate.service is FAILED — its OnFailure has already paged; kicking a
#     failing run every 5 minutes is not a resume. The 01:00 nightly owns the retry
#     (pre-trigger behaviour);
#   * kb-backup.service / kb-backup-local.service is active/activating — the Sep 1 boot
#     showed ticks at boot+0:38 and boot+5:36 landing inside the boot-path backup while
#     the boot guard only fires at boot+10; a drain must not start under a dump or an
#     offsite push.
#
# ATTEMPT CAP (review #8 H10c). $KB_STATE_DIR/resume-attempts counts starts since the
# marker was armed. After RESUME_MAX_ATTEMPTS (3) starts without the marker clearing,
# ONE line says 'leaving leftovers to the nightly' and the trigger stands down until a
# clean run clears the marker (the no-marker path sweeps the counter with the stamp).
#
# The pause MARKER is deliberately NOT removed here: only a consolidation run that
# finishes with no battery pause clears it (powerguard.record_battery_pause), so a
# resume cut short by another outage re-arms this trigger by itself.
#
# --dry-run prints the decision and changes nothing (no stamp/counter writes or
# removals, never starts the service).
#
# Test seams (tests/test_ac_restore.py): KB_ENV_FILE overrides which env file is
# sourced (default: the repo .env, same pattern as backup.sh), PYTHON_BIN overrides
# the interpreter used for the probe (default: the repo venv), and systemctl resolves
# on PATH (a recording stub in the rig). Never prints secrets — only state words,
# ages, counts and paths.
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
ATTEMPTS="${KB_STATE_DIR}/resume-attempts"
GRID_DEBOUNCE_SECONDS="${GRID_DEBOUNCE_SECONDS:-240}"
RESUME_MAX_ATTEMPTS="${RESUME_MAX_ATTEMPTS:-3}"
PYTHON_BIN="${PYTHON_BIN:-${REPO_DIR}/.venv/bin/python}"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

# STAND-DOWN: a live drain, a failed drain, or a running backup (either unit) —
# see the header. `is-active` prints the state word and exits nonzero for
# anything but active, so the word is captured and the exit code ignored.
for unit in kb-consolidate.service kb-backup.service kb-backup-local.service; do
  UNIT_STATE="$(systemctl --user is-active "${unit}" 2>/dev/null || true)"
  case "${unit}:${UNIT_STATE}" in
    *:active|*:activating|kb-consolidate.service:failed)
      if [[ "${DRY_RUN}" == "1" ]]; then
        echo "ac_restore: dry-run: ${unit} is ${UNIT_STATE} — standing down"
      fi
      exit 0
      ;;
  esac
done

if [[ ! -f "${MARKER}" ]]; then
  # Nothing is paused — the every-5-minutes common case stays silent. A leftover
  # debounce stamp is swept here (verifier wfcd8e1248: a clean nightly clears
  # the MARKER but not the stamp, and an ancient stamp would let the NEXT pause
  # skip the debounce entirely on its first grid tick). The resume-attempt
  # counter is swept with it: a cleared marker means a run finished clean, so
  # the next pause starts its count from zero.
  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "ac_restore: dry-run: no pause marker at ${MARKER} — nothing to do"
  else
    rm -f -- "${STAMP}" "${ATTEMPTS}"
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

# ATTEMPT CAP: how many times has this marker already been resumed?
ATTEMPTS_N=0
if [[ -f "${ATTEMPTS}" ]]; then
  ATTEMPTS_N="$(tr -dc '0-9' < "${ATTEMPTS}")"
  ATTEMPTS_N="${ATTEMPTS_N:-0}"
fi
if (( ATTEMPTS_N >= RESUME_MAX_ATTEMPTS )); then
  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "ac_restore: dry-run: ${ATTEMPTS_N} resume start(s) without the marker clearing (cap ${RESUME_MAX_ATTEMPTS}) — would stand down"
  elif (( ATTEMPTS_N == RESUME_MAX_ATTEMPTS )); then
    # Said ONCE: the transition writes cap+1 so every later tick stays silent.
    echo "ac_restore: ${ATTEMPTS_N} resume starts without the pause marker clearing (cap ${RESUME_MAX_ATTEMPTS}) — leaving leftovers to the nightly; standing down until a clean run clears the marker"
    echo "$(( ATTEMPTS_N + 1 ))" > "${ATTEMPTS}"
  fi
  exit 0
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  echo "ac_restore: dry-run: state=grid, stamp ${AGE}s old (>= ${GRID_DEBOUNCE_SECONDS}s) — would start kb-consolidate.service (resume attempt $(( ATTEMPTS_N + 1 ))/${RESUME_MAX_ATTEMPTS})"
  exit 0
fi

# The marker's pause instant, for the resume line only (review #9a INS-8: AGE is
# the debounce stamp's age since the FIRST healthy tick after the stand-down,
# not how long the grid has held — say exactly that). The marker is JSON written
# by powerguard.record_battery_pause; 'at' is pulled with sed (no jq dependency),
# fractional seconds dropped, '?' when a marker carries none.
PAUSED_AT="$(sed -n 's/.*"at": *"\([^"]*\)".*/\1/p' "${MARKER}" 2>/dev/null | head -n 1 | sed -E 's/\.[0-9]+//')"
PAUSED_AT="${PAUSED_AT:-?}"

echo "$(( ATTEMPTS_N + 1 ))" > "${ATTEMPTS}"
systemctl --user start --no-block kb-consolidate.service
echo "ac_restore: grid healthy on consecutive ticks ${AGE}s apart (debounce ${GRID_DEBOUNCE_SECONDS}s) — resuming the battery-paused nightly (paused ${PAUSED_AT}; kb-consolidate.service; resume attempt $(( ATTEMPTS_N + 1 ))/${RESUME_MAX_ATTEMPTS})"
rm -f -- "${STAMP}"
# The marker stays: the resumed run's clean finish clears it (record_battery_pause),
# so a resume that pauses again on battery leaves this trigger armed.
exit 0

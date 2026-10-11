#!/usr/bin/env bash
# Install/refresh the Knowledge Vault systemd user units (PLAN.md §9).
# kb-mcp (Ph2), kb-worker (Ph3), kb-consolidate.{service,timer} (Ph4),
# kb-restore-test.{service,timer} + kb-cloudflared-refresh.{service,timer}
# + kb-backup.{service,timer} (§6d) + kb-consolidate-boot.{timer,service}
#   (catch-up drain + its boot-path guard: backup runs before the drain)
# + kb-boot-check.service (fires one ntfy line after an unclean shutdown)
# + kb-notify-fail@.service (the OnFailure notifier every unit hands off to).
#
# EVERY unit file in systemd/ is installed by the loop below — deliberately, not
# by an enumerated list. A hand-maintained list is how kb-consolidate-boot.timer
# came to exist only on the live machine, one `install_systemd.sh` away from
# being deleted (tests/test_unit_files.py asserts the loop covers them all).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
mkdir -p "${UNIT_DIR}"

# --- a CHANGED OnCalendar must not fire on install (2026-10-11) ---------------
# A calendar timer computes its next elapse from last_trigger, which systemd
# loads from the Persistent stamp when the timer STARTS, and a daemon-reload does
# not reset it. Moving a schedule LATER after today's new time has passed (last
# run 01:00:12, new time 01:30) starts a full nightly the moment the new unit
# loads. So, for every ALREADY-INSTALLED timer whose OnCalendar= lines differ
# from the repo copy: stop it BEFORE the copy, touch its stamp after it, let the
# daemon-reload below load the unit, then start it again if it was running (the
# start re-reads the stamp, so the next elapse is computed from now). New
# installs and unchanged timers are left exactly as before. Trade-off: a slot
# that fell between the old last run and now is skipped, not caught up. In
# particular, moving a schedule EARLIER and installing between the new and the
# old time (new 00:30, old 01:30, install at 01:00) SKIPS THAT NIGHT: the stamp
# says "ran now", so the next elapse is tomorrow's new slot and the old slot
# still ahead tonight is gone with the old unit. Install such a move before the
# new time, or start the service by hand afterwards.
oncalendar_of() {
  { grep -E '^[[:space:]]*OnCalendar[[:space:]]*=' "$1" 2>/dev/null || true; } |
    sed -E 's/^[[:space:]]*OnCalendar[[:space:]]*=[[:space:]]*//; s/[[:space:]]+$//'
}
# The stamp directory is the one the USER MANAGER reads: its own XDG_DATA_HOME,
# else its own HOME + /.local/share (systemd's xdg_user_data_dir), both read from
# `systemctl --user show-environment` -- not this shell's: a mismatch would touch
# a file systemd never reads, and the move would fire at once. A manager that
# ANSWERS without an XDG_DATA_HOME line uses its HOME, even when this shell
# exports one. Only when show-environment FAILS (no user bus) does this fall
# back to the shell's XDG_DATA_HOME, then ~/.local/share, with one stderr line.
# Resolved only when a timer is reseated (below), so an ordinary install makes no
# extra systemctl call.
#
# showenv_decode VALUE: one value as show-environment prints it -- bare, or, when
# it holds a special character, shell-quoted: current systemd (259 on the owner
# box) prints $'...' with \\ \' \n \t (and \xHH) escapes; older ones "..." or
# '...'. Decoded in pure bash -- the manager's output is never eval'd.
showenv_decode() {
  local v="$1" out="" c hex i=0 n
  if [[ "${v}" == "\$'"*"'" && ${#v} -ge 3 ]]; then
    v="${v:2:${#v}-3}"
    n=${#v}
    while ((i < n)); do
      c="${v:i:1}"
      if [[ "${c}" == '\' ]] && ((i + 1 < n)); then
        i=$((i + 1))
        c="${v:i:1}"
        case "${c}" in
          n) c=$'\n' ;;
          t) c=$'\t' ;;
          x)
            hex="${v:i+1:2}"
            if [[ "${hex}" =~ ^[0-9A-Fa-f]{2}$ ]]; then
              printf -v c "\\x${hex}"
              i=$((i + 2))
            else
              c='\x'
            fi
            ;;
        esac
      fi
      out+="${c}"
      i=$((i + 1))
    done
    v="${out}"
  elif [[ "${v}" == \'*\' && ${#v} -ge 2 ]]; then
    v="${v:1:${#v}-2}"
  elif [[ "${v}" == \"*\" && ${#v} -ge 2 ]]; then
    v="${v:1:${#v}-2}"
    v="$(printf '%s' "${v}" | sed -E 's/\\(.)/\1/g')"
  fi
  printf '%s' "${v}"
}
stamp_dir() {
  local out="" rc=0 xdg="" mhome=""
  out="$(systemctl --user show-environment 2>/dev/null)" || rc=$?
  if ((rc == 0)); then
    if [[ $'\n'"${out}" == *$'\n'XDG_DATA_HOME=* ]]; then
      xdg="$(showenv_decode "$(printf '%s\n' "${out}" | sed -n 's/^XDG_DATA_HOME=//p' | tail -n 1)")"
      if [[ "${xdg}" != /* ]]; then
        echo "install_systemd.sh: the user manager's XDG_DATA_HOME is not an absolute path; stamping under its HOME" >&2
        xdg=""
      fi
    fi
    if [[ -z "${xdg}" ]]; then
      if [[ $'\n'"${out}" == *$'\n'HOME=* ]]; then
        mhome="$(showenv_decode "$(printf '%s\n' "${out}" | sed -n 's/^HOME=//p' | tail -n 1)")"
      fi
      [[ "${mhome}" == /* ]] || mhome="${HOME}"
      xdg="${mhome}/.local/share"
    fi
  else
    echo "install_systemd.sh: systemctl --user show-environment failed rc=${rc}; stamping under this shell's XDG_DATA_HOME (else ~/.local/share)" >&2
    xdg="${XDG_DATA_HOME:-}"
    [[ "${xdg}" == /* ]] || xdg="${HOME}/.local/share"
  fi
  printf '%s/systemd/timers\n' "${xdg}"
}
RESEAT_TOUCH=""   # timers whose stamp is touched after the copy
RESEAT_RESTART="" # ...and of those, the ones to start again (they were active)
for timer in "${REPO_DIR}"/systemd/*.timer; do
  [[ -e "${timer}" ]] || continue
  name="$(basename "${timer}")"
  [[ -e "${UNIT_DIR}/${name}" ]] || continue
  old_cal="$(oncalendar_of "${UNIT_DIR}/${name}")"
  new_cal="$(oncalendar_of "${timer}")"
  [[ "${old_cal}" == "${new_cal}" ]] && continue
  was_active=no
  if systemctl --user is-active --quiet "${name}"; then was_active=yes; fi
  if ! systemctl --user stop "${name}"; then
    echo "install_systemd.sh: could not stop ${name}; refusing to install its changed OnCalendar onto a running timer (it would fire at once)" >&2
    exit 1
  fi
  RESEAT_TOUCH="${RESEAT_TOUCH} ${name}"
  [[ "${was_active}" == yes ]] && RESEAT_RESTART="${RESEAT_RESTART} ${name}"
  echo "reseat: ${name} OnCalendar ${old_cal//$'\n'/; } -> ${new_cal//$'\n'/; } (stopped; stamp touched; was active: ${was_active})"
done

for unit in "${REPO_DIR}"/systemd/*.service "${REPO_DIR}"/systemd/*.timer; do
  [[ -e "${unit}" ]] || continue
  install -m 644 "${unit}" "${UNIT_DIR}/"
done
# The notifier scripts are executed from the repo (like scripts/backup.sh), so
# they only need to be executable — but a lost +x turns an alert into a second
# failure (or a silent boot recovery), so assert it here rather than trusting the
# checkout. kb-boot-check.sh is the unclean-shutdown notifier's ExecStart.
chmod 755 "${REPO_DIR}/systemd/kb-notify-fail.sh"
chmod 755 "${REPO_DIR}/systemd/kb-boot-check.sh"
# The reseated timers' stamps: "last triggered now", read when each next starts.
if [[ -n "${RESEAT_TOUCH}" ]]; then
  STAMP_DIR="$(stamp_dir)"
  mkdir -p "${STAMP_DIR}"
  for name in ${RESEAT_TOUCH}; do touch "${STAMP_DIR}/stamp-${name}"; done
fi

# --- tiktoken cache: persistent, and warm before the first run ----------------
# tiktoken's default cache lives under /tmp (tmpfs), so every boot wipes it and
# the first chunker call re-downloads the BPE. A post-power-cut catch-up did that
# with no network yet and hard-failed. kb-consolidate.service pins
# TIKTOKEN_CACHE_DIR here; seed it from the vendored copy so the first run after
# an install never needs the network either.
TIKTOKEN_DIR="${HOME}/kb/cache/tiktoken"
mkdir -p "${TIKTOKEN_DIR}"
if compgen -G "${REPO_DIR}/vendor/tiktoken/*" > /dev/null; then
  cp -n "${REPO_DIR}"/vendor/tiktoken/* "${TIKTOKEN_DIR}/" 2>/dev/null || true
fi

systemctl --user daemon-reload
# A reseated timer that was running before its stop runs again NOW -- right
# after the reload that loaded its new OnCalendar, and BEFORE any enable line
# below: under set -e a later failing step (enable --now kb-worker.service during
# a DB outage) would otherwise abort the install with the nightly left stopped
# and nothing to fire OnFailure. The start reads the stamp touched above. A no-op
# for one an `enable --now` below starts again; one that was stopped stays so.
for name in ${RESEAT_RESTART}; do systemctl --user start "${name}"; done
# kb-mcp.service is NOT enabled: the MCP server runs as the kb-mcp CONTAINER
# (docker compose), and both bind 127.0.0.1:8321 — the loser dies with
# "[Errno 98] address already in use" on every boot. The unit is kept for a
# docker-less deployment, but enabling it here silently re-broke a fix that had
# already been applied live (disabled 2026-07-31, re-enabled by this installer
# 2026-08-01). Enable it only if you are NOT running the container.
# systemctl --user enable --now kb-mcp.service
systemctl --user enable --now kb-worker.service
# The consolidation is timer-driven: enable the TIMER (not the oneshot service).
systemctl --user enable --now kb-consolidate.timer
# Catch-up after an unclean shutdown: Persistent=true only covers a window that
# was MISSED, not a run that started and was killed 32 minutes in (2026-07-30).
# Enabled WITHOUT --now on purpose: OnBootSec=10min is already in the past on a
# box that has been up for a day, so `--now` would start a 4.5–7h consolidation
# the moment someone runs this installer. It arms itself at the next boot, which
# is the only moment it is meant to fire.
systemctl --user enable kb-consolidate-boot.timer
# kb-backup is now ENABLED by default. It used to be left off "until the owner
# reviews the schedule", which in practice meant the only backup was
# kb-consolidate's ExecStartPost — conditional on a 4.5–7h run completing, so an
# interrupted night produced nothing at all (29 + 30 July have no dump). The
# local dump needs no configuration; the offsite leg stays opt-in via .env.
systemctl --user enable --now kb-backup.timer
# kb-boot-check detects an unclean prior shutdown and pushes ONE ntfy line.
# Enabled WITH --now on purpose (unlike the boot catch-up timer): --now arms the
# dirty-flag marker immediately, so a clean shutdown TODAY is recorded and the
# first run cannot false-positive (an absent marker reads as clean). It does no
# heavy work — it only touches a marker file and, at most, sends one push.
# RemainAfterExit keeps it active so its ExecStop fires on an orderly shutdown.
systemctl --user enable --now kb-boot-check.service
# kb-consolidate-boot.service (the boot-path guard the boot timer triggers) is
# deliberately NOT enabled here: it has no [Install] and is started only by
# kb-consolidate-boot.timer's Unit=. Installing the file (loop above) is enough.
# kb-restore-test is intentionally left DISABLED (it spins a throwaway container);
# enable manually: systemctl --user enable --now kb-restore-test.timer
# kb-cloudflared-refresh is also left DISABLED (it re-pulls + recreates the public
# edge); enable manually: systemctl --user enable --now kb-cloudflared-refresh.timer
# kb-notify-fail@ is a TEMPLATE pulled in by OnFailure= — never enabled directly.
# refresh semantics: a unit that was already running restarts onto new code/config
systemctl --user try-restart kb-mcp.service || true
systemctl --user try-restart kb-worker.service || true

# Keep user services alive after logout (required once per machine).
loginctl enable-linger "${USER}" || true

systemctl --user --no-pager status kb-mcp.service || true
systemctl --user --no-pager status kb-worker.service || true
systemctl --user --no-pager list-timers 'kb-*' || true
echo "installed: every unit in ${REPO_DIR}/systemd (services + timers)"
echo "  enabled: kb-worker, kb-consolidate.timer, kb-consolidate-boot.timer, kb-backup.timer, kb-boot-check.service"
echo "  installed but not enabled: kb-restore-test.timer, kb-cloudflared-refresh.timer"
echo "  kb-consolidate-boot.service is the boot guard, started only by kb-consolidate-boot.timer"
echo "  kb-notify-fail@.service is a template invoked via OnFailure= (never enabled directly)"
echo "  tiktoken cache: ${TIKTOKEN_DIR} (pre-warmed from vendor/tiktoken)"
echo "logs: journalctl --user -u kb-mcp -f | -u kb-worker -f | -u kb-consolidate -f | -u kb-boot-check -f"
# MANUAL CLEANUP — this installer runs NO docker. The compose stack pins its
# network to 'kbnet' (docker-compose.yml), which orphaned the old auto-created
# default network 'data-server_default'. Remove it by hand once nothing uses it:
echo "manual cleanup (no docker is run here): the orphaned compose network"
echo "  'data-server_default' can be removed once nothing is attached —"
echo "    docker network rm data-server_default"

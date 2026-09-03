#!/usr/bin/env bash
# kb-boot-check.sh — push ONE ntfy line when the box came back from an unclean
# shutdown (power loss / crash), nothing otherwise — and log ONE line saying how
# the PREVIOUS boot ended, so the next event is self-explaining (review #8 H10j).
#
# WHY: after a power cut the machine restarts with no signal that it went down
# hard — four reboots on 2026-08-01 were completely silent. This turns each
# unclean recovery into exactly one notification, so the grid blipping is visible
# without anyone having to go read journald.
#
# HOW: kbcore.boot keeps a persistent dirty-flag marker — armed with the running
# boot id while up, removed by this unit's ExecStop on a clean stop. `boot check`
# arms it and prints the recovery line ONLY when the marker survived from a
# previous, uncleanly-ended boot, so this script pushes at most once per boot and
# never on a clean reboot. The idempotency lives in kbcore.boot, not here (see
# src/kbcore/boot.py); this file is only the ntfy transport.
#
# BOOT CLASSIFICATION (review #8 H10j): the marker says clean/unclean; the
# previous boot's journal says HOW it ended — PID 1 logs 'Reached target
# reboot.target' / 'poweroff.target' on an orderly stop and nothing at all on a
# power loss. One line 'boot-check: previous boot ended <class>' goes to stdout
# (journald under this unit) and to $LOG on EVERY boot:
#   orderly-reboot | orderly-poweroff | orderly-halt | orderly-kexec
#   orderly (journal end: none)   marker clean, no target seen (first boot, rotated
#                                 journal, or no adm/systemd-journal group)
#   unclean (journal end: <x>)    marker survived; 'none' = power loss, a target =
#                                 an orderly stop where this unit's ExecStop never ran
# The 2026-09-01 20:58 event (an owner-requested GNOME reboot 12 min into a real
# outage) read as a mystery for a day; this line would have said orderly-reboot.
# The unclean push carries the class too. Reading is best-effort: any journal
# failure degrades to 'none', never to a failed unit.
#
# CREDENTIAL HANDLING mirrors kb-notify-fail.sh EXACTLY: the ntfy auth NEVER
# reaches argv (world-readable in /proc, and it leaked once via a `bash -x`
# trace). curl reads it from a config on STDIN (`-K -`). Absent config = silently
# do nothing — a box with no ntfy channel must not turn a recovery into an error.
#
# ALWAYS exits 0: a boot-time unit that can fail is a boot loop waiting to happen.
# curl's exit code is not swallowed — it goes to the same log kb-notify-fail.sh uses.
#
# Test seams (tests/test_boot_check_classify.py): KB_PYTHON (the marker helper's
# interpreter), KB_JOURNALCTL (a stub printing canned PID-1 lines), KB_NOTIFY_ENV,
# KB_NOTIFY_LOG.
set -u

PYTHON="${KB_PYTHON:-$HOME/kb/repo/.venv/bin/python}"
CFG="${KB_NOTIFY_ENV:-$HOME/.claude/notify.env}"
JOURNALCTL="${KB_JOURNALCTL:-journalctl}"

LOG="${KB_NOTIFY_LOG:-$HOME/.claude/hooks/notify-fail.log}"
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
log_line() { { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" >> "$LOG"; } 2>/dev/null || true; }
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 200 ]; then
  { tail -100 "$LOG" > "$LOG.t" && mv "$LOG.t" "$LOG"; } 2>/dev/null || true
fi

# Arm the marker for THIS boot and learn whether the LAST shutdown was unclean.
# Runs regardless of notification config, so detection state stays correct even
# on a box with no ntfy channel — it just will not push. A python fault here must
# not fail the unit (no boot loop): fall through to a clean exit.
MSG="$("$PYTHON" -m kbcore.boot check 2>>"$LOG" || true)"

# How did the PREVIOUS boot end, per PID 1's last shutdown target? Only PID 1's
# lines are read (a few hundred, not the whole boot). Empty when there is no
# previous boot, the journal is unreadable, or the box simply lost power.
JOURNAL_END="$("$JOURNALCTL" -b -1 -q -o cat --no-pager _PID=1 2>/dev/null \
  | grep -oE 'Reached target (reboot|poweroff|halt|kexec)\.target' | tail -1 \
  | sed -E 's/Reached target ([a-z]+)\.target/\1/')"
JOURNAL_END="${JOURNAL_END:-none}"
if [ -n "$MSG" ]; then
  CLASS="unclean (journal end: ${JOURNAL_END})"
elif [ "$JOURNAL_END" != "none" ]; then
  CLASS="orderly-${JOURNAL_END}"
else
  CLASS="orderly (journal end: none)"
fi
echo "boot-check: previous boot ended ${CLASS}"
log_line "boot-check: previous boot ended ${CLASS}"

[ -n "$MSG" ] || exit 0   # clean boot: nothing to report

[ -r "$CFG" ] || log_line "boot-check skipped: no config at $CFG"
[ -r "$CFG" ] || exit 0
# shellcheck disable=SC1090
. "$CFG"
[ -n "${NTFY_TOPIC:-}" ] || log_line "boot-check skipped: no NTFY_TOPIC in $CFG"
[ -n "${NTFY_TOPIC:-}" ] || exit 0

BASE="${NTFY_URL:-https://ntfy.sh}"
MACHINE="${NTFY_MACHINE:-$(hostname 2>/dev/null || echo unknown)}"
BODY="${MSG} [${CLASS}] @ ${MACHINE}"

# BOUNDED RETRY WITH BACKOFF, same shape and budget as kb-notify-fail.sh: this
# push races cloudflared and ntfy coming up right after a boot, so the first
# attempt is the one most likely to hit a dead name or a refused connection.
# 3 x 10s + (10 + 30) = 70s, which must stay under the unit's TimeoutStartSec=120.
NOTIFY_TRIES=3
NOTIFY_MAX_TIME=10
NOTIFY_BACKOFF="10 30"   # one entry fewer than NOTIFY_TRIES: no sleep after the last

# The config on stdin carries ONLY auth; everything else is non-secret argv.
ntfy_auth_config() {   # -> a curl config on stdout; empty when unauthenticated
  if [ -n "${NTFY_TOKEN:-}" ]; then
    printf 'header = "Authorization: Bearer %s"\n' "$NTFY_TOKEN"
  elif [ -n "${NTFY_USER:-}" ] && [ -n "${NTFY_PASSWORD:-}" ]; then
    printf 'user = "%s:%s"\n' "$NTFY_USER" "$NTFY_PASSWORD"
  fi
}

try=1
for delay in $NOTIFY_BACKOFF ""; do
  ERR=$(ntfy_auth_config | curl -fsS -4 --max-time "$NOTIFY_MAX_TIME" -K - \
    -H "Title: Knowledge Vault BOOT" \
    -H "Priority: default" \
    -H "Tags: electric_plug" \
    -d "$BODY" \
    "${BASE%/}/${NTFY_TOPIC}" 2>&1 >/dev/null)
  RC=$?
  log_line "boot-check push try=$try/$NOTIFY_TRIES rc=$RC ${ERR:-ok}"
  [ "$RC" -eq 0 ] && break
  [ -n "$delay" ] || break
  try=$((try + 1))
  sleep "$delay"
done

# ALWAYS succeed: a failing boot-time notifier is a loop, and the marker is
# already consumed so a non-zero exit would buy nothing. The rc is in $LOG.
exit 0

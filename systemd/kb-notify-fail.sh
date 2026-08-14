#!/usr/bin/env bash
# kb-notify-fail.sh — push an alert when a Knowledge Vault unit FAILS.
#
# Invoked only by kb-notify-fail@.service, which every long-running kb unit names
# in OnFailure=. Without it a failed nightly is discovered by noticing, days
# later, that nothing has been consolidated or backed up — which is how 29 and 30
# July passed with no backup at all.
#
# CREDENTIAL HANDLING: the ntfy auth NEVER reaches argv. Anything on a command
# line is world-readable in /proc for the life of the process and lands in shell
# history and `bash -x` traces — that last one is precisely how an ntfy admin
# password leaked into a transcript on 2026-07-31 and had to be rotated. curl
# reads its auth from a config file on STDIN (`-K -`) instead, so the secret only
# ever exists in this process's memory and a pipe.
#
# TWO CREDENTIAL SHAPES, and both are still live. A remote box holds NTFY_TOKEN, a
# WRITE-ONLY publish token — that is all GET /api/setup/notify.env ever hands out.
# The OWNER box holds NTFY_USER/NTFY_PASSWORD for the ntfy admin account, which is
# the pair that leaked and was rotated. Token first, basic auth as the fallback.
#
# Channel config is the same ~/.claude/notify.env the completion hooks use (a
# machine fetches it from the KB host at enrolment; see GET /api/setup/notify.env).
# Absent config = silently do nothing: a box without notifications configured must
# not turn every failure into a SECOND failure.
set -u

UNIT="${1:-unknown-unit}"
CFG="${KB_NOTIFY_ENV:-$HOME/.claude/notify.env}"

# INVOCATION LOG. This script used to end its curl with `>/dev/null 2>&1 || true`,
# discarding the exit code — so a push that never arrived looked byte-identical to
# one that did. That is the exact invisible-failure bug notify-complete.sh already
# fixed, and it is worse here: this is the notifier for UNATTENDED failures, i.e.
# the one path where nobody is present to notice its silence. One line per
# attempt, with rc and curl's stderr, self-truncating.
LOG="${KB_NOTIFY_LOG:-$HOME/.claude/hooks/notify-fail.log}"
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
log_line() { { printf '%s %s\n' "$(date +%H:%M:%S)" "$*" >> "$LOG"; } 2>/dev/null || true; }
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 200 ]; then
  { tail -100 "$LOG" > "$LOG.t" && mv "$LOG.t" "$LOG"; } 2>/dev/null || true
fi

# Both stand-downs are LOGGED rather than silent: "no channel is configured" and
# "the push failed" look identical from the outside — a missing notification —
# and they have opposite fixes. The `|| exit 0` lines keep their exact shape
# because tests/test_unit_files.py asserts it verbatim, so the log line sits
# beside each guard instead of inside a brace group that would break that check.
[ -r "$CFG" ] || log_line "$UNIT skipped: no config at $CFG"
[ -r "$CFG" ] || exit 0
# shellcheck disable=SC1090
. "$CFG"
[ -n "${NTFY_TOPIC:-}" ] || log_line "$UNIT skipped: no NTFY_TOPIC in $CFG"
[ -n "${NTFY_TOPIC:-}" ] || exit 0

BASE="${NTFY_URL:-https://ntfy.sh}"
MACHINE="${NTFY_MACHINE:-$(hostname 2>/dev/null || echo unknown)}"
# One line of context beats a bare "it failed": the exit result and the last log
# line are what decide whether this needs attention now or at breakfast.
RESULT="$(systemctl --user show -p Result --value "$UNIT" 2>/dev/null || true)"
# TRUNCATED on purpose. This is a phone notification, not a log shipper: an
# untrimmed journal tail can be kilobytes, and log lines are the one place a
# credential still appears in the clear (the KB scrubs what it stores; journald
# does not). Two lines, 160 characters — enough to tell "OOM" from "no such
# host", with `journalctl --user -u <unit>` one command away for the rest.
#
# REDACTED before it leaves the box. The excerpt is raw journald text and ships
# to an external ntfy server, so it passes through a shell-side redaction first:
# the shell-greppable prefixes of kbcore.scrub's SECRET_PATTERNS — kbg_/kbe_
# (the credentials this system itself mints), ghp_, tk_ (the ntfy token, which
# is exactly the secret most likely to appear in THIS unit's journal), the age
# backup identity, the Google refresh token, and any Bearer value — each
# replaced with a plain ASCII '***'. Redaction runs BEFORE the 160-char cut, so
# the cut can never leave a live token half visible at the boundary; the output
# stays ASCII and stays bounded exactly as before.
kb_redact() { # stdin -> stdout with credential shapes replaced by ***
  sed -E \
    -e 's/kb[ge]_[A-Za-z0-9_-]+/***/g' \
    -e 's/ghp_[A-Za-z0-9]+/***/g' \
    -e 's/tk_[A-Za-z0-9]+/***/g' \
    -e 's/AGE-SECRET-KEY-[0-9A-Z]+/***/g' \
    -e 's|1//0[A-Za-z0-9_-]+|***|g' \
    -e 's/[Bb]earer +[A-Za-z0-9+/=._-]+/Bearer ***/g'
}
TAIL="$(journalctl --user -u "$UNIT" -n 2 --no-pager -o cat 2>/dev/null | tr '\n' ' ' | kb_redact | cut -c1-160 || true)"
MSG="FAILED: ${UNIT} @ ${MACHINE} (result=${RESULT:-unknown}) ${TAIL}"

# BOUNDED RETRY WITH BACKOFF. The failures most worth alerting on cluster around
# BOOT — the catch-up drain, a unit that died with the machine, anything after a
# power cut — and at boot this push races cloudflared and ntfy coming up, so the
# first attempt is the one most likely to hit a dead name or a refused connection.
# curl's own --retry is the wrong tool: its delays are seconds, and the thing being
# waited for is a container starting.
#
# The budget is arithmetic, not vibes: NOTIFY_TRIES x NOTIFY_MAX_TIME plus the
# backoff sleeps must stay under the unit's TimeoutStartSec, or systemd kills the
# notifier mid-push and we are back to a silent failure by another route.
# 3 x 10s + (10 + 30) = 70s against TimeoutStartSec=120. tests/test_ops_health.py
# re-derives that sum from BOTH files, so changing one alone is a red test.
NOTIFY_TRIES=3
NOTIFY_MAX_TIME=10
NOTIFY_BACKOFF="10 30"  # one entry fewer than NOTIFY_TRIES: no sleep after the last

# The config on stdin carries ONLY auth; everything else is non-secret argv.
ntfy_auth_config() { # -> a curl config on stdout; empty when unauthenticated
  if [ -n "${NTFY_TOKEN:-}" ]; then
    printf 'header = "Authorization: Bearer %s"\n' "$NTFY_TOKEN"
  elif [ -n "${NTFY_USER:-}" ] && [ -n "${NTFY_PASSWORD:-}" ]; then
    printf 'user = "%s:%s"\n' "$NTFY_USER" "$NTFY_PASSWORD"
  fi
}

# -4: the AAAA record resolves first here and IPv6 egress fails, so happy-eyeballs
# would spend part of the budget on a doomed attempt. stderr is CAPTURED, not
# discarded: "could not resolve host" and "401" have opposite fixes, and the log is
# where that distinction has to survive until someone reads it.
try=1
for delay in $NOTIFY_BACKOFF ""; do
  ERR=$(ntfy_auth_config | curl -fsS -4 --max-time "$NOTIFY_MAX_TIME" -K - \
    -H "Title: Knowledge Vault FAILURE" \
    -H "Priority: high" \
    -H "Tags: rotating_light" \
    -d "$MSG" \
    "${BASE%/}/${NTFY_TOPIC}" 2>&1 >/dev/null)
  RC=$?
  log_line "$UNIT push try=$try/$NOTIFY_TRIES rc=$RC ${ERR:-ok}"
  [ "$RC" -eq 0 ] && break
  [ -n "$delay" ] || break
  try=$((try + 1))
  sleep "$delay"
done

# ALWAYS succeed: a failing failure-notifier is a loop, and a non-zero exit here
# would also mask the original failure's status in
# `systemctl --user list-units --failed`. The rc is no longer swallowed with it —
# it is in $LOG, which is the entire point of this change.
exit 0

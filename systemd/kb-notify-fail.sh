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
# Channel config is the same ~/.claude/notify.env the completion hooks use (the
# machine fetches it from the KB host at install time; see
# GET /api/setup/notify.env). Absent config = silently do nothing: a box without
# notifications configured must not turn every failure into a SECOND failure.
set -u

UNIT="${1:-unknown-unit}"
CFG="${KB_NOTIFY_ENV:-$HOME/.claude/notify.env}"
[ -r "$CFG" ] || exit 0
# shellcheck disable=SC1090
. "$CFG"
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
TAIL="$(journalctl --user -u "$UNIT" -n 2 --no-pager -o cat 2>/dev/null | tr '\n' ' ' | cut -c1-160 || true)"
MSG="FAILED: ${UNIT} @ ${MACHINE} (result=${RESULT:-unknown}) ${TAIL}"

# The config on stdin carries ONLY auth; everything else is non-secret argv.
{
  if [ -n "${NTFY_TOKEN:-}" ]; then
    printf 'header = "Authorization: Bearer %s"\n' "$NTFY_TOKEN"
  elif [ -n "${NTFY_USER:-}" ] && [ -n "${NTFY_PASSWORD:-}" ]; then
    printf 'user = "%s:%s"\n' "$NTFY_USER" "$NTFY_PASSWORD"
  fi
} | curl -fsS -4 --max-time 15 --retry 2 --retry-delay 2 -K - \
  -H "Title: Knowledge Vault FAILURE" \
  -H "Priority: high" \
  -H "Tags: rotating_light" \
  -d "$MSG" \
  "${BASE%/}/${NTFY_TOPIC}" >/dev/null 2>&1 || true

# ALWAYS succeed: a failing failure-notifier is a loop, and it would also mask
# the original failure's status in `systemctl --user list-units --failed`.
exit 0

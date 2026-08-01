#!/usr/bin/env bash
# Stop hook: Claude finished responding. Push a short notice if a channel is
# configured, otherwise do nothing at all.
#
# WHY IT IS A NO-OP BY DEFAULT: a hook that fails, or that writes to a terminal
# nobody is watching, is worse than no hook — it adds noise to every single
# response for no benefit. Configure a channel in ~/.claude/notify.env and this
# starts working; leave it absent and it costs one process spawn.
#
# Channels, in order:
#   NTFY_TOPIC   -> https://ntfy.sh/<topic>   (phone push; install the ntfy app
#                   and subscribe. The topic IS the secret — anyone who knows it
#                   can read your notifications, so use a long random one.)
#   NTFY_URL     -> a self-hosted ntfy base, if you would rather not use ntfy.sh
#   otherwise    -> notify-send, which only reaches a desktop session ON THIS BOX
set -u
CFG="$HOME/.claude/notify.env"
[ -r "$CFG" ] && . "$CFG"

# Claude Code feeds the hook a JSON event on stdin; we only need to know it
# fired, so drain it rather than risk a blocked write upstream.
INPUT=$(cat 2>/dev/null || true)

# Invocation log. Without it there is no way to distinguish "Claude Code never
# called this hook" from "it ran and the push failed" — and those have opposite
# fixes. One line per fire, self-truncating.
LOG="$HOME/.claude/hooks/notify.log"
SID_LOG=$(printf '%s' "$INPUT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
CWD_LOG=$(printf '%s' "$INPUT" | grep -o '"cwd"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
{ printf '%s fired sid=%.8s proj=%s\n' "$(date +%H:%M:%S)" "${SID_LOG:-none}" "$(basename "${CWD_LOG:-$PWD}")" >> "$LOG"; } 2>/dev/null || true
[ -f "$LOG" ] && [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 200 ] && tail -100 "$LOG" > "$LOG.t" && mv "$LOG.t" "$LOG"

CWD=$(printf '%s' "$INPUT" | grep -o '"cwd"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
WHERE=$(basename "${CWD:-$PWD}")
# How long did this turn take? turn-start.sh stamps it at UserPromptSubmit, and
# the duration rides in the message so a glance tells you whether it was a quick
# answer or a long agentic run.
#
# DEFAULT 0 — notify on EVERY completion (owner decision 2026-07-31). The usual
# argument against is that a hook pinging constantly becomes noise you learn to
# ignore; it does not apply here, because this is a private self-hosted channel
# whose only subscriber is the owner. Nothing else competes for attention in it,
# so a missed completion costs more than an extra ping. Set NTFY_MIN_SECONDS to a
# positive number of seconds to suppress turns shorter than that.
SID=$(printf '%s' "$INPUT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
MARK="${TMPDIR:-/tmp}/claude-turns/${SID:-none}"
SECS=""
if [ -n "${SID:-}" ] && [ -r "$MARK" ]; then
  START=$(cat "$MARK" 2>/dev/null || echo "")
  rm -f "$MARK" 2>/dev/null || true
  case "$START" in ''|*[!0-9]*) START="" ;; esac
  [ -n "$START" ] && SECS=$(( $(date +%s) - START ))
fi

MIN="${NTFY_MIN_SECONDS:-0}"
# Unknown duration (first turn after install, or a lost marker) NOTIFIES rather
# than stays silent: a missed ping is worse than an extra one for this purpose.
if [ -n "$SECS" ] && [ "$SECS" -lt "$MIN" ]; then
  exit 0
fi

if [ -n "$SECS" ]; then
  if [ "$SECS" -ge 60 ]; then TOOK=" ($((SECS / 60))m$((SECS % 60))s)"; else TOOK=" (${SECS}s)"; fi
else
  TOOK=""
fi
# WHICH MACHINE. With this hook shipped to every box (linux, windows, rm1) the
# project alone is ambiguous — the same repo is worked on from more than one.
MACHINE="${NTFY_MACHINE:-$(hostname 2>/dev/null || echo unknown)}"
MSG="Claude finished — ${WHERE} @ ${MACHINE}${TOOK}"

if [ -n "${NTFY_TOPIC:-}" ]; then
  BASE="${NTFY_URL:-https://ntfy.sh}"
  # Bounded and backgrounded: a slow or dead push must never delay the session.
  #
  # NOTHING SECRET GOES ON THE COMMAND LINE. `-u user:pass` and
  # `-H "Authorization: ..."` put the credential in argv, which is world-readable
  # through /proc/<pid>/cmdline and shows up in `ps` — on this system an admin
  # password has already leaked into a transcript exactly that way, and a
  # transcript is what these hooks flush into the KB. `curl -K -` reads the same
  # options from a config file; feeding that file on STDIN keeps the credential
  # in a pipe, out of argv and off disk. The URL and everything non-secret stay
  # as normal arguments so the invocation is still readable in a process list.
  #
  # A scoped WRITE-ONLY token is the right credential for a remote machine: it
  # can announce completions and nothing else — it cannot read your notification
  # history (which carries client project names) and cannot publish to another
  # topic. Basic auth remains for the owner box, which holds the admin account.
  ntfy_auth_config() { # -> a curl config on stdout; empty when unauthenticated
    if [ -n "${NTFY_TOKEN:-}" ]; then
      printf 'header = "Authorization: Bearer %s"\n' "$NTFY_TOKEN"
    elif [ -n "${NTFY_USER:-}" ] && [ -n "${NTFY_PASSWORD:-}" ]; then
      printf 'user = "%s:%s"\n' "$NTFY_USER" "$NTFY_PASSWORD"
    fi
  }
    # NTFY_TOKEN / NTFY_USER+NTFY_PASSWORD authenticate against a SELF-HOSTED ntfy.
    # Required there and harmless on ntfy.sh: an instance reachable from the
    # internet must run auth-default-access=deny-all, or the topic name alone lets
    # anyone read your notifications — and this payload carries the working
    # directory, i.e. a client project name.
    # FOREGROUND, deliberately. This used to be backgrounded with `&` so a slow
    # push could not delay the session — but the hook then hit `exit 0` while curl
    # was still in flight, and Claude Code reaps the hook's process group on exit,
    # so the push was killed mid-request. Delivery became a race that mostly lost:
    # notifications stopped arriving while the hook still reported success.
    # --max-time 8 against the hook's own 10s timeout bounds the wait, so the
    # original concern (never delay the session) is still satisfied without the race.
    # -4: the AAAA record resolves first here and IPv6 egress fails, so happy-eyeballs
    # wastes part of the budget on a doomed attempt. --retry 2 covers a transient
    # tunnel blip. The exit code is LOGGED: `|| true` alone made every failure
    # invisible, which is why a broken push looked identical to a working one for
    # two rounds of debugging.
    ERR=$(ntfy_auth_config | curl -fsS -4 -K - --max-time 8 --retry 2 --retry-delay 1 \
        -H "Title: Claude Code" -H "Priority: default" \
        -d "$MSG" "${BASE%/}/$NTFY_TOPIC" 2>&1 >/dev/null)
    RC=$?
    { printf '%s push rc=%s %s\n' "$(date +%H:%M:%S)" "$RC" "${ERR:-ok}" >> "$LOG"; } 2>/dev/null || true
elif command -v notify-send >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  notify-send "Claude Code" "$MSG" >/dev/null 2>&1 || true
fi

exit 0   # never block or fail the session

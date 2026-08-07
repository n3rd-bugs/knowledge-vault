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
  if [ "$SECS" -ge 60 ]; then TOOK="$((SECS / 60))m$((SECS % 60))s"; else TOOK="${SECS}s"; fi
else
  TOOK=""
fi

# ------------------------------------------------------ how much reached the KB
# "kb 4.1/5.2MB" — of this transcript's 5.2 MB, 4.1 MB have been accepted by the
# Knowledge Vault. It answers the one question the completion ping could not:
# whether the work just done is captured, or is still sitting on this disk.
#
# THE NUMBER COMES FROM THIS SESSION'S OWN CHECKPOINT FILE, never from the shared
# flush log. That log is append-only and machine-wide: every session on the box
# writes "ok bytes=" lines into it, so reading the last one attributes a
# NEIGHBOUR's flush to this session. That is precisely the misattribution the
# backfill sweep shipped with and had to fix; do not reintroduce it by grepping
# the log. The per-session offset file has no such ambiguity — its name IS the
# session identity.
#
# EVERY read here is a local stat/cat/awk on files the hooks themselves own, and
# every failure path returns EMPTY rather than guessing, so the worst case is a
# notification that looks exactly like the one before this feature existed.

kb_human() { # <bytes> [<denominator bytes>] -> "437B" | "812KB" | "4.1MB" | "1.2GB"
  # With two arguments the pair SHARES one unit, chosen from the second (the
  # larger, i.e. the transcript size): "4.1/5.2MB" reads at a glance, where
  # "4.1MB/5.2MB" makes the reader compare units before values.
  awk -v a="$1" -v b="${2:-}" 'BEGIN {
    s = (b == "" ? a : b)
    if      (s >= 1073741824) { d = 1073741824; u = "GB"; p = 1 }
    else if (s >= 1048576)    { d = 1048576;    u = "MB"; p = 1 }
    else if (s >= 1024)       { d = 1024;       u = "KB"; p = 0 }
    else                      { d = 1;          u = "B";  p = 0 }
    if (b == "") printf "%.*f%s", p, a / d, u
    else         printf "%.*f/%.*f%s", p, a / d, p, b / d, u
  }' 2>/dev/null
}

kb_posixpath() { # JSON-escaped native Windows path -> POSIX; anything else passes through
  # A Windows event carries "C:\\Users\\..." and our grep/sed extraction keeps
  # the doubled backslashes (the flush hook never sees them — its jq/python3
  # backend unescapes during parsing). Collapse them, then map the drive path
  # the way flush-session-to-kb.sh's kb_winpath does: cygpath when it exists,
  # else the pure-shell C:\x -> /c/x fallback. Non-drive paths pass through
  # untouched, so a Linux path containing a backslash is never corrupted.
  case "${1:-}" in [A-Za-z]:*) ;; *)
    printf '%s' "${1:-}"
    return 0
    ;;
  esac
  _kbp=$(printf '%s' "$1" | sed 's/\\\\/\\/g')
  if command -v cygpath >/dev/null 2>&1; then
    _kbw=$(cygpath -u "$_kbp" 2>/dev/null) || _kbw=""
  else
    _kbw=""
  fi
  [ -n "$_kbw" ] || _kbw="/$(printf '%s' "$_kbp" | cut -c1 | tr '[:upper:]' '[:lower:]')$(printf '%s' "$_kbp" | cut -c3- | tr '\\' '/')"
  printf '%s' "$_kbw"
}

kb_progress() { # -> "kb <flushed>/<size>" | "kb <size> synced" | nothing at all
  _tp=$(printf '%s' "$INPUT" | grep -o '"transcript_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
  _tp=$(kb_posixpath "$_tp")
  # No transcript in the event, or a path this shell cannot open (a deleted
  # file): we know nothing, so we say nothing.
  [ -n "$_tp" ] && [ -r "$_tp" ] || return 0
  _size=$(stat -c%s "$_tp" 2>/dev/null || stat -f%z "$_tp" 2>/dev/null || wc -c <"$_tp" 2>/dev/null | tr -d ' ')
  case "$_size" in '' | *[!0-9]*) return 0 ;; esac
  [ "$_size" -gt 0 ] || return 0

  _state="$HOME/.claude/hooks/state"
  # THE KEY IS DERIVED EXACTLY AS flush-session-to-kb.sh DERIVES IT (`OKEY`, the
  # MCP/REST branch) — same `tr -c` sanitisation, same 120-byte cap, same
  # ${OKEY:-default} fallback. Approximating it would read a file that does not
  # exist for any session id containing a character the hook rewrites, and the
  # feature would silently never fire. tests/test_capture_funnel.py generates the
  # key both ways and compares.
  _okey=$(printf '%s' "${SID:-$_tp}" | tr -c 'A-Za-z0-9._-' '_' | tail -c 120)
  _off="$_state/${_okey:-default}.offset"

  # BRANCH 1 — the checkpoint file exists: the network transports (MCP/REST) own
  # it and advance it after each accepted chunk, so it is exactly "bytes of this
  # transcript the KB has taken". Report progress against the current size.
  if [ -e "$_off" ]; then
    _flushed=$(cat "$_off" 2>/dev/null || true)
    # A corrupt (non-numeric, or unreadable) checkpoint is NOT reported as zero:
    # the hook itself treats it as "restart from 0", but printing "0/5.2MB" would
    # claim nothing is captured when the truth is that we cannot tell. Omit.
    case "$_flushed" in '' | *[!0-9]*) return 0 ;; esac
    # A transcript that shrank (rotated/replaced) can leave the checkpoint above
    # the size; the hook restarts from 0 next firing. "6.0/5.2MB" is nonsense, so
    # clamp rather than print it.
    [ "$_flushed" -gt "$_size" ] && _flushed="$_size"
    _h=$(kb_human "$_flushed" "$_size") || return 0
    [ -n "$_h" ] || return 0
    printf 'kb %s' "$_h"
    return 0
  fi

  # BRANCH 2 — no checkpoint file. The flush hook DELETED it after a terminal
  # SessionEnd drain (fully captured), or nothing has ever flushed. The backfill
  # ledger is the durable record that survives the deletion: it holds
  # "<session key><TAB><bytes accounted for>" for every session a sweep
  # finished.
  _led="$_state/.backfill-done"
  if [ -f "$_led" ]; then
    # Split on TAB *or* US (0x1F): the ledger is written TAB-separated, and the
    # US is accepted so a record written with the sweep's other field separator
    # still parses. Built with printf so no awk has to interpret an escape.
    _fs=$(printf '[\t\037]')
    _acct=$(awk -F"$_fs" -v k="$_okey" '$1 == k { v = $2 } END { print v }' "$_led" 2>/dev/null)
    case "$_acct" in
    '' | *[!0-9]*) ;;
    *)
      if [ "$_acct" -ge "$_size" ]; then
        _h=$(kb_human "$_size") || return 0
        [ -n "$_h" ] || return 0
        printf 'kb %s synced' "$_h"
      else
        # Accounted for less than the file now holds (it grew after the sweep)
        # — say so in the same progress shape rather than calling it synced.
        _h=$(kb_human "$_acct" "$_size") || return 0
        [ -n "$_h" ] || return 0
        printf 'kb %s' "$_h"
      fi
      return 0
      ;;
    esac
  fi

  # BRANCH 3 — NO record at all. Whether that is "zero" or "unknowable" depends
  # on the box's flush TRANSPORT (owner report 2026-08-07: young sessions on the
  # Windows box pushed pings with no kb segment at all, which reads as broken):
  #   * NETWORK mode (KB_REST_URL / KB_MCP_URL / connector): every accepted
  #     chunk advances an offset and every finished sweep writes the ledger, so
  #     no record genuinely means NOTHING of this transcript is captured yet —
  #     say so: "kb 0.0/5.2MB" is the honest zero, not a guess (the flush fires
  #     at PreCompact/SessionEnd, so a young session is EXPECTED to read 0).
  #   * LOCAL mode (KB_DIR) enqueues through the CLI and writes neither record —
  #     truly unknowable from here, so keep the original omission.
  # A corrupt offset (branch 1) still omits: there the truth is "cannot tell",
  # not zero. Mode is read from the flush hook's own config in a SUBSHELL so
  # none of its variables (tokens included) can leak into this environment.
  _mode=$( [ -r "$HOME/.claude/kb-flush.env" ] && (
    . "$HOME/.claude/kb-flush.env" 2>/dev/null
    if [ -n "${KB_REST_URL:-}" ] || [ -n "${KB_MCP_URL:-}" ] || [ "${KB_CONNECTOR:-0}" = "1" ]; then
      printf net
    fi
  ) 2>/dev/null ) || _mode=""
  if [ "$_mode" = "net" ]; then
    _h=$(kb_human 0 "$_size") || return 0
    [ -n "$_h" ] || return 0
    printf 'kb %s' "$_h"
  fi
}

# Belt and braces around the whole computation: a stat that errors, an awk that
# is missing, a $HOME that is not writable — none of it may cost the ping.
KBPART=$(kb_progress 2>/dev/null || true)

# "(3m20s, kb 4.1/5.2MB)", "(3m20s)", "(kb 4.1/5.2MB)" or nothing — the
# parenthetical carries whatever is actually known.
DETAIL="$TOOK"
if [ -n "$KBPART" ]; then
  if [ -n "$DETAIL" ]; then DETAIL="$DETAIL, $KBPART"; else DETAIL="$KBPART"; fi
fi
[ -n "$DETAIL" ] && DETAIL=" ($DETAIL)"

# WHICH MACHINE. With this hook shipped to every box (linux, windows, rm1) the
# project alone is ambiguous — the same repo is worked on from more than one.
MACHINE="${NTFY_MACHINE:-$(hostname 2>/dev/null || echo unknown)}"
# THE BODY IS PURE ASCII — DELIBERATELY (KB 12885). On Windows the MSYS->native
# argv conversion runs through the ANSI codepage, which rewrites an em dash into
# the single CP-1252 byte 0x97. That byte is invalid UTF-8, so ntfy classifies
# the whole body as a binary attachment and, with attachments disabled, rejects
# the push (400, code 40014) — silently, because this hook exits 0 by contract.
# This was patched locally on the Windows box four times and reintroduced by
# every release; tests now assert every published body is ASCII, so keep it so.
MSG="Claude finished - ${WHERE} @ ${MACHINE}${DETAIL}"

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

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
  # else the PURE-BASH C:\x -> /c/x fallback (no cut/tr: MSYS rewrites a native
  # tr's lone `/` argument, KB 40411; ${x,} is bash 4+, reached only on a drive
  # path). Non-drive paths pass through untouched, so a Linux path containing a
  # backslash is never corrupted.
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
  if [ -z "$_kbw" ]; then
    _kbw=${_kbp//\\//}
    _kbw=${_kbw,}
    _kbw="/${_kbw:0:1}${_kbw:2}"
  fi
  printf '%s' "$_kbw"
}

kb_mtime() { # <path> -> epoch mtime, or nothing
  _kbm=$(stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || true)
  case "$_kbm" in '' | *[!0-9]*) ;; *) printf '%s' "$_kbm" ;; esac
}

kb_age() { # <epoch> -> "12m" | "3h" | "2d" (ASCII; nothing when unparsable)
  case "${1:-}" in '' | *[!0-9]*) return 0 ;; esac
  _kbn=$(date +%s 2>/dev/null || echo 0)
  case "$_kbn" in '' | *[!0-9]*) return 0 ;; esac
  _kbd=$((_kbn - $1))
  [ "$_kbd" -ge 0 ] || _kbd=0
  if [ "$_kbd" -lt 3600 ]; then printf '%sm' "$((_kbd / 60))"
  elif [ "$_kbd" -lt 172800 ]; then printf '%sh' "$((_kbd / 3600))"
  else printf '%sd' "$((_kbd / 86400))"; fi
}

# A per-session marker the flush hook keeps BESIDE the checkpoint, spelled
# `<okey>.<ext>` (proposal_v2 Appendix I: <okey>.retry, <okey>.unseeded -- only
# the lock keeps the .offset infix). ONE spelling, the one both hooks write;
# tests/hookrig/cases/39 plants exactly these names.
kb_marker() { # <state> <okey> <ext> -> the marker path when it exists
  [ -e "$1/$2.$3" ] && printf '%s' "$1/$2.$3"
  return 0
}

# The sweep's health, appended ONLY when unhealthy (ASCII, KB 12885): the ping
# is the one per-turn channel on a box that lives in resumed sessions and never
# sees a SessionStart brief. Healthy = silent.
#   sweep:never       no sweep has ever completed although one is expected
#                     (the systemd timer is rendered, or a trigger fired long ago)
#   sweep:26h stale   .sweep-last older than 3 x KB_SWEEP_INTERVAL (timer dead?)
#   sweep:STALLED 1   transcripts the sweep gave up retrying for now
#   sweep:unknown 6   unseeded transcripts awaiting an owner --seed decision
# KB_SWEEP_INTERVAL=0 is the documented OFF switch (hybrid_v1 s2.8): no cadence
# means no "stale" and no "never" -- there is nothing to be late against.
kb_sweep_note() { # <state> -> " sweep:<...>" | nothing
  _swi="${KB_SWEEP_INTERVAL:-1800}"
  case "$_swi" in '' | *[!0-9]*) _swi=1800 ;; esac
  _swf="$1/.sweep-last"
  if [ ! -f "$_swf" ]; then
    # `never` needs BOTH: a rendered timer unit (a sweep is expected on this
    # box) AND no recent ask -- a trigger younger than 3 x the interval means a
    # sweep was requested and may still be running; older, or absent although
    # the timer's ExecStartPre would have touched it, means nothing fires.
    [ "$_swi" -gt 0 ] || return 0
    [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/kb-flush-sweep.timer" ] || return 0
    if [ -e "$1/.sweep.trigger" ]; then
      _swt=$(kb_mtime "$1/.sweep.trigger")
      _swn=$(date +%s 2>/dev/null || echo 0)
      case "$_swt$_swn" in
        *[!0-9]* | '') return 0 ;;
        *) [ "$((_swn - _swt))" -gt "$((3 * _swi))" ] || return 0 ;;
      esac
    fi
    printf ' sweep:never'
    return 0
  fi
  _swts=$(sed -n 's/^ts=\([0-9]*\).*/\1/p' "$_swf" 2>/dev/null | head -1)
  [ -n "$_swts" ] || _swts=$(kb_mtime "$_swf")
  _swn=$(date +%s 2>/dev/null || echo 0)
  case "$_swts$_swn" in
    *[!0-9]* | '') ;;
    *)
      if [ "$_swi" -gt 0 ] && [ "$((_swn - _swts))" -gt "$((3 * _swi))" ]; then
        printf ' sweep:%s stale' "$(kb_age "$_swts")"
        return 0
      fi
      ;;
  esac
  _swst=$(sed -n 's/.* stalled=\([0-9]*\).*/\1/p' "$_swf" 2>/dev/null | head -1)
  _swun=$(sed -n 's/.* unknown=\([0-9]*\).*/\1/p' "$_swf" 2>/dev/null | head -1)
  if [ -n "$_swst" ] && [ "$_swst" -gt 0 ]; then printf ' sweep:STALLED %s' "$_swst"
  elif [ -n "$_swun" ] && [ "$_swun" -gt 0 ]; then printf ' sweep:unknown %s' "$_swun"; fi
}

kb_progress() { # -> "kb <flushed>/<size>[ retry#N]" | "kb <size> synced" | "kb ?/<size> unseeded" | nothing
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
  # transcript the KB has taken". Checkpoints are NEVER deleted by the hooks any
  # more (wave C): a drained session keeps its file at offset == size and reads
  # `synced`; a ` retry#N` suffix means the last chunk failed N times and the
  # Stop gate is backing off (the marker is removed once the offset catches up).
  if [ -e "$_off" ]; then
    _flushed=$(cat "$_off" 2>/dev/null || true)
    # A corrupt (non-numeric, or unreadable) checkpoint is NOT reported as zero:
    # printing "0/5.2MB" would claim nothing is captured when the truth is that
    # we cannot tell. The hook REFUSES such a session as unseeded (never a
    # from-0 resend, KB 33783) and leaves <okey>.unseeded beside it: say
    # `?/<size> unseeded` then, exactly as for a missing checkpoint; omit when
    # the hook has not yet ruled on it.
    case "$_flushed" in
      '' | *[!0-9]*)
        [ -n "$(kb_marker "$_state" "${_okey:-default}" unseeded)" ] || return 0
        _h=$(kb_human "$_size") || return 0
        [ -n "$_h" ] || return 0
        printf 'kb ?/%s unseeded' "$_h"
        return 0
        ;;
    esac
    _sfx=""
    _rt=$(kb_marker "$_state" "${_okey:-default}" retry)
    if [ -n "$_rt" ]; then
      # N = the marker's 2nd field. 0 (a lock-deferred chunk, nothing failed
      # yet) reads as a bare ` retry` -- `retry#0` would claim zero retries.
      _rn=$(awk 'NR == 1 { print $2 }' "$_rt" 2>/dev/null)
      case "$_rn" in '' | *[!0-9]* | 0) _sfx=" retry" ;; *) _sfx=" retry#$_rn" ;; esac
    fi
    # offset >= size is "everything the file holds has been accepted". A
    # checkpoint ABOVE the size (rotated/replaced transcript; the hook logs
    # `reason=shrank` and restarts) reads the same way rather than "6.0/5.2MB".
    if [ "$_flushed" -ge "$_size" ]; then
      _h=$(kb_human "$_size") || return 0
      [ -n "$_h" ] || return 0
      printf 'kb %s synced%s' "$_h" "$_sfx"
      return 0
    fi
    _h=$(kb_human "$_flushed" "$_size") || return 0
    [ -n "$_h" ] || return 0
    printf 'kb %s%s' "$_h" "$_sfx"
    return 0
  fi

  # BRANCH 1b — no checkpoint, but the hook has REFUSED to start one: the
  # transcript predates the box's `.checkpoints-trusted-since` stamp, so a
  # from-0 send would resend a session an older hook already captured. Nothing
  # of it is known to be in the KB and nothing will be until the owner seeds
  # it (`kb-flush-backfill.sh --seed <sid> current|<bytes>`). Say exactly that.
  if [ -n "$(kb_marker "$_state" "${_okey:-default}" unseeded)" ]; then
    _h=$(kb_human "$_size") || return 0
    [ -n "$_h" ] || return 0
    printf 'kb ?/%s unseeded' "$_h"
    return 0
  fi

  # BRANCH 2 — no checkpoint file and no refusal. Hooks from before wave C
  # DELETED the checkpoint after a terminal SessionEnd drain and the backfill
  # ledger was the durable record that survived it: "<session key><TAB><bytes
  # accounted for>" per session a sweep finished. The ledger is INERT now (no
  # longer written) but is still honoured as a read-only relic, so a session
  # drained under the old hooks keeps reading `synced` instead of a false zero.
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
# The sweep's health rides on the kb segment (never on its own: a LOCAL box has
# no sweep to report on) and only when there is something to act on.
if [ -n "$KBPART" ]; then
  KBPART="$KBPART$(kb_sweep_note "$HOME/.claude/hooks/state" 2>/dev/null || true)"
fi

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

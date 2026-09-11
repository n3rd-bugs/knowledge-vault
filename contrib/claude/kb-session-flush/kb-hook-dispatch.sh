#!/usr/bin/env bash
# ONE hook per Claude Code event: read the event JSON ONCE, then hand the SAME
# bytes to every script that event needs.
#
# WHY THIS EXISTS. Claude Code on Windows has an OPEN upstream bug (GH #77078 and
# siblings): when MORE THAN ONE hook is registered on a single event, only one of
# the spawned processes receives the event JSON on stdin -- the others read an
# empty stream. There is no env-var fallback carrying the event fields, and no
# fixed version to wait for, so a hook that lost the race simply has nothing to
# work with.
#
# That is not cosmetic for this skill. We used to register TWO hooks on
# UserPromptSubmit (turn-start.sh + kb-session-brief.sh) and one or more on Stop,
# so on Windows the per-prompt recall regularly came up with no prompt and no
# cwd -- it printed nothing, exactly like a healthy empty KB -- and captures died
# the same silent way. The documented workaround is what this file is: register a
# SINGLE hook per event, let it read stdin once, and fan the bytes out itself.
#
# CONTRACT
#   * usage: kb-hook-dispatch.sh <event>
#   * children are resolved beside THIS script, run SEQUENTIALLY, in map order;
#   * every child gets byte-identical stdin AND the event name as $1 (stdin can
#     arrive empty -- see below -- but our argv always carries the event);
#   * every child runs under a PER-CHILD timeout, so one hung child cannot eat
#     the hook slot for the children behind it;
#   * a child that fails, times out, or is missing, never stops a later one;
#   * an unknown event runs nothing;
#   * ALWAYS exits 0 -- a dispatcher that failed a session would be worse than
#     the bug it works around;
#   * no jq, no python3, no backgrounding (the flush hook detaches its own
#     child), and no non-ASCII byte in anything it can print (KB 12885: the
#     Windows ANSI-codepage argv conversion mangles wide characters).
set -u

EV="${1:-}"

# Native Windows path (C:\x or C:/x) -> POSIX. A trimmed copy of the portable
# block's kb_winpath: this script deliberately carries NO JSON backend (it never
# parses the event, it only forwards it), so it must not pull in the block.
# $0 can arrive as a native path, which dirname would mangle into the drive path.
kb_winpath() {
  case "${1:-}" in [A-Za-z]:\\* | [A-Za-z]:/*) ;; *)
    printf '%s' "${1:-}"
    return 0
    ;;
  esac
  _kbp=""
  if command -v cygpath >/dev/null 2>&1; then _kbp=$(cygpath -u "$1" 2>/dev/null) || _kbp=""; fi
  if [ -z "$_kbp" ]; then
    _kbp="/$(printf '%s' "$1" | cut -c1 | tr '[:upper:]' '[:lower:]')$(printf '%s' "$1" | cut -c3- | tr '\\' '/')"
  fi
  printf '%s' "$_kbp"
}

# A TRUSTWORTHY timeout, trimmed from the portable block for the same reason as
# kb_winpath above (this script deliberately does not carry the whole block).
# A binary merely NAMED `timeout` is not enough: on Git Bash `command -v
# timeout` resolves to C:\Windows\System32\timeout.exe -- a WAIT utility that
# cannot run a command and errors on redirected stdin -- so trusting the name
# would make every per-child bound below a silent no-op. Trust a candidate only
# if it behaves like GNU/BSD timeout: a coreutils --version, or a self-test that
# really bounds a command with stdin redirected. Else gtimeout, else a perl
# watchdog (Git Bash ships perl), else run unbounded rather than mis-invoke.
_kb_timeout_ok() {
  [ -n "${1:-}" ] || return 1
  "$1" --version 2>/dev/null | grep -qi coreutils && return 0
  "$1" 1 true </dev/null >/dev/null 2>&1
}
KB_TIMEOUT_BIN=""
KB_TIMEOUT_PROBED=0
kb_timeout() { # <seconds> cmd...
  if [ "$KB_TIMEOUT_PROBED" = 0 ]; then
    KB_TIMEOUT_PROBED=1
    for _kbt in "$(command -v timeout 2>/dev/null || true)" "$(command -v gtimeout 2>/dev/null || true)"; do
      [ -n "$_kbt" ] || continue
      if _kb_timeout_ok "$_kbt"; then
        KB_TIMEOUT_BIN="$_kbt"
        break
      fi
    done
  fi
  _kbd="$1"
  shift
  if [ -n "$KB_TIMEOUT_BIN" ]; then
    "$KB_TIMEOUT_BIN" "$_kbd" "$@"
  elif command -v perl >/dev/null 2>&1; then
    perl -e '
      my $t = shift @ARGV;
      my $pid = fork();
      if (!defined $pid) { exit 127; }   # fork failed: bail, do NOT exec unbounded
      if ($pid == 0)     { exec @ARGV or exit 127; }
      my $to = 0;
      local $SIG{ALRM} = sub { $to = 1; kill "TERM", $pid; };
      alarm $t;
      waitpid($pid, 0);
      my $rc = $?;
      alarm 0;
      exit 124 if $to;
      exit(($rc & 127) ? 128 + ($rc & 127) : ($rc >> 8));
    ' "$_kbd" "$@"
  else
    "$@"
  fi
}

HOOKDIR="$(cd "$(dirname "$(kb_winpath "$0")")" 2>/dev/null && pwd)"
[ -n "$HOOKDIR" ] || HOOKDIR="$HOME/.claude/hooks"

# The map, with a PER-CHILD TIMEOUT in seconds after each name. The children run
# SEQUENTIALLY, so before this bound one wedged child owned the WHOLE hook slot:
# a hung turn-start.sh spent the entire UserPromptSubmit budget and the recall
# behind it never ran at all. Each child is killed (TERM, rc 124) at its budget
# and the NEXT child still runs.
#
# The budgets are set against install.sh's registered per-event timeouts
# (PreCompact 15 / SessionEnd 15 / SessionStart 35 / UserPromptSubmit 40 /
# Stop 15) so the dispatcher finishes INSIDE its slot instead of being killed by
# it: turn-start 5 + brief 34 = 39 < 40; brief 34 < 35; on Stop the flush gate
# 3 + notify 11 = 14 < 15 (the slot is NOT widened: 15 stays 15 on every box).
# The flush is the deliberate exception on PreCompact/SessionEnd: its hook
# detaches its real work in well under a second, so 300 is a runaway net for a
# pathological pre-detach hang (a dead mount, a wedged uv) -- it exceeds the
# 15 s slot on purpose. On Stop (wave C, C5) the flush runs FIRST under a 3 s
# budget: a closed gate is two stats and a few cats, an open one only pins the
# project and detaches, and the 45 s token auto-mint moved into the detached
# child (never on Stop/SessionEnd), so 3 s is generous. Flush before notify so
# the `kb <flushed>/<size>` ping reads the freshest offset.
#
# ORDER MATTERS on UserPromptSubmit: turn-start.sh is a ~5 ms stamp and
# must not sit behind the recall's budget, and the recall's stdout is the
# last thing written, which is what Claude Code injects.
case "$EV" in
  PreCompact | SessionEnd) CHILDREN="flush-session-to-kb.sh:300" ;;
  SessionStart) CHILDREN="kb-session-brief.sh:34" ;;
  UserPromptSubmit) CHILDREN="turn-start.sh:5 kb-session-brief.sh:34" ;;
  Stop) CHILDREN="flush-session-to-kb.sh:3 notify-complete.sh:11" ;;
  *)
    # Unknown (or missing) event: run nothing. Still drain stdin so the writer
    # upstream never blocks or takes a SIGPIPE on our account.
    cat >/dev/null 2>&1 || true
    exit 0
    ;;
esac

# READ ONCE. Everything below forwards this same value, so the children cannot
# race each other for the stream -- which is the whole point of the file.
INPUT=$(cat 2>/dev/null || true)

# A dispatcher that arrives with nothing is the residual upstream bug reaching us
# anyway (or Claude Code firing a hook with no payload). Make it COUNTABLE: one
# line in the log the pre-brief already surfaces.
#
# The wording is deliberately none of FAILED / BUG: / NOT CONFIGURED /
# NOT CAPTURED / NEEDS TOKEN -- those are the tokens the pre-brief's dropped-
# session scanner greps for, and an empty event is not a lost transcript. The
# children still run: each degrades gracefully on its own (a stdin-less notify
# still pushes a basic ping), so silence here would be strictly worse.
if [ -z "$INPUT" ]; then
  _kbl="$HOME/.claude/hooks"
  mkdir -p "$_kbl" 2>/dev/null || true
  printf '%s dispatch hook event arrived EMPTY: event=%s - children still invoked\n' \
    "$(date -Is 2>/dev/null || LC_ALL=C date)" "$EV" >>"$_kbl/flush-session.log" 2>/dev/null || true
fi

# Sequential fan-out. `bash <script>` rather than executing it directly: on Git
# Bash the exec bit is not always meaningful, and we are already a bash process
# so $BASH is the interpreter we know exists.
#
# THE EVENT NAME TRAVELS AS $1. The dispatcher is the only process that reliably
# knows it -- it arrives on OUR argv from the settings.json registration, so an
# event whose stdin came up EMPTY (the residual upstream bug logged above) still
# carries it -- while a child re-deriving it from the stdin JSON reads nothing at
# all in exactly that case. Children that do not need it ignore extra argv.
#
# A failing AND a timed-out (rc 124) child alike never stop a later sibling or
# the exit code. A timed-out child IS written down, though -- one line per
# (child, event) per hour, in the log the pre-brief and the sweep read: on Stop
# the flush hook stamps <okey>.gate BEFORE it detaches, so a parent killed here
# leaves a .gate newer than .last, the gate backs off one interval, and this
# line is the only trace of WHY. Wording avoids the pre-brief's dropped-session
# tokens (FAILED / BUG: / NOT CONFIGURED / NOT CAPTURED / NEEDS TOKEN): a killed
# gate is a deferred drain, not a lost transcript. ASCII only (KB 12885). The
# rate limit lives in a BOX-level stamp, state/.dispatch-124-<child>-<event>.stamp
# (one epoch per child/event, beside .notoken-*.stamp / .notconfigured.stamp):
# never a per-session file, so the hook's state sweep must never retire it.
for _kbc in $CHILDREN; do
  _kbn="${_kbc%%:*}"
  _kbb="${_kbc##*:}"
  _kbs="$HOOKDIR/$_kbn"
  [ -f "$_kbs" ] || continue
  _kbr=0
  printf '%s' "$INPUT" | kb_timeout "$_kbb" "${BASH:-bash}" "$_kbs" "$EV" || _kbr=$?
  if [ "$_kbr" = 124 ]; then
    _kbl="$HOME/.claude/hooks"
    _kbst="$_kbl/state/.dispatch-124-${_kbn%.sh}-$EV.stamp"
    mkdir -p "$_kbl/state" 2>/dev/null || true
    _kbnow=$(date +%s 2>/dev/null || echo 0)
    case "$_kbnow" in '' | *[!0-9]*) _kbnow=0 ;; esac
    _kblast=$(cat "$_kbst" 2>/dev/null || echo 0)
    case "$_kblast" in '' | *[!0-9]*) _kblast=0 ;; esac
    if [ "$((_kbnow - _kblast))" -ge 3600 ]; then
      printf '%s\n' "$_kbnow" >"$_kbst" 2>/dev/null || true
      printf '%s dispatch %s hit its %ss budget on %s (rc 124) - killed, later children still ran\n' \
        "$(date -Is 2>/dev/null || LC_ALL=C date)" "$_kbn" "$_kbb" "$EV" >>"$_kbl/flush-session.log" 2>/dev/null || true
    fi
  fi
done

exit 0

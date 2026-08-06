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
#   * every child gets byte-identical stdin, and its stdout/stderr pass through
#     UNTOUCHED (the brief's stdout IS the injected context);
#   * a child that fails, or is missing, never stops a later one;
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

HOOKDIR="$(cd "$(dirname "$(kb_winpath "$0")")" 2>/dev/null && pwd)"
[ -n "$HOOKDIR" ] || HOOKDIR="$HOME/.claude/hooks"

# The map. ORDER MATTERS on UserPromptSubmit: turn-start.sh is a ~5 ms stamp and
# must not sit behind the recall's 35 s budget, and the recall's stdout is the
# last thing written, which is what Claude Code injects.
case "$EV" in
  PreCompact | SessionEnd) CHILDREN="flush-session-to-kb.sh" ;;
  SessionStart) CHILDREN="kb-session-brief.sh" ;;
  UserPromptSubmit) CHILDREN="turn-start.sh kb-session-brief.sh" ;;
  Stop) CHILDREN="notify-complete.sh" ;;
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
for _kbc in $CHILDREN; do
  _kbs="$HOOKDIR/$_kbc"
  [ -f "$_kbs" ] || continue
  printf '%s' "$INPUT" | "${BASH:-bash}" "$_kbs" || true
done

exit 0

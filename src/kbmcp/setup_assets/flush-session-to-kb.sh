#!/usr/bin/env bash
# Claude Code PreCompact/SessionEnd hook: flush this session's NEW transcript
# delta into the Knowledge Vault before compaction. Large sessions supported:
# deltas are sent as sequential <=900KB line-boundary chunks (up to 1 GB total),
# with the offset advancing per accepted chunk (mid-delta resume on failure). Detaches immediately so it
# never blocks/stalls compaction. Config lives in ~/.claude/kb-flush.env:
#   LOCAL  (KB host): KB_DIR=/path/to/kb-repo   [UV=/path/to/uv]
#   MCP    (client) : KB_MCP_URL=https://host/mcp   KB_TOKEN=...  (reuses the
#                     `claude mcp` connector registration — zero extra config)
#   REMOTE (client) : KB_REST_URL=https://host[:port]   KB_TOKEN=...
# Installed by the `kb-session-flush` skill. See that skill's README for setup.
set -uo pipefail

[ -n "${KB_HOOK_GUARD:-}" ] && exit 0 # we were spawned by a KB hook — never recurse
CFG="$HOME/.claude/kb-flush.env"
[ -f "$CFG" ] || exit 0
# shellcheck disable=SC1090
. "$CFG"

# >>> KB PORTABLE BLOCK v1 — BYTE-IDENTICAL in install.sh, flush-session-to-kb.sh,
# kb-session-brief.sh and kb-mcp-call.sh. tests/hookrig/cases/26 extracts it from
# all four and diffs, so never hand-edit one copy. It exists because a locked-down
# box (no sudo, no jq) used to be a dead end: install.sh aborted with "apt install
# jq" and both hooks exited 0 in SILENCE, capturing nothing forever.
# Provides: platform detection, native-path conversion, a TRUSTWORTHY timeout, a
# detach command, and a JSON backend chain -- jq on PATH, a jq co-located with the
# hooks, $KB_JQ, then python3 -- plus the loud, sudo-free failure when none exist.
KB_UNAME=$(uname -s 2>/dev/null || echo unknown)
kb_is_windows() { case "$KB_UNAME" in MINGW* | MSYS* | CYGWIN*) return 0 ;; *) return 1 ;; esac; }

# Native Windows path (C:\x or C:/x) -> POSIX. cygpath when it exists, else a
# pure-shell fallback (C:\Users\First Last\p -> /c/Users/First Last/p) so a Git
# Bash without cygpath still resolves event paths. Anything else passes through.
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

# POSIX path -> native, for values Claude Code hands to CreateProcess (the hook
# command on Windows). cygpath -w when present; else derive from Git Bash's
# EXEPATH; else the bare exe name, which Windows resolves through PATH.
kb_nativepath() {
  _kbn=""
  if command -v cygpath >/dev/null 2>&1; then _kbn=$(cygpath -w "$1" 2>/dev/null) || _kbn=""; fi
  if [ -z "$_kbn" ] && [ -n "${EXEPATH:-}" ]; then
    case "$1" in */bash) _kbn="${EXEPATH%\\}\\usr\\bin\\bash.exe" ;; esac
  fi
  [ -n "$_kbn" ] || _kbn="$(basename "$1").exe"
  printf '%s' "$_kbn"
}

kb_detach_cmd() { # setsid is absent on Git Bash -- nohup is the detach fallback
  if command -v setsid >/dev/null 2>&1; then printf 'setsid'; else printf 'nohup'; fi
}

# A binary merely NAMED `timeout` is not enough: on Git Bash `command -v timeout`
# resolves to C:\Windows\System32\timeout.exe -- a WAIT utility that cannot run a
# command and errors on redirected stdin ("Input redirection is not supported").
# Using it would make every bounded call a silent no-op. Trust a candidate only if
# it behaves like GNU/BSD timeout: a coreutils --version, or a self-test that
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

# ---- JSON backend: jq on PATH -> jq beside the hooks -> $KB_JQ -> python3 ----
# KB_JQ_DIR1/KB_JQ_DIR2 are the co-location dirs the caller sets before probing
# (two separate vars, not a PATH-style list: a Windows path contains a colon).
KB_JQ_BIN=""
KB_PY_BIN=""
KB_JSON_PROBED=0
_kb_jq_ok() { [ -n "${1:-}" ] && [ -x "$1" ] && printf '{"a":1}' | "$1" -e . >/dev/null 2>&1; }
kb_json_probe() {
  if [ "$KB_JSON_PROBED" = 0 ]; then
    KB_JSON_PROBED=1
    for _kbj in "$(command -v jq 2>/dev/null || true)" "$(command -v jq.exe 2>/dev/null || true)" \
      "${KB_JQ_DIR1:-}/jq" "${KB_JQ_DIR1:-}/jq.exe" "${KB_JQ_DIR2:-}/jq" "${KB_JQ_DIR2:-}/jq.exe" \
      "${KB_JQ:-}"; do
      if _kb_jq_ok "$_kbj"; then
        KB_JQ_BIN="$_kbj"
        break
      fi
    done
    if [ -z "$KB_JQ_BIN" ]; then
      for _kbp in python3 python; do
        if command -v "$_kbp" >/dev/null 2>&1 && "$_kbp" -c 'import json' >/dev/null 2>&1; then
          KB_PY_BIN="$_kbp"
          break
        fi
      done
    fi
  fi
  [ -n "$KB_JQ_BIN" ] || [ -n "$KB_PY_BIN" ]
}

kb_json_get() { # <key>; JSON object on stdin -> that top-level value (or nothing)
  if [ -n "$KB_JQ_BIN" ]; then
    "$KB_JQ_BIN" -r --arg k "$1" '.[$k] // empty' 2>/dev/null
  else
    "$KB_PY_BIN" -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
v=d.get(sys.argv[1]) if isinstance(d,dict) else None
if v is None: sys.exit(0)
sys.stdout.write(v if isinstance(v,str) else json.dumps(v))' "$1" 2>/dev/null
  fi
}

kb_json_valid() { # <file> -- is it parseable JSON?
  if [ -n "$KB_JQ_BIN" ]; then
    "$KB_JQ_BIN" -e . "$1" >/dev/null 2>&1
  else
    "$KB_PY_BIN" -c 'import json,sys
json.load(open(sys.argv[1],encoding="utf-8"))' "$1" >/dev/null 2>&1
  fi
}

# Build a flat JSON object: kb_json_build <outfile|-> [KEY TYPE VALUE]...
# TYPE s=string  n=number/raw JSON  f=string read VERBATIM from a file (the
# chunk path: a ~900 KB payload must never travel as an argv).
kb_json_build() {
  _kbo="$1"
  shift
  if [ -n "$KB_JQ_BIN" ]; then
    _kba=()
    _kbf='{}'
    _kbi=0
    while [ "$#" -ge 3 ]; do
      _kbi=$((_kbi + 1))
      case "$2" in
        s) _kba+=(--arg "v$_kbi" "$3") ;;
        n) _kba+=(--argjson "v$_kbi" "$3") ;;
        f) _kba+=(--rawfile "v$_kbi" "$3") ;;
      esac
      _kbf="$_kbf + {\"$1\":\$v$_kbi}"
      shift 3
    done
    if [ "$_kbo" = "-" ]; then
      "$KB_JQ_BIN" -n "${_kba[@]}" "$_kbf"
    else
      "$KB_JQ_BIN" -n "${_kba[@]}" "$_kbf" >"$_kbo"
    fi
  else
    "$KB_PY_BIN" -c 'import json,sys
o=sys.argv[1]; a=sys.argv[2:]; d={}
for i in range(0,len(a)-2,3):
    k,t,v=a[i],a[i+1],a[i+2]
    if t=="s": d[k]=v
    elif t=="n": d[k]=json.loads(v)
    elif t=="f": d[k]=open(v,encoding="utf-8",errors="replace").read()
s=json.dumps(d,ensure_ascii=False)
sys.stdout.write(s+"\n") if o=="-" else open(o,"w",encoding="utf-8").write(s+"\n")' "$_kbo" "$@"
  fi
}

kb_urlencode() { # stdin -> percent-encoded (jq @uri / urllib.parse.quote agree)
  if [ -n "$KB_JQ_BIN" ]; then
    "$KB_JQ_BIN" -sRr @uri
  else
    "$KB_PY_BIN" -c 'import sys,urllib.parse
sys.stdout.write(urllib.parse.quote(sys.stdin.read(),safe=""))'
  fi
}

# The sudo-free remedy. NEVER says "apt install": the box that hits this has no
# root. jq is one static file, so every option below is a plain file copy.
kb_nojson_hint() {
  printf '%s\n' \
    "kb-session-flush: no JSON tool found -- the hooks need jq or python3, and this box has neither on PATH." \
    "Fix it WITHOUT sudo (jq is a single self-contained binary -- github.com/jqlang/jq/releases:" \
    "jq-linux-amd64, jq-macos-arm64, jq-windows-amd64.exe). Any ONE of:" \
    "  1. put it in ~/.local/bin/jq and chmod +x it (add ~/.local/bin to PATH)" \
    "  2. drop it next to the hooks: $HOME/.claude/hooks/jq   (jq.exe on Windows)" \
    "  3. point KB_JQ at it anywhere:   export KB_JQ=/path/to/jq" \
    "  4. or make any python3 reachable on PATH -- the hooks use it as a built-in fallback"
}

# A hook with no JSON backend cannot parse its event, but it must NOT die in
# silence (the bug class of rig case 12): one rate-limited line an hour into the
# flush log, phrased so the pre-brief's failure surfacing (which greps for
# NOT CONFIGURED) lifts it into the next session.
kb_nojson_warn() { # <tag: flush|brief|mcp-call>
  _kbh="$HOME/.claude/hooks"
  mkdir -p "$_kbh/state" 2>/dev/null || true
  _kbs="$_kbh/state/.nojson-${1:-hook}.stamp"
  _kbn=$(date +%s 2>/dev/null || echo 0)
  case "$_kbn" in '' | *[!0-9]*) _kbn=0 ;; esac
  _kbl=$(cat "$_kbs" 2>/dev/null || echo 0)
  case "$_kbl" in '' | *[!0-9]*) _kbl=0 ;; esac
  [ "$((_kbn - _kbl))" -ge 3600 ] || return 0
  printf '%s\n' "$_kbn" >"$_kbs" 2>/dev/null || true
  printf '%s %s NOT CONFIGURED: no JSON tool (jq or python3) on this box -- the KB hooks cannot parse Claude Code events, so this session was NOT captured. Fix without sudo: drop a static jq in ~/.local/bin or in %s, or set KB_JQ=/path/to/jq.\n' \
    "$(date -Is 2>/dev/null || date)" "${1:-hook}" "$_kbh" >>"$_kbh/flush-session.log" 2>/dev/null || true
}
# <<< END KB PORTABLE BLOCK v1 <<<

# Windows/Git Bash (KB item 496): a co-located jq (installer drops one next to
# the hooks) must resolve even when Claude Code inherited a PATH without it.
# $0 itself may arrive as a native path, so normalise it before dirname.
HOOKDIR="$(cd "$(dirname "$(kb_winpath "$0")")" 2>/dev/null && pwd)"
PATH="$HOOKDIR:$PATH"
KB_JQ_DIR1="$HOOKDIR"
KB_JQ_DIR2="$HOME/.claude/hooks"
# No JSON backend => we cannot parse the event at all. Do NOT exit in silence
# (that was the case-12 bug class: a box captured nothing, forever, unlogged);
# leave a rate-limited NOT CONFIGURED line the pre-brief will surface.
kb_json_probe || {
  kb_nojson_warn flush
  exit 0
}
DETACH="$(kb_detach_cmd)"

INPUT=$(cat)
T=$(printf '%s' "$INPUT" | kb_json_get transcript_path)
CWD=$(printf '%s' "$INPUT" | kb_json_get cwd)
SID=$(printf '%s' "$INPUT" | kb_json_get session_id)
EV=$(printf '%s' "$INPUT" | kb_json_get hook_event_name)
# Native Windows event paths (C:\... or C:/...) must be converted — basename on a
# backslash path yields the whole drive path (item 496). kb_winpath uses cygpath
# when present and a pure-shell C:\ -> /c/ fallback when it is not.
T=$(kb_winpath "$T")
CWD=$(kb_winpath "$CWD")
[ -n "$T" ] && [ -f "$T" ] || exit 0

# Project = the working repo's dir name, slugified (falls back to "sessions").
# THE GIT ROOT, not the cwd. A session started in a subdirectory (repeater/main,
# src/, docs/) used to file under that subdirectory's name — so the same repo
# produced different projects depending on where you happened to start, split its
# knowledge across them, and had no token for the accidental ones. The repo root
# is the stable identity; a non-git directory still falls back to the cwd.
PROJECT_DIR=$(git -C "${CWD:-.}" rev-parse --show-toplevel 2>/dev/null) || PROJECT_DIR=""
[ -n "$PROJECT_DIR" ] || PROJECT_DIR="${CWD:-sessions}"
PROJECT=$(basename "$PROJECT_DIR" | tr '[:upper:]' '[:lower:]' |
  tr -c 'a-z0-9-' '-' | sed 's/-\{2,\}/-/g;s/^-//;s/-$//')
[ -z "$PROJECT" ] && PROJECT=sessions
# Git context for code_ref anchoring: kb_ingest_session accepts repo/branch/
# commit and the drain stamps a code_ref from them. Local plumbing reads only
# (ms-cheap, no network); a non-git CWD degrades silently to empty. The network
# arg builders append them as extra kb_json_build triples — empty fields are
# OMITTED entirely (never sent as "").
REPO=$(git -C "${CWD:-.}" remote get-url origin 2>/dev/null)
[ -n "$REPO" ] || REPO=$(git -C "${CWD:-.}" rev-parse --show-toplevel 2>/dev/null)
REPO=${REPO%/}
REPO=${REPO%.git}
REPO=${REPO##*/}
REPO=${REPO##*:}
BRANCH=$(git -C "${CWD:-.}" rev-parse --abbrev-ref HEAD 2>/dev/null)
[ "$BRANCH" = "HEAD" ] && BRANCH="" # detached HEAD names no branch
COMMIT=$(git -C "${CWD:-.}" rev-parse --short HEAD 2>/dev/null)
LOG="$HOME/.claude/hooks/flush-session.log"
export T CWD SID PROJECT LOG REPO BRANCH COMMIT
# The detached child is a FRESH bash: hand it the JSON backend (which may be
# python3, with no jq anywhere on the box) as exported vars + functions.
export KB_JQ_BIN KB_PY_BIN KB_JSON_PROBED
export -f kb_json_build kb_json_valid
# kb_timeout must reach the detached child too: the CONNECTOR relay is the one
# network call in this script that was never bounded, and an unbounded call there
# cannot even report itself (every summary line is written AFTER the loop).
export KB_TIMEOUT_BIN KB_TIMEOUT_PROBED
export -f kb_timeout _kb_timeout_ok

# Only ONE drain may own a session's offset file at a time. PreCompact and
# SessionEnd both detach, and a large transcript keeps a child busy for minutes,
# so firings overlap routinely. Two children then read the same offset, relay the
# same chunk, and the slower one's late write can REWIND the offset and re-send
# bytes already accepted. flock where it exists; a mkdir lock (atomic everywhere,
# including Git Bash) otherwise. A lock older than an hour is assumed dead — a
# drain that long has already failed, and refusing forever would be worse.
kb_take_lock() { # <lockpath> -- 0 = we own it, 1 = someone else is draining
  _kbl="$1"
  if command -v flock >/dev/null 2>&1; then
    # BRACES MATTER. `exec 9>f 2>/dev/null` applies BOTH redirections to the
    # shell permanently, so the whole drain's stderr went to /dev/null for the
    # rest of the run — silencing exactly the diagnostics this file exists to
    # preserve. Scoping the 2>/dev/null to the group keeps fd 9 open and stderr
    # intact.
    { exec 9>"$_kbl"; } 2>/dev/null || return 0
    flock -n 9 2>/dev/null || return 1
    return 0
  fi
  if ! mkdir "$_kbl.d" 2>/dev/null; then
    _kba=$(find "$_kbl.d" -maxdepth 0 -mmin +60 2>/dev/null)
    [ -n "$_kba" ] || return 1
    rmdir "$_kbl.d" 2>/dev/null || true
    mkdir "$_kbl.d" 2>/dev/null || return 1
  fi
  trap 'rmdir "$_kbl.d" 2>/dev/null || true' EXIT INT TERM
  return 0
}
export -f kb_take_lock

# Opportunistic housekeeping — bound our OWN state/log growth so a daily-driver
# machine doesn't accumulate thousands of dead .offset files and an unrotated
# multi-MB log. Cheap (runs before the detach) and scoped strictly to the hooks'
# own dirs; never touches anything else.
STATE="$HOME/.claude/hooks/state"
mkdir -p "$STATE" 2>/dev/null || true
find "$STATE" -name '*.offset' -mtime +30 -delete 2>/dev/null || true
if [ -f "$LOG" ]; then
  LSIZE=$(stat -c%s "$LOG" 2>/dev/null || stat -f%z "$LOG" 2>/dev/null || wc -c <"$LOG" | tr -d " ")
  case "$LSIZE" in "" | *[!0-9]*) LSIZE=0 ;; esac
  if [ "$LSIZE" -gt 1048576 ]; then
    tail -n 500 "$LOG" >"$LOG.t" 2>/dev/null && mv "$LOG.t" "$LOG" 2>/dev/null || rm -f "$LOG.t" 2>/dev/null
  fi
fi

# ---------------------------------------------------------- per-project token
# A machine writing several projects should carry one NARROW, revocable
# credential per project rather than one broad credential for everything. Tokens
# live in ~/.claude/kb-tokens.env as KB_TOKEN_<PROJECT>, and the project is
# already known (slugified cwd), so the right one is selected here. KB_TOKEN
# stays the fallback, so a single-token machine keeps working unchanged.
TOKENS="$HOME/.claude/kb-tokens.env"
if [ -f "$TOKENS" ]; then
  # shellcheck disable=SC1090
  . "$TOKENS"
fi
# Fold the project slug into a legal shell variable name the same way
# kb_project_token does when it prints the line to add.
PROJ_KEY=$(printf '%s' "$PROJECT" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9' '_')
PROJ_TOKEN=$(eval "printf '%s' \"\${KB_TOKEN_${PROJ_KEY}:-}\"" 2>/dev/null || true)
if [ -n "$PROJ_TOKEN" ]; then
  KB_TOKEN="$PROJ_TOKEN"
  KB_TOKEN_SOURCE="project token for $PROJECT"
else
  KB_TOKEN_SOURCE="shared KB_TOKEN"
fi

# A machine that has per-project tokens but none for THIS project is not
# misconfigured in general — it just has not been given access to this one yet.
# Say exactly that, and exactly how to fix it, rather than failing later with an
# opaque 403. Rate-limited like the other NOT CONFIGURED lines.
if [ -z "${KB_TOKEN:-}" ] && [ -f "$TOKENS" ]; then
  STAMP="$HOME/.claude/hooks/state/.notoken-$PROJECT.stamp"
  mkdir -p "$HOME/.claude/hooks/state" 2>/dev/null || true
  NOW=$(date +%s 2>/dev/null || echo 0)
  LAST=$(cat "$STAMP" 2>/dev/null || echo 0)
  case "$LAST" in ''|*[!0-9]*) LAST=0 ;; esac
  if [ "$((NOW - LAST))" -ge 3600 ]; then
    printf '%s\n' "$NOW" >"$STAMP" 2>/dev/null || true
    echo "$(date -Is) flush $PROJECT NEEDS TOKEN: no KB_TOKEN_$PROJ_KEY in $TOKENS and no shared KB_TOKEN, so this session was NOT captured. Ask the KB for one: kb_project_token(project=\"$PROJECT\", machine=\"$(hostname 2>/dev/null || echo this-machine)\") — then add the printed line to $TOKENS" >>"$LOG"
  fi
  # Stop here rather than falling through to the generic NOT CONFIGURED branch.
  # Both lines are true, but the pre-brief shows the LAST failure, and "you need
  # a token for this project, run this to get one" is the one the user can act
  # on — letting the generic line land after it buries the useful message.
  # LOCAL mode needs no token, so it is never short-circuited here.
  [ -z "${KB_DIR:-}" ] && exit 0
fi

if [ -n "${KB_DIR:-}" ]; then
  # LOCAL: trusted CLI enqueue — checkpointed, GPU-free (nightly drain extracts).
  UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
  export KB_DIR UV
  "$DETACH" bash -c '
    echo "$(date -Is) local $PROJECT $T"
    cd "$KB_DIR" 2>/dev/null || { echo "$(date -Is) local $PROJECT BUG: KB_DIR unreachable ($KB_DIR)"; exit 0; }
    set --   # git-context flags for code_ref — empty values omitted entirely
    [ -n "$REPO" ] && set -- "$@" --repo "$REPO"
    [ -n "$BRANCH" ] && set -- "$@" --branch "$BRANCH"
    [ -n "$COMMIT" ] && set -- "$@" --commit "$COMMIT"
    if "$UV" run kb ingest-session "$T" --project "$PROJECT" --queue "$@"; then
      # Log the same fully-drained success marker the network paths emit, so a
      # prior stale FAILED self-clears in the pre-brief warn logic instead of
      # resurfacing as noise for ~40 firings (LOCAL never chunks — a successful
      # enqueue is always a full drain, hence offset SZ/SZ).
      SZ=$(stat -c%s "$T" 2>/dev/null || stat -f%z "$T" 2>/dev/null || wc -c <"$T" | tr -d " ")
      case "$SZ" in ""|*[!0-9]*) SZ=0 ;; esac
      echo "$(date -Is) local $PROJECT ok bytes=$SZ (offset $SZ/$SZ)"
    else
      echo "$(date -Is) local $PROJECT FAILED: kb ingest-session errored"
    fi
  ' </dev/null >>"$LOG" 2>&1 &
elif { [ -n "${KB_MCP_URL:-}" ] || [ -n "${KB_REST_URL:-}" ]; } && [ -n "${KB_TOKEN:-}" ]; then
  # MCP or REMOTE: submit the delta over the network, CHUNKED. Large sessions
  # (up to 1 GB) are sent as sequential ≤900 KB windows cut at line boundaries;
  # the per-session byte offset advances after EACH accepted chunk, so a failure
  # resumes mid-delta next firing. Chunks go via a temp file (kb_json_build's `f`
  # type reads it verbatim) — passing the text as an argv would hit the kernel's
  # ~128 KB per-argument limit long before 1 MB.
  STATE="$HOME/.claude/hooks/state"
  mkdir -p "$STATE"
  OKEY=$(printf '%s' "${SID:-$T}" | tr -c 'A-Za-z0-9._-' '_' | tail -c 120)
  OFFF="$STATE/${OKEY:-default}.offset"
  HELPER="$HOOKDIR/kb-mcp-call.sh"
  # Overridable for the same reason KB_CONNECTOR_MAX is: the budget is a policy
  # choice, not a constant of nature, and a cap that cannot be set small cannot
  # be tested at all (a 200 MB fixture per run is not a test, it is a wait).
  export KB_FLUSH_CHUNK="${KB_FLUSH_CHUNK:-900000}"
  export KB_FLUSH_MAX="${KB_FLUSH_MAX:-209715200}"
  export KB_MCP_URL="${KB_MCP_URL:-}" KB_REST_URL="${KB_REST_URL:-}" KB_TOKEN OFFF HELPER EV
  "$DETACH" bash -c '
    CHUNK="$KB_FLUSH_CHUNK"; MAX_PER_FIRING="$KB_FLUSH_MAX"   # rest resumes next firing
    [ "$EV" = "SessionEnd" ] && MAX_PER_FIRING=1099511627776   # terminal firing: drain all
    _fsize() { stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || wc -c <"$1" | tr -d " "; }
    if ! kb_take_lock "$OFFF.lock"; then
      [ "$EV" = "SessionEnd" ] && echo "$(date -Is) flush $PROJECT NOT CAPTURED: the final firing could not take the session lock (another drain was still running) — re-ingest $T if this session matters"
      exit 0
    fi
    OFFSET=$(cat "$OFFF" 2>/dev/null || echo 0)
    case "$OFFSET" in ""|*[!0-9]*) OFFSET=0 ;; esac   # empty/corrupt offset -> restart
    SIZE=$(_fsize "$T"); case "$SIZE" in ""|*[!0-9]*) SIZE=0 ;; esac
    # A transcript SMALLER than the stored offset was rotated, truncated or
    # replaced (or the offset was corrupted upward). Without this branch the
    # `-le` below exits 0 forever and the session is never captured again —
    # silently, with no log line, until the 30-day sweep removes the offset.
    if [ "$SIZE" -lt "$OFFSET" ]; then
      echo "$(date -Is) flush $PROJECT transcript shrank ($SIZE < offset $OFFSET) — restarting from 0"
      OFFSET=0
      printf "%s" 0 >"$OFFF.t" 2>/dev/null && mv "$OFFF.t" "$OFFF" 2>/dev/null || true
    fi
    [ "$SIZE" -le "$OFFSET" ] && exit 0
    SENT=0
    set --   # git context, appended to every request body; empty fields omitted
    [ -n "$REPO" ] && set -- "$@" repo s "$REPO"
    [ -n "$BRANCH" ] && set -- "$@" branch s "$BRANCH"
    [ -n "$COMMIT" ] && set -- "$@" commit s "$COMMIT"
    while [ "$OFFSET" -lt "$SIZE" ] && [ "$SENT" -lt "$MAX_PER_FIRING" ]; do
      TMPC=$(mktemp) || { echo "$(date -Is) flush $PROJECT BUG: mktemp failed"; break; }
      tail -c +$((OFFSET+1)) "$T" | head -c "$CHUNK" >"$TMPC"
      BYTES=$(_fsize "$TMPC")
      [ "$BYTES" -gt 0 ] || { rm -f "$TMPC"; break; }
      # ALWAYS cut on a line boundary: trim to the last newline unless the chunk
      # already ends in one. Robust to the transcript growing after SIZE was read
      # (an EOF-crossing window is still trimmed), so the offset never advances
      # into the middle of a JSONL line and no straddled message is lost. Only a
      # single line longer than CHUNK is sent unterminated (server tolerates it).
      if [ "$(tail -c1 "$TMPC")" != "" ]; then
        PART=$(tail -n1 "$TMPC" | wc -c)
        KEEP=$((BYTES - PART))
        if [ "$KEEP" -gt 0 ]; then
          head -c "$KEEP" "$TMPC" >"$TMPC.k" && mv "$TMPC.k" "$TMPC"
          BYTES=$KEEP
        elif [ "$BYTES" -lt "$CHUNK" ]; then
          # No newline anywhere AND smaller than a full chunk => a single line still
          # being written. HOLD it (break, no advance) so the COMPLETED line is picked
          # up next firing, instead of shipping a half-line the drain drops as malformed
          # JSON. EXCEPTION: the terminal SessionEnd firing at EOF has no next firing —
          # send the final line as-is rather than lose it silently (the drain still
          # drops it only if truly malformed, so sending never loses data).
          if [ "$EV" = "SessionEnd" ] && [ $((OFFSET + BYTES)) -ge "$SIZE" ]; then
            :
          else
            rm -f "$TMPC"; break
          fi
        fi
      fi
      if [ -n "$KB_MCP_URL" ]; then
        # args go via FILE (@) — a ~900KB argv would hit the kernel arg limit.
        # Chunks are gzip+base64 encoded when the tools exist (10x less wire);
        # plaintext fallback keeps minimal systems working.
        if command -v gzip >/dev/null 2>&1 && command -v base64 >/dev/null 2>&1 \
           && gzip -c "$TMPC" | base64 | tr -d "\n" >"$TMPC.gz64" && [ -s "$TMPC.gz64" ]; then
          kb_json_build "$TMPC.args" project s "$PROJECT" transcript_gz f "$TMPC.gz64" kind s session "$@"
          rm -f "$TMPC.gz64"
        else
          rm -f "$TMPC.gz64"
          kb_json_build "$TMPC.args" project s "$PROJECT" transcript f "$TMPC" kind s session "$@"
        fi
        if [ ! -s "$TMPC.args" ] || ! kb_json_valid "$TMPC.args"; then
          echo "$(date -Is) flush $PROJECT BUG: request body build failed (empty/invalid) at offset $OFFSET — NOT submitted, will retry"
          rm -f "$TMPC.args" "$TMPC"; break
        fi
        bash "$HELPER" kb_ingest_session "@$TMPC.args" >/dev/null 2>>"$LOG" && CODE=202 || CODE=err
        rm -f "$TMPC.args"
      else
        BODYF="$TMPC.body"
        if command -v gzip >/dev/null 2>&1 && command -v base64 >/dev/null 2>&1 \
           && gzip -c "$TMPC" | base64 | tr -d "\n" >"$TMPC.gz64" && [ -s "$TMPC.gz64" ]; then
          kb_json_build "$BODYF" project s "$PROJECT" kind s session transcript_gz f "$TMPC.gz64" "$@"
          rm -f "$TMPC.gz64"
        else
          rm -f "$TMPC.gz64"
          kb_json_build "$BODYF" project s "$PROJECT" kind s session transcript f "$TMPC" "$@"
        fi
        if [ ! -s "$BODYF" ] || ! kb_json_valid "$BODYF"; then
          echo "$(date -Is) flush $PROJECT BUG: request body build failed (empty/invalid) at offset $OFFSET — NOT submitted, will retry"
          rm -f "$BODYF" "$TMPC"; break
        fi
        CODE=$(curl -sS -o "$TMPC.resp" -w "%{http_code}" --max-time 30 \
             -X POST "$KB_REST_URL/api/ingest-session" \
             -H "Authorization: Bearer $KB_TOKEN" -H "Content-Type: application/json" \
             --data @"$BODYF")
        # A flaked response read (000) whose body still carries a queue id was
        # ACCEPTED by the server (KB item 497) — count it as success.
        case "$CODE" in 2*) : ;; *) grep -q "queue_id" "$TMPC.resp" 2>/dev/null && CODE=202 ;; esac
        rm -f "$BODYF" "$TMPC.resp"
      fi
      rm -f "$TMPC"
      case "$CODE" in
        2*|202) OFFSET=$((OFFSET + BYTES)); printf '%s' "$OFFSET" >"$OFFF.t" && mv "$OFFF.t" "$OFFF"; SENT=$((SENT + BYTES)) ;;
        *) echo "$(date -Is) flush $PROJECT chunk FAILED ($CODE) at offset $OFFSET — will resume"; break ;;
      esac
    done
    # Truthful summary: only a FULLY drained firing (OFFSET>=SIZE) logs the
    # " ok bytes=" success line the pre-brief matches. A firing that broke early
    # (chunk FAILED, held partial line, or per-firing cap) logs a "partial" line
    # that does NOT match that pattern — so a failed chunk followed by earlier
    # successes can no longer masquerade as success and strand the tail forever
    # on the terminal SessionEnd firing (HIGH-1).
    if [ "$SENT" -gt 0 ]; then
      if [ "$OFFSET" -ge "$SIZE" ]; then
        echo "$(date -Is) flush $PROJECT ok bytes=$SENT (offset $OFFSET/$SIZE)"
      else
        echo "$(date -Is) flush $PROJECT partial bytes=$SENT (offset $OFFSET/$SIZE) — will resume"
      fi
    fi
    # Terminal event, fully drained: drop this session'"'"'s offset file (else it is
    # dead weight forever). The 30-day sweep at hook start catches the rest.
    [ "$EV" = "SessionEnd" ] && [ "$OFFSET" -ge "$SIZE" ] && rm -f "$OFFF" 2>/dev/null
  ' </dev/null >>"$LOG" 2>&1 &
elif [ "${KB_CONNECTOR:-0}" = "1" ] && [ -n "${CLAUDE_BIN:-}" ]; then
  # CONNECTOR: zero-config — uses the claude.ai KB connector bridged into
  # Claude Code (cloud-held OAuth; nothing local). A headless `claude -p` call
  # relays each chunk into kb_ingest_session. The MODEL is the data path, so
  # chunks are small (~24 KB echo fidelity) and each flush costs Claude usage —
  # bearer modes (MCP/REST) are the efficient path; this is the no-setup one.
  STATE="$HOME/.claude/hooks/state"
  mkdir -p "$STATE"
  OKEY=$(printf '%s' "${SID:-$T}" | tr -c 'A-Za-z0-9._-' '_' | tail -c 120)
  OFFF="$STATE/${OKEY:-default}.offset"
  export CLAUDE_BIN OFFF EV
  export KB_CONNECTOR_CHUNK="${KB_CONNECTOR_CHUNK:-24000}"
  export KB_CONNECTOR_MAX="${KB_CONNECTOR_MAX:-240000}"
  # An ABSOLUTE ceiling per firing, including the terminal one. SessionEnd used
  # to lift the cap to 1 TB so the tail could never be stranded — correct for a
  # normal session, catastrophic for a large one: at 24 KB per model call a
  # 300 MB transcript is ~13,000 headless `claude -p` invocations, i.e. days of
  # background work silently spending the user'"'"'s Claude usage, detached and
  # untracked. CONNECTOR is the zero-config transport, not the bulk one.
  export KB_CONNECTOR_SESSION_MAX="${KB_CONNECTOR_SESSION_MAX:-2000000}"
  "$DETACH" bash -c '
    export KB_HOOK_GUARD=1   # children (claude -p) must not re-trigger KB hooks
    _fsize() { stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || wc -c <"$1" | tr -d " "; }
    # Losing the lock mid-session is fine (the other firing is draining). On the
    # TERMINAL firing it is not: there is no next firing, so whatever the winner
    # does not reach is lost — and staying silent lets its " ok bytes=" line be
    # read as a fully captured session.
    if ! kb_take_lock "$OFFF.lock"; then
      [ "$EV" = "SessionEnd" ] && echo "$(date -Is) connector $PROJECT NOT CAPTURED: the final firing could not take the session lock (another drain was still running), so any remaining bytes were not relayed — re-ingest $T if this session matters"
      exit 0
    fi
    OFFSET=$(cat "$OFFF" 2>/dev/null || echo 0)
    case "$OFFSET" in ""|*[!0-9]*) OFFSET=0 ;; esac
    SIZE=$(_fsize "$T"); case "$SIZE" in ""|*[!0-9]*) SIZE=0 ;; esac
    if [ "$SIZE" -lt "$OFFSET" ]; then
      echo "$(date -Is) connector $PROJECT transcript shrank ($SIZE < offset $OFFSET) — restarting from 0"
      OFFSET=0
      printf "%s" 0 >"$OFFF.t" 2>/dev/null && mv "$OFFF.t" "$OFFF" 2>/dev/null || true
    fi
    [ "$SIZE" -le "$OFFSET" ] && exit 0
    SENT=0
    # SessionEnd still gets a LARGER budget than a mid-session firing (it is the
    # last chance for this session), but no longer an unbounded one. In practice
    # the backlog gate below is what enforces the bound — it trims the delta to
    # at most KB_CONNECTOR_SESSION_MAX before the loop starts, so this CAP can
    # never bind. It is kept as defence in depth, not as the primary mechanism.
    CAP="$KB_CONNECTOR_MAX"; [ "$EV" = "SessionEnd" ] && CAP="$KB_CONNECTOR_SESSION_MAX"

    # BACKLOG GATE. If the undrained delta exceeds what this transport can carry,
    # skip forward and keep the most RECENT bytes rather than the oldest: on a
    # transcript growing faster than the budget, a head-first policy would relay
    # the session opening forever and never reach what just happened.
    # This runs BEFORE any relay, so the loud line lands immediately instead of
    # hours in, and it names the transcript path — nothing else in the connector
    # log does, and without it the dropped bytes cannot be re-ingested by hand.
    SKIPPED=0
    BACKLOG=$((SIZE - OFFSET))
    if [ "$BACKLOG" -gt "$KB_CONNECTOR_SESSION_MAX" ]; then
      NEWOFF=$((SIZE - KB_CONNECTOR_SESSION_MAX))
      # Realign forward to the next line start so the first relayed chunk is not
      # half a JSONL record.
      ADV=$(tail -c +$((NEWOFF+1)) "$T" | head -c "$KB_CONNECTOR_CHUNK" | head -1 | wc -c | tr -d " ")
      case "$ADV" in ""|*[!0-9]*) ADV=0 ;; esac
      NEWOFF=$((NEWOFF + ADV))
      [ "$NEWOFF" -gt "$SIZE" ] && NEWOFF="$SIZE"
      SKIPPED=$((NEWOFF - OFFSET))
      OFFSET="$NEWOFF"
      printf "%s" "$OFFSET" >"$OFFF.t" 2>/dev/null && mv "$OFFF.t" "$OFFF" 2>/dev/null || true
      echo "$(date -Is) connector $PROJECT NOT CAPTURED $SKIPPED bytes (kept the most recent $KB_CONNECTOR_SESSION_MAX of $SIZE) — CONNECTOR relays one model call per $KB_CONNECTOR_CHUNK bytes, so this delta would have cost $((BACKLOG / KB_CONNECTOR_CHUNK)) calls. Those bytes are GONE, not queued and not resumable. For full capture set KB_MCP_URL/KB_REST_URL + KB_TOKEN (or KB_DIR) in $CFG, then re-ingest $T"
    fi
    while [ "$OFFSET" -lt "$SIZE" ] && [ "$SENT" -lt "$CAP" ]; do
      TMPC=$(mktemp)
      tail -c +$((OFFSET+1)) "$T" | head -c "$KB_CONNECTOR_CHUNK" >"$TMPC"
      BYTES=$(stat -c%s "$TMPC" 2>/dev/null || wc -c <"$TMPC")
      [ "$BYTES" -gt 0 ] || { rm -f "$TMPC"; break; }
      if [ "$(tail -c1 "$TMPC")" != "" ]; then
        PART=$(tail -n1 "$TMPC" | wc -c); KEEP=$((BYTES - PART))
        if [ "$KEEP" -gt 0 ]; then
          head -c "$KEEP" "$TMPC" >"$TMPC.k" && mv "$TMPC.k" "$TMPC"; BYTES=$KEEP
        elif [ "$BYTES" -lt "$KB_CONNECTOR_CHUNK" ]; then
          # single still-being-written partial line: hold for the next firing rather
          # than ship a half-line — UNLESS this is the terminal SessionEnd firing at
          # EOF (no next firing), where we send the final line as-is to avoid losing it.
          if [ "$EV" = "SessionEnd" ] && [ $((OFFSET + BYTES)) -ge "$SIZE" ]; then
            :
          else
            rm -f "$TMPC"; break
          fi
        fi
      fi
      # BOUNDED: this was the only unbounded network call in the script, and the
      # one place where hanging is invisible — every summary line below is
      # written AFTER the loop, so a wedged relay reports nothing at all, ever.
      OUT=$(kb_timeout "${KB_CONNECTOR_TIMEOUT:-120}" "$CLAUDE_BIN" -p "The stdin content is a session transcript delta. Call the kb_ingest_session tool (mcp__claude_ai_Knowledge_Base__kb_ingest_session, or any tool named kb_ingest_session) with project='"'"'$PROJECT'"'"', kind='"'"'session'"'"', transcript = the stdin content VERBATIM. Reply ONLY with the queue id number, or the single word DUPLICATE if the tool reports it was already queued, or FAILED."             --allowedTools "mcp__claude_ai_Knowledge_Base__kb_ingest_session,mcp__kb__kb_ingest_session"             <"$TMPC" 2>>"$LOG" | tail -1)
      rm -f "$TMPC"
      # OUT is untrusted model output: a headless claude -p run whose stdin is the
      # raw transcript delta, so an adversarial tool-result/webpage seen during the
      # session can steer it. Classify on the raw $OUT, but LOG a sanitized copy —
      # the pre-brief later lifts FAILED lines verbatim into the next session
      # context, so an un-truncated line is a prompt-injection + context-bloat
      # vector (LOW-2). Strip control chars + backticks, cap at 200 chars.
      OUTLOG=$(printf "%s" "$OUT" | tr -d "[:cntrl:]\140" | cut -c1-200)
      case "$OUT" in
        # FAILURE IS CHECKED FIRST. This block used to sit above the failure
        # patterns and match any reply CONTAINING "duplicate", so a reply like
        # "FAILED: error while checking for a duplicate" advanced the offset over
        # bytes the server never stored — silent, permanent loss on the transport
        # where nothing can re-send them.
        *[Ff][Aa][Ii][Ll]*|*[Ee]rror*|*NOTOOL*|*unavailable*)
          echo "$(date -Is) connector $PROJECT chunk FAILED ($OUTLOG) at offset $OFFSET — will resume"; break ;;
        # The server dedupes on content hash and answers that the chunk was
        # already queued, which carries NO digits — so without this it fell to
        # the "no id" branch and broke WITHOUT advancing, re-sending the same
        # bytes forever after any replay (a crash, a zero-length offset file, an
        # overlapping firing). A duplicate means the bytes ARE in the queue.
        # ORDER is what makes this safe, not narrowness. This block used to sit
        # ABOVE the failure patterns and match any reply containing "duplicate",
        # so "FAILED: error while checking for a duplicate" advanced the offset
        # over bytes the server never stored. With failures matched first, that
        # reply can no longer reach here, and the phrasing can stay tolerant.
        # The model is asked for the single word DUPLICATE but may echo the
        # server phrase "duplicate - already queued" instead, and rejecting that
        # would reinstate the permanent stall this branch exists to prevent.
        # (No apostrophes in here: the whole drain is one single-quoted bash -c.)
        *[Dd][Uu][Pp][Ll][Ii][Cc][Aa][Tt][Ee]*|*already\ queued*)
          OFFSET=$((OFFSET + BYTES)); printf "%s" "$OFFSET" >"$OFFF.t" && mv "$OFFF.t" "$OFFF"; SENT=$((SENT + BYTES)) ;;
        *[0-9]*) OFFSET=$((OFFSET + BYTES)); printf '%s' "$OFFSET" >"$OFFF.t" && mv "$OFFF.t" "$OFFF"; SENT=$((SENT + BYTES)) ;;
        *) echo "$(date -Is) connector $PROJECT chunk FAILED (no id: $OUTLOG) at offset $OFFSET — will resume"; break ;;
      esac
    done
    # Truthful summary (HIGH-1): only a fully-drained firing logs " ok bytes=";
    # an early break (chunk FAILED / held partial line / per-firing cap) logs a
    # "partial" line the pre-brief does not read as success.
    # "will resume" is a promise, and on the TERMINAL firing there is nothing left
    # to keep it: no further firing exists for this session_id, so bytes still
    # undrained at SessionEnd are lost, not deferred. Saying "will resume" there
    # is the difference between a user re-ingesting the file and a user believing
    # the session was captured. Likewise the cap notice must name the cap that
    # was ACTUALLY in force, not KB_CONNECTOR_MAX regardless of event.
    if [ "$SENT" -gt 0 ] || [ "$SKIPPED" -gt 0 ]; then
      SKIPNOTE=""
      [ "$SKIPPED" -gt 0 ] && SKIPNOTE=" NOT CAPTURED $SKIPPED bytes"
      if [ "$OFFSET" -ge "$SIZE" ]; then
        echo "$(date -Is) connector $PROJECT ok bytes=$SENT (offset $OFFSET/$SIZE)$SKIPNOTE"
      elif [ "$EV" = "SessionEnd" ]; then
        echo "$(date -Is) connector $PROJECT NOT CAPTURED $((SIZE-OFFSET)) bytes (offset $OFFSET/$SIZE)$SKIPNOTE — SessionEnd is the FINAL firing for this session, so these bytes will NOT resume. Re-ingest $T, or switch to a bearer transport in $CFG"
      else
        echo "$(date -Is) connector $PROJECT partial bytes=$SENT (offset $OFFSET/$SIZE)$SKIPNOTE — will resume"
      fi
    fi
    [ "$OFFSET" -lt "$SIZE" ] && [ "$SENT" -ge "$CAP" ] && [ "$EV" != "SessionEnd" ] &&       echo "$(date -Is) connector $PROJECT per-firing cap reached ($CAP); $((SIZE-OFFSET)) bytes resume next firing"
    # Terminal event, fully drained: drop this session'"'"'s offset file.
    [ "$EV" = "SessionEnd" ] && [ "$OFFSET" -ge "$SIZE" ] && rm -f "$OFFF" 2>/dev/null
  ' </dev/null >>"$LOG" 2>&1 &
else
  # NO transport is configured (no KB_DIR, no KB_MCP_URL/KB_REST_URL+KB_TOKEN, no
  # CONNECTOR). Without this branch the hook fell off the end and exited 0 in
  # SILENCE: the machine captured nothing, forever, with no signal anywhere —
  # and because the SessionStart pre-brief surfaces failures by grepping THIS
  # log, its warning stayed silent too. That breaks the skill's own
  # never-silent-failure guarantee. Found by tests/hookrig case 12.
  # Rate-limited to one line an hour so a deliberately-unconfigured box does not
  # spam the log on every PreCompact + SessionEnd.
  STAMP="$STATE/.notconfigured.stamp"
  NOW=$(date +%s 2>/dev/null || echo 0)
  LAST=$(cat "$STAMP" 2>/dev/null || echo 0)
  case "$LAST" in '' | *[!0-9]*) LAST=0 ;; esac
  if [ "$((NOW - LAST))" -ge 3600 ]; then
    printf '%s\n' "$NOW" >"$STAMP" 2>/dev/null || true
    echo "$(date -Is) flush $PROJECT NOT CONFIGURED: no transport in $CFG (set KB_DIR, or KB_MCP_URL/KB_REST_URL + KB_TOKEN, or KB_CONNECTOR=1) — this session was NOT captured" >>"$LOG"
  fi
fi
exit 0

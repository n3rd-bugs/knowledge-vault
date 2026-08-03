#!/usr/bin/env bash
# kb-flush-backfill.sh -- owner-run sweep: push EVERY Claude Code transcript's
# UNFLUSHED remainder on this box through the REAL flush hook.
#
# WHY THIS EXISTS. The flush hook only ever sees the sessions that fire it.
# Everything else is knowledge sitting on disk that the KB has never been told
# about: sessions the hook REFUSED (8 of 22 flushes on 2026-07-31 -- sungrow,
# rm3, gateway, bss-backend -- because no project token existed yet), sessions
# that ran before the hooks were installed, and every transcript on a box that
# has only just been (re-)onboarded. Recovering the refused sungrow/bss
# transcripts was done BY HAND once, hand-firing the flush hook per transcript.
# This is that procedure, productised; a re-onboarded box (rm1, Windows) runs it
# immediately after install.sh.
#
# NOT A HOOK. install.sh drops it beside the hooks but registers NO event for it
# in settings.json: it is an owner-invoked recovery, never something a session
# triggers.
#
# WHAT IT DELIBERATELY DOES NOT DO. It reimplements nothing. Offsets, chunking,
# line-boundary cutting, per-project token selection (and auto-mint), scrubbing,
# the transport choice and every retry rule belong to flush-session-to-kb.sh.
# This script only decides WHICH transcripts still have bytes to send, and feeds
# the hook a SessionEnd event for each -- byte for byte the shape Claude Code
# feeds it. Nothing here ever opens a socket or prints a credential.
#
#   kb-flush-backfill.sh                       # DRY RUN (default): table + totals
#   kb-flush-backfill.sh --push                # flush every remainder, oldest first
#   kb-flush-backfill.sh --push --project bss-backend --limit 5
set -uo pipefail

# The anti-recursion guard belongs to the HOOKS; this script is invoked by a
# PERSON. Inheriting KB_HOOK_GUARD (the connector relay exports it into its
# `claude -p` child) would make every firing below exit 0 in silence -- the exact
# "captured nothing, said nothing" failure this tool exists to repair.
unset KB_HOOK_GUARD

# >>> KB PORTABLE BLOCK v1 — BYTE-IDENTICAL in install.sh, flush-session-to-kb.sh,
# kb-session-brief.sh, kb-mcp-call.sh and kb-flush-backfill.sh. tests/hookrig/
# cases/26 extracts it from all five and diffs, so never hand-edit one copy. It exists because a locked-down
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

# Same co-location rule as the hooks (KB item 496): $0 may arrive as a native
# Windows path, so normalise it before dirname, and let the JSON probe find a jq
# dropped beside this script or beside the hooks.
BF_SELF="$(cd "$(dirname "$(kb_winpath "$0")")" 2>/dev/null && pwd)"
HOOKS="$HOME/.claude/hooks"
PATH="$BF_SELF:$PATH"
KB_JQ_DIR1="$BF_SELF"
KB_JQ_DIR2="$HOOKS"

BF_PROJECTS="${KB_BACKFILL_PROJECTS:-$HOME/.claude/projects}"
BF_STATE="${KB_BACKFILL_STATE:-$HOOKS/state}"
BF_HOOK="${KB_BACKFILL_HOOK:-$HOOKS/flush-session-to-kb.sh}"
BF_LOG="${KB_BACKFILL_LOG:-$HOOKS/flush-session.log}"
BF_CFG="$HOME/.claude/kb-flush.env"
# Backfill-OWNED state, deliberately NOT an .offset file. The hook DELETES a
# session's offset once a terminal SessionEnd firing has fully drained it, and
# LOCAL mode never writes one at all -- so "no offset" cannot tell "never
# flushed" apart from "flushed, then cleaned up". Without this ledger a second
# sweep would re-push every transcript the first sweep had just finished. It
# records only "<session key><TAB><bytes accounted for>", and it never SHIFTS a
# starting point, so the REMAINING column always equals what the hook will send.
BF_LEDGER="$BF_STATE/.backfill-done"
# Ceiling on the wait for ONE transcript's detached drain to report itself; a
# 38 MB session at 900 KB a chunk is minutes of work. The normal path breaks out
# the moment the hook's summary line lands, so this only bounds a wedged run.
BF_TIMEOUT="${KB_BACKFILL_TIMEOUT:-600}"
# How long to wait for ANY evidence (a log line, or the offset moving) before
# concluding the hook did nothing at all. Needed because the hook's refusals are
# RATE-LIMITED to one line an hour: on an unconfigured box only the first
# transcript of a sweep gets a NOT CONFIGURED / NEEDS TOKEN line and the rest
# would otherwise each burn the full timeout waiting for a line that will never
# be written.
BF_IDLE="${KB_BACKFILL_IDLE:-60}"
# How far into a transcript to look for the session's own cwd record.
BF_SCAN="${KB_BACKFILL_SCAN:-50}"
TAB=$(printf '\t')
# The in-memory index is US-separated (0x1F), NOT tab-separated. Bash treats tab
# as IFS *whitespace*, so `IFS=<tab> read` COLLAPSES a run of tabs into one
# delimiter: an empty column shifts every column after it left. That silently ate
# the skip REASON -- the one field whose whole job is to explain why a transcript
# was not pushed. 0x1F is not IFS whitespace, so empty columns survive, and no
# path or project slug can contain it.
SEP=$(printf '\037')

PUSH=0
ONLY=""
LIMIT=0

usage() {
  cat <<'USAGE'
kb-flush-backfill.sh -- push every Claude Code transcript's unflushed remainder
into the Knowledge Vault, through the flush hook already installed on this box.

  (no flags)          DRY RUN: one row per transcript, plus totals. Sends nothing.
  --push              actually fire the flush hook, oldest transcript first
  --project <slug>    only transcripts that land in this project
  --limit <n>         push at most n transcripts this run (default: unlimited)
  --hook <path>       flush hook to fire   (default ~/.claude/hooks/flush-session-to-kb.sh)
  --projects <dir>    where transcripts live (default ~/.claude/projects)
  --timeout <secs>    per-transcript ceiling on the wait for the hook's summary
  -h, --help          this text

Re-running is safe. The hook owns the offsets and advances them as it accepts
bytes, so a transcript that has already been flushed has nothing left to send.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --push) PUSH=1 ;;
    --project)
      ONLY="${2:-}"
      shift
      ;;
    --project=*) ONLY="${1#*=}" ;;
    --limit)
      LIMIT="${2:-}"
      shift
      ;;
    --limit=*) LIMIT="${1#*=}" ;;
    --hook)
      BF_HOOK="${2:-}"
      shift
      ;;
    --hook=*) BF_HOOK="${1#*=}" ;;
    --projects)
      BF_PROJECTS="${2:-}"
      shift
      ;;
    --projects=*) BF_PROJECTS="${1#*=}" ;;
    --timeout)
      BF_TIMEOUT="${2:-}"
      shift
      ;;
    --timeout=*) BF_TIMEOUT="${1#*=}" ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      printf 'kb-flush-backfill: unknown argument %s\n\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done
case "$LIMIT" in '' | *[!0-9]*)
  printf 'kb-flush-backfill: --limit takes a number, got [%s]\n' "$LIMIT" >&2
  exit 2
  ;;
esac
case "$BF_TIMEOUT" in '' | *[!0-9]*) BF_TIMEOUT=600 ;; esac
case "$BF_IDLE" in '' | *[!0-9]*) BF_IDLE=60 ;; esac
case "$BF_SCAN" in '' | *[!0-9]*) BF_SCAN=50 ;; esac

# No JSON backend means we can neither read a transcript's own cwd nor build the
# event the hook parses. Say so the sudo-free way rather than guessing.
kb_json_probe || {
  kb_nojson_hint >&2
  exit 1
}

# ------------------------------------------------------------------- primitives
bf_size() { # <file> -> bytes (0 when unreadable)
  _bfs=$(stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || wc -c <"$1" 2>/dev/null | tr -d ' ')
  case "$_bfs" in '' | *[!0-9]*) _bfs=0 ;; esac
  printf '%s' "$_bfs"
}

bf_mtime() { # <file> -> unix mtime (0 when unreadable) -- the sweep order
  _bfm=$(stat -c%Y "$1" 2>/dev/null || stat -f%m "$1" 2>/dev/null || echo 0)
  case "$_bfm" in '' | *[!0-9]*) _bfm=0 ;; esac
  printf '%s' "$_bfm"
}

# THE SAME state the flush hook uses: $HOME/.claude/hooks/state/<key>.offset,
# where <key> is the session id sanitised exactly as the hook sanitises it. A
# missing or corrupt file reads as 0 -- which is also how the hook reads it, so
# the REMAINING column can never disagree with what the hook goes on to send.
bf_okey() { # <session id> -> the hook's offset-file key
  printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_' | tail -c 120
}

bf_offset() { # <okey> -> checkpointed bytes
  _bfo=$(cat "$BF_STATE/$1.offset" 2>/dev/null || echo 0)
  case "$_bfo" in '' | *[!0-9]*) _bfo=0 ;; esac
  printf '%s' "$_bfo"
}

bf_offset_exists() { # <okey> -> 0 when the hook's checkpoint file is present
  [ -e "$BF_STATE/$1.offset" ]
}

bf_ledger() { # <okey> -> bytes a previous sweep already accounted for
  [ -f "$BF_LEDGER" ] || {
    printf '0'
    return 0
  }
  _bfv=$(awk -F"$TAB" -v k="$1" '$1 == k { v = $2 } END { print (v == "" ? "0" : v) }' \
    "$BF_LEDGER" 2>/dev/null)
  case "$_bfv" in '' | *[!0-9]*) _bfv=0 ;; esac
  printf '%s' "$_bfv"
}

bf_ledger_record() { # <okey> <bytes>
  mkdir -p "$BF_STATE" 2>/dev/null || true
  if [ -f "$BF_LEDGER" ]; then
    awk -F"$TAB" -v k="$1" '$1 != k' "$BF_LEDGER" >"$BF_LEDGER.t" 2>/dev/null &&
      mv "$BF_LEDGER.t" "$BF_LEDGER" 2>/dev/null || rm -f "$BF_LEDGER.t" 2>/dev/null
  fi
  printf '%s\t%s\n' "$1" "$2" >>"$BF_LEDGER" 2>/dev/null || true
}

# THE CWD. The flush hook derives the project from the EVENT's cwd, so a backfill
# has to supply the same thing. Claude Code's own records carry a "cwd" field,
# which is the only authoritative source on disk -- take the first record that
# has one. jq when there is a jq; python3 otherwise (same chain as the hooks).
bf_cwd_of() { # <transcript> -> the session's recorded cwd, or nothing
  _bfc=""
  if [ -n "$KB_JQ_BIN" ]; then
    _bfc=$(head -n "$BF_SCAN" "$1" 2>/dev/null |
      "$KB_JQ_BIN" -r 'select(type == "object") | .cwd // empty' 2>/dev/null | head -n1)
  fi
  if [ -z "$_bfc" ]; then
    _bfy="${KB_PY_BIN:-$(command -v python3 2>/dev/null || true)}"
    [ -n "$_bfy" ] && _bfc=$(head -n "$BF_SCAN" "$1" 2>/dev/null | "$_bfy" -c 'import json,sys
for line in sys.stdin:
    try: rec = json.loads(line)
    except Exception: continue
    if isinstance(rec, dict) and isinstance(rec.get("cwd"), str) and rec["cwd"].strip():
        sys.stdout.write(rec["cwd"]); break' 2>/dev/null)
  fi
  [ -n "$_bfc" ] && kb_winpath "$_bfc"
}

# LAST RESORT ONLY, and deliberately weak. Claude Code names a transcript's
# directory by replacing every '/' of the cwd with '-', which is NOT reversible:
# a directory whose own name contains a dash ("data-server") decodes to a path
# that never existed ("/home/usama/data/server"), and two different cwds can
# collapse to the same slug. So the naive decode is accepted ONLY when it names a
# directory that actually exists on this box. Anything else is reported SKIPPED
# with its reason -- a transcript is never guessed into the wrong project,
# because filing one client's session under another's name is exactly the
# confidentiality failure the degenerate session identity already cost us.
bf_slug_cwd() { # <project directory name> -> an EXISTING directory, or nothing
  _bfd="/$(printf '%s' "${1#-}" | tr '-' '/')"
  [ -d "$_bfd" ] && printf '%s' "$_bfd"
}

# The slug the flush hook will derive from a cwd -- git root, then a .kbproject
# override, then the same slugify rules. DISPLAY AND --project FILTERING ONLY:
# the hook re-derives it authoritatively from the cwd we hand it, so a drift here
# can mislead a human but can never misfile knowledge.
bf_project_of() { # <cwd> -> project slug
  [ -n "${1:-}" ] || return 0
  _bfr=$(git -C "$1" rev-parse --show-toplevel 2>/dev/null) || _bfr=""
  [ -n "$_bfr" ] || _bfr="$1"
  _bfn=$(basename "$_bfr")
  if [ -r "$_bfr/.kbproject" ]; then
    _bfk=$(head -n1 "$_bfr/.kbproject" 2>/dev/null | tr -d '\r' | cut -c1-64)
    [ -n "$_bfk" ] && _bfn="$_bfk"
  fi
  _bfp=$(printf '%s' "$_bfn" | tr '[:upper:]' '[:lower:]' |
    tr -c 'a-z0-9-' '-' | sed 's/-\{2,\}/-/g;s/^-//;s/-$//')
  [ -n "$_bfp" ] || _bfp=sessions
  printf '%s' "$_bfp"
}

# ---------------------------------------------------------------------- preflight
if [ ! -d "$BF_PROJECTS" ]; then
  printf 'kb-flush-backfill: nothing to do -- %s does not exist (no Claude Code transcripts on this box)\n' \
    "$BF_PROJECTS"
  exit 0
fi
if [ "$PUSH" = 1 ]; then
  if [ ! -f "$BF_HOOK" ]; then
    printf 'kb-flush-backfill: no flush hook at %s -- run install.sh first (or pass --hook)\n' \
      "$BF_HOOK" >&2
    exit 2
  fi
  if [ ! -f "$BF_CFG" ]; then
    printf 'kb-flush-backfill: %s is missing, so the hook would exit 0 on every transcript and capture nothing. Run install.sh (or enrol this machine) first.\n' \
      "$BF_CFG" >&2
    exit 2
  fi
fi

BF_TMP=$(mktemp -d "${TMPDIR:-/tmp}/kb-backfill.XXXXXX") || {
  printf 'kb-flush-backfill: could not create a temp dir\n' >&2
  exit 1
}
trap 'rm -rf "$BF_TMP"' EXIT INT TERM
IDX="$BF_TMP/index"
: >"$IDX"

# ---------------------------------------------------------------------- discovery
shopt -s nullglob
for T in "$BF_PROJECTS"/*/*.jsonl; do
  [ -f "$T" ] || continue
  SID=$(basename "$T")
  SID="${SID%.jsonl}"
  [ -n "$SID" ] || continue
  OKEY=$(bf_okey "$SID")
  SIZE=$(bf_size "$T")
  OFF=$(bf_offset "$OKEY")
  ACCOUNTED=$(bf_ledger "$OKEY")

  STATUS=ready
  REASON=""
  CWD=$(bf_cwd_of "$T")
  if [ -z "$CWD" ]; then
    CWD=$(bf_slug_cwd "$(basename "$(dirname "$T")")")
    [ -n "$CWD" ] && REASON="cwd recovered from the ambiguous directory slug (it resolved to a real directory)"
  fi
  if [ -z "$CWD" ]; then
    STATUS=skipped
    REASON="no cwd in the first $BF_SCAN records and the directory slug names no existing directory"
  fi

  PROJ=$(bf_project_of "$CWD")
  [ -n "$PROJ" ] || PROJ="?"

  # What the hook will actually send. The ledger can only mark a transcript
  # FULLY accounted for; it never shifts the starting point, because the hook
  # would still begin from its own offset file.
  BASE="$OFF"
  [ "$ACCOUNTED" -ge "$SIZE" ] && [ "$ACCOUNTED" -gt 0 ] && BASE="$SIZE"
  # A transcript smaller than its checkpoint was rotated or truncated: the hook
  # restarts from 0, so the remainder is the whole file.
  [ "$SIZE" -lt "$BASE" ] && BASE=0
  if [ "$SIZE" -gt "$BASE" ]; then REMAIN=$((SIZE - BASE)); else REMAIN=0; fi
  if [ "$STATUS" = ready ] && [ "$REMAIN" -eq 0 ]; then
    STATUS=flushed
    REASON=""
  fi

  if [ -n "$ONLY" ] && [ "$PROJ" != "$ONLY" ]; then continue; fi
  printf '%s\037%s\037%s\037%s\037%s\037%s\037%s\037%s\037%s\037%s\037%s\n' \
    "$(bf_mtime "$T")" "$STATUS" "$PROJ" "$SID" "$SIZE" "$BASE" "$REMAIN" \
    "$OKEY" "$CWD" "$T" "$REASON" >>"$IDX"
done
shopt -u nullglob

if [ ! -s "$IDX" ]; then
  if [ -n "$ONLY" ]; then
    printf 'kb-flush-backfill: no transcripts under %s land in project %s\n' "$BF_PROJECTS" "$ONLY"
  else
    printf 'kb-flush-backfill: no transcripts under %s\n' "$BF_PROJECTS"
  fi
  exit 0
fi
sort -n "$IDX" >"$IDX.sorted" 2>/dev/null && mv "$IDX.sorted" "$IDX"

# -------------------------------------------------------------------- the table
printf '%-22s %-40s %12s %12s %12s  %s\n' PROJECT SESSION SIZE OFFSET REMAINING STATE
N_TOTAL=0
N_READY=0
N_FLUSHED=0
N_SKIPPED=0
B_REMAIN=0
while IFS="$SEP" read -r MT STATUS PROJ SID SIZE BASE REMAIN OKEY CWD T REASON; do
  N_TOTAL=$((N_TOTAL + 1))
  case "$STATUS" in
    ready)
      N_READY=$((N_READY + 1))
      B_REMAIN=$((B_REMAIN + REMAIN))
      ;;
    flushed) N_FLUSHED=$((N_FLUSHED + 1)) ;;
    *) N_SKIPPED=$((N_SKIPPED + 1)) ;;
  esac
  printf '%-22.22s %-40.40s %12s %12s %12s  %s\n' \
    "$PROJ" "$SID" "$SIZE" "$BASE" "$REMAIN" "$STATUS"
  [ -n "$REASON" ] && printf '%-22s %s\n' '' "-- $REASON"
done <"$IDX"

printf '\n%s transcript(s): %s with unflushed bytes (%s bytes), %s already flushed, %s SKIPPED\n' \
  "$N_TOTAL" "$N_READY" "$B_REMAIN" "$N_FLUSHED" "$N_SKIPPED"

if [ "$PUSH" != 1 ]; then
  printf 'DRY RUN -- nothing was sent. Re-run with --push to flush the %s remainder(s).\n' "$N_READY"
  [ "$LIMIT" -gt 0 ] && printf '(--limit %s would push only the %s oldest.)\n' "$LIMIT" "$LIMIT"
  exit 0
fi

# --------------------------------------------------------------------- the push
# One transcript at a time, oldest mtime first. Sequential is not a style choice:
# the hook DETACHES, so firing the whole sweep at once would put N drains on one
# box competing for per-session locks, for the same queue and (in CONNECTOR mode)
# for the user's Claude usage. Each firing is followed to its outcome before the
# next one starts.
bf_log_lines() {
  [ -f "$BF_LOG" ] || {
    printf '0'
    return 0
  }
  _bfl=$(wc -l <"$BF_LOG" 2>/dev/null | tr -d ' ')
  case "$_bfl" in '' | *[!0-9]*) _bfl=0 ;; esac
  printf '%s' "$_bfl"
}
bf_log_since() { # <line count taken before the firing> -> this firing's lines
  [ -f "$BF_LOG" ] || return 0
  tail -n +"$((${1:-0} + 1))" "$BF_LOG" 2>/dev/null
}

PUSHED=0
DEFERRED=0
FAILED=0
LIMITED=0
BYTES=0
printf '\n'
while IFS="$SEP" read -r MT STATUS PROJ SID SIZE BASE REMAIN OKEY CWD T REASON <&3; do
  [ "$STATUS" = ready ] || continue
  if [ "$LIMIT" -gt 0 ] && [ "$PUSHED" -ge "$LIMIT" ]; then
    LIMITED=$((LIMITED + 1))
    continue
  fi

  # THE EVENT, never hand-written. kb_json_build is the encoder these hooks
  # already own, so a cwd or session id carrying a quote, a backslash or a
  # newline reaches the hook exactly as Claude Code would have delivered it.
  EVF="$BF_TMP/event.json"
  kb_json_build "$EVF" transcript_path s "$T" cwd s "$CWD" session_id s "$SID" \
    hook_event_name s SessionEnd
  if [ ! -s "$EVF" ] || ! kb_json_valid "$EVF"; then
    printf '%-22.22s %-40.40s FAILED: could not build the hook event\n' "$PROJ" "$SID"
    FAILED=$((FAILED + 1))
    continue
  fi

  PRE=$(bf_log_lines)
  printf '%-22.22s %-40.40s %s bytes ... ' "$PROJ" "$SID" "$REMAIN"
  bash "$BF_HOOK" <"$EVF" >"$BF_TMP/hook.out" 2>&1
  HRC=$?

  # VERDICTS ARE KEYED TO THIS TRANSCRIPT, never to the shared log alone. The
  # hook detaches (exit code says nothing) and the flush log is written by EVERY
  # session on the box, so a bare " ok bytes=" match once scored a neighbour's
  # success as ours (adversarial verifier, reproduced) — while pure offset-watching
  # has the opposite blindspot: a terminal SessionEnd drain writes and then DELETES
  # the checkpoint, and if that happens between two polls nothing is ever observed
  # (rig case 27, 739s of idle timeouts). So three transcript-keyed signals, any
  # one sufficient:
  #   1. the OKEY offset reaches EOF (partial/live drains)
  #   2. the offset was seen moving and the file then vanished (terminal cleanup)
  #   3. the log gained "flush <THIS project> ... ok bytes= ... (offset <SIZE>/" —
  #      project AND total size must both match, which no concurrent flush of a
  #      different transcript can fake
  # Refusals are likewise matched WITH the project name. Idle timeout = deferred.
  VERDICT=""
  DETAIL=""
  I=0
  SAW_OFFSET=0
  while [ "$I" -lt "$((BF_TIMEOUT * 4))" ]; do
    CUR_OFF=$(bf_offset "$OKEY")
    if [ "$CUR_OFF" != "$BASE" ] && [ "$CUR_OFF" != 0 ]; then
      SAW_OFFSET=1
    fi
    if [ "$CUR_OFF" -ge "$SIZE" ] && [ "$CUR_OFF" -gt 0 ]; then
      VERDICT=pushed
      break
    fi
    if [ "$SAW_OFFSET" = 1 ] && ! bf_offset_exists "$OKEY"; then
      VERDICT=pushed # watched it advance, then vanish: terminal cleanup after a full drain
      break
    fi
    SEG=$(bf_log_since "$PRE")
    if printf '%s' "$SEG" | grep -q "flush $PROJ .*ok bytes=.*(offset $SIZE/"; then
      VERDICT=pushed # terminal drain logged for THIS project at THIS exact size
      break
    fi
    if printf '%s' "$SEG" | grep -q "flush $PROJ .*\(NEEDS TOKEN\|NOT CONFIGURED\|NOT CAPTURED\)"; then
      VERDICT=deferred
      DETAIL="the hook refused this one; see $BF_LOG"
      break
    fi
    if [ "$SAW_OFFSET" = 0 ] && [ "$I" -ge "$((BF_IDLE * 4))" ]; then
      VERDICT=deferred
      DETAIL="no progress for this transcript within ${BF_IDLE}s -- refusals are rate-limited to one log line an hour, so check $BF_LOG"
      break
    fi
    I=$((I + 1))
    sleep 0.25
  done
  if [ -z "$VERDICT" ]; then
    LAST=$(bf_offset "$OKEY")
    if [ "$LAST" -ge "$SIZE" ] && [ "$LAST" -gt 0 ]; then
      VERDICT=pushed
    elif [ "$LAST" != "$BASE" ]; then
      VERDICT=failed
      DETAIL="partial drain (offset $LAST of $SIZE) -- re-run to resume from the new offset"
    else
      VERDICT=failed
      DETAIL="no progress within ${BF_TIMEOUT}s (hook exit $HRC)"
    fi
  fi

  case "$VERDICT" in
    pushed)
      PUSHED=$((PUSHED + 1))
      BYTES=$((BYTES + REMAIN))
      bf_ledger_record "$OKEY" "$SIZE"
      printf 'ok\n'
      ;;
    deferred)
      DEFERRED=$((DEFERRED + 1))
      printf 'deferred (%s)\n' "$DETAIL"
      ;;
    *)
      FAILED=$((FAILED + 1))
      printf 'FAILED (%s)\n' "$DETAIL"
      ;;
  esac
done 3<"$IDX"

printf '\nkb-flush-backfill: pushed=%s deferred=%s failed=%s skipped=%s bytes=%s\n' \
  "$PUSHED" "$DEFERRED" "$FAILED" "$((N_SKIPPED + N_FLUSHED + LIMITED))" "$BYTES"
if [ "$LIMITED" -gt 0 ]; then
  printf '%s transcript(s) left for the next run by --limit %s.\n' "$LIMITED" "$LIMIT"
fi
[ "$FAILED" -eq 0 ]

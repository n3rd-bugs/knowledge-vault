#!/usr/bin/env bash
# Minimal MCP streamable-HTTP client for the KB hooks: initialize, then call one
# tool. Reads KB_MCP_URL + KB_TOKEN from the environment. Prints the tool's text
# result on success (exit 0); prints an error to stderr on failure (exit 1).
# Usage: kb-mcp-call.sh <tool_name> <arguments_json | @args_file>
# Pass @/path/to/file for large arguments — argv is capped at ~128 KB per arg
# by the kernel, so chunk-sized payloads MUST go via the file form.
# Needs jq OR python3 (see the portable block); the caller appends our stderr to
# flush-session.log, so a missing backend is loud rather than silent.
set -uo pipefail
TOOL="${1:?tool name}"
ARGS="${2:-}"
[ -n "$ARGS" ] || ARGS="{}"
ARGSFILE=""
case "$ARGS" in @*) ARGSFILE="${ARGS#@}" ;; esac
[ -n "${KB_MCP_URL:-}" ] && [ -n "${KB_TOKEN:-}" ] || {
  echo "KB_MCP_URL/KB_TOKEN unset" >&2
  exit 1
}

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

KB_JQ_DIR1="$(cd "$(dirname "$(kb_winpath "$0")")" 2>/dev/null && pwd)"
KB_JQ_DIR2="$HOME/.claude/hooks"
kb_json_probe || {
  kb_nojson_hint >&2
  kb_nojson_warn mcp-call
  exit 1
}

AUTH="Authorization: Bearer $KB_TOKEN"
CT="Content-Type: application/json"
AC="Accept: application/json, text/event-stream"
SID=$(curl -s -D - -o /dev/null --connect-timeout 3 --max-time 20 -X POST "$KB_MCP_URL" -H "$AUTH" -H "$CT" -H "$AC" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"kb-hook","version":"1"}}}' |
  awk 'tolower($1)=="mcp-session-id:"{print $2}' | tr -d '\r')
[ -n "$SID" ] || {
  echo "no mcp session (auth/url?)" >&2
  exit 1
}
curl -s -o /dev/null --connect-timeout 3 --max-time 20 -X POST "$KB_MCP_URL" -H "$AUTH" -H "$CT" -H "$AC" \
  -H "Mcp-Session-Id: $SID" -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'
# Build the request into a FILE (never argv) so chunk-sized payloads work.
REQF=$(mktemp)
if [ -n "$KB_JQ_BIN" ]; then
  if [ -n "$ARGSFILE" ]; then
    "$KB_JQ_BIN" -cn --arg n "$TOOL" --slurpfile a "$ARGSFILE" \
      '{jsonrpc:"2.0",id:2,method:"tools/call",params:{name:$n,arguments:$a[0]}}' >"$REQF"
  else
    "$KB_JQ_BIN" -cn --arg n "$TOOL" --argjson a "$ARGS" \
      '{jsonrpc:"2.0",id:2,method:"tools/call",params:{name:$n,arguments:$a}}' >"$REQF"
  fi
else
  "$KB_PY_BIN" - "$REQF" "$TOOL" "$ARGSFILE" "$ARGS" <<'PY'
import json, sys
out, tool, argsfile, argstr = sys.argv[1:5]
if argsfile:
    a = json.load(open(argsfile, encoding="utf-8"))
else:
    a = json.loads(argstr)
with open(out, "w", encoding="utf-8") as fh:
    json.dump({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
               "params": {"name": tool, "arguments": a}}, fh, ensure_ascii=False)
PY
fi
RESP=$(curl -s --connect-timeout 3 --max-time 60 -X POST "$KB_MCP_URL" -H "$AUTH" -H "$CT" -H "$AC" \
  -H "Mcp-Session-Id: $SID" --data @"$REQF")
rm -f "$REQF"
# CLOSE THE SESSION. Every chunk opens its own MCP session, and without this the
# server keeps each one indefinitely: a 100 MB transcript is ~117 chunks, so one
# flush leaked 117 server-side transports. Best-effort and never fatal — the
# submission already succeeded or failed above, and a failed cleanup must not
# change that verdict or the caller's offset decision.
[ -n "$SID" ] && curl -s -o /dev/null --connect-timeout 2 --max-time 5 \
  -X DELETE "$KB_MCP_URL" -H "$AUTH" -H "Mcp-Session-Id: $SID" 2>/dev/null || true
# isError:true is a tool rejection (queue full, bad project, oversize) surfaced
# as an HTTP-200 JSON-RPC result — it must FAIL so the caller does NOT advance
# the offset and lose the chunk.
if [ -n "$KB_JQ_BIN" ]; then
  if printf '%s' "$RESP" | "$KB_JQ_BIN" -e '.result and (.result.isError | not)' >/dev/null 2>&1; then
    printf '%s' "$RESP" | "$KB_JQ_BIN" -r '.result.content[]? | select(.type=="text") | .text'
    exit 0
  fi
  if printf '%s' "$RESP" | "$KB_JQ_BIN" -e '.result.isError == true' >/dev/null 2>&1; then
    echo "mcp tool rejected: $(printf '%s' "$RESP" | "$KB_JQ_BIN" -r '.result.content[]? | .text' 2>/dev/null | head -c 200)" >&2
    exit 1
  fi
  echo "mcp error: $(printf '%s' "$RESP" | "$KB_JQ_BIN" -r '.error.message // "unknown"' 2>/dev/null | head -c 200)" >&2
  exit 1
fi
printf '%s' "$RESP" | "$KB_PY_BIN" -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: d={}
r=d.get("result") if isinstance(d,dict) else None
if isinstance(r,dict) and not r.get("isError"):
    for c in (r.get("content") or []):
        if isinstance(c,dict) and c.get("type")=="text": sys.stdout.write((c.get("text") or "")+"\n")
    sys.exit(0)
if isinstance(r,dict) and r.get("isError") is True:
    t="".join((c.get("text") or "") for c in (r.get("content") or []) if isinstance(c,dict))
    sys.stderr.write("mcp tool rejected: "+t[:200]+"\n"); sys.exit(1)
e=(d.get("error") or {}).get("message") if isinstance(d,dict) else None
sys.stderr.write("mcp error: "+str(e or "unknown")[:200]+"\n"); sys.exit(1)'

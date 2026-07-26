#!/usr/bin/env bash
# Install the KB session-flush hook on THIS machine: writes the hook script,
# creates ~/.claude/kb-flush.env (LOCAL if a KB repo is here, else a REMOTE stub),
# and registers PreCompact + SessionEnd in ~/.claude/settings.json. Idempotent.
#
# NO SUDO REQUIRED. jq is preferred but optional: the installer and the hooks
# fall back to python3, and jq can be a static binary you drop in ~/.local/bin,
# next to the hooks, or point KB_JQ at. Only when NEITHER exists do we stop, and
# then loudly, with those instructions (never "apt install jq" -- a locked-down
# box has no root, so that was a dead end).
set -euo pipefail

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

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS="$HOME/.claude/hooks"
mkdir -p "$HOOKS"
HOOK="$HOOKS/flush-session-to-kb.sh"
CFG="$HOME/.claude/kb-flush.env"
SETTINGS="$HOME/.claude/settings.json"

# Co-location dirs the JSON probe searches after PATH: a jq the operator dropped
# beside the hooks (or one a previous install put there) and one shipped with the
# skill itself.
KB_JQ_DIR1="$HOOKS"
KB_JQ_DIR2="$SKILL_DIR"
kb_json_probe || {
  kb_nojson_hint >&2
  exit 1
}
if [ -n "$KB_JQ_BIN" ]; then JSON_MODE="jq ($KB_JQ_BIN)"; else JSON_MODE="python3 fallback ($KB_PY_BIN) -- no jq found"; fi

install -m 0755 "$SKILL_DIR/flush-session-to-kb.sh" "$HOOK"
BRIEF="$HOOKS/kb-session-brief.sh"
install -m 0755 "$SKILL_DIR/kb-session-brief.sh" "$BRIEF"
install -m 0755 "$SKILL_DIR/kb-mcp-call.sh" "$HOOKS/kb-mcp-call.sh"

# Co-locate the jq we just used NEXT TO THE HOOKS (originally a Windows fix, KB
# item 496: Claude Code may have inherited a PATH from before jq was installed).
# Extended to every platform for the no-sudo case: the operator drops ONE static
# jq anywhere we can find it and the installer pins it where the hooks always
# look. Keeps the source name, so jq.exe stays jq.exe on Windows.
if [ -n "$KB_JQ_BIN" ] && [ -f "$KB_JQ_BIN" ]; then
  JQ_DEST="$HOOKS/$(basename "$KB_JQ_BIN")"
  if [ "$KB_JQ_BIN" != "$JQ_DEST" ]; then
    cp -f "$KB_JQ_BIN" "$JQ_DEST" 2>/dev/null && chmod 0755 "$JQ_DEST" 2>/dev/null || true
  fi
fi

# Discover an existing KB MCP registration (url + bearer) from ~/.claude.json so
# a machine already connected to the KB via `claude mcp` needs zero extra config.
# Prefers a server named 'kb'; else the first http server whose url ends in /mcp
# and carries a bearer. Prints "url<TAB>token" or nothing.
_discover_mcp() {
  local cj="$HOME/.claude.json"
  [ -f "$cj" ] || return 0
  if [ -n "$KB_JQ_BIN" ]; then
    "$KB_JQ_BIN" -r '
      (.mcpServers // {}) | to_entries
      | map(select((.key | test("kb|knowledge"; "i"))
          and (.value | type == "object")
          and ((.value.url // "") | endswith("/mcp"))
          and ((.value.headers.Authorization // "") | test("Bearer"))))
      | (.[0].value // empty)
      | [.url, (.headers.Authorization | sub("^Bearer +"; ""))] | @tsv
    ' "$cj" 2>/dev/null
  else
    "$KB_PY_BIN" - "$cj" <<'PY' 2>/dev/null
import json, re, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(0)
for k, v in (d.get("mcpServers") or {}).items():
    if not isinstance(v, dict) or not re.search("kb|knowledge", k, re.I):
        continue
    url = v.get("url") or ""
    auth = (v.get("headers") or {}).get("Authorization") or ""
    if url.endswith("/mcp") and re.search("Bearer", auth):
        sys.stdout.write(url + "\t" + re.sub("^Bearer +", "", auth) + "\n")
        break
PY
  fi
}

# Locate a claude CLI for CONNECTOR mode (PATH, then common install spots,
# then the VS Code extension bundle).
_find_claude() {
  command -v claude 2>/dev/null && return 0
  for c in "$HOME/.local/bin/claude" "$HOME/.claude/local/claude"; do
    [ -x "$c" ] && {
      echo "$c"
      return 0
    }
  done
  ls -1t "$HOME"/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude \
    2>/dev/null | head -1
}

if [ ! -f "$CFG" ]; then
  # Create the config with owner-only perms BEFORE the bearer token is written.
  # Under the default umask 022 a plain '>' redirect would create $CFG as 0644,
  # leaving KB_TOKEN world-readable in the window before the chmod below — and
  # permanently 0644 if the installer is interrupted between write and chmod.
  # install(1) sets the mode atomically at creation; the '>' rewrites below keep
  # it (redirection to an existing file does not change its mode). (LOW-3)
  install -m 600 /dev/null "$CFG"
  KB_DIR_GUESS="${KB_DIR:-$HOME/data-server}"
  MCP_LINE="$(_discover_mcp || true)"
  CLAUDE_BIN_FOUND="$(_find_claude || true)"
  if [ -f "$KB_DIR_GUESS/pyproject.toml" ] && command -v uv >/dev/null 2>&1; then
    {
      echo "# KB session-flush (LOCAL)"
      echo "KB_DIR=$KB_DIR_GUESS"
      echo "UV=$(command -v uv)"
    } >"$CFG"
    MODE="LOCAL enqueue via $KB_DIR_GUESS"
  elif [ -n "$MCP_LINE" ]; then
    MURL=$(printf '%s' "$MCP_LINE" | cut -f1)
    MTOK=$(printf '%s' "$MCP_LINE" | cut -f2)
    {
      echo "# KB session hooks (MCP) — reuses your 'claude mcp' KB registration"
      echo "KB_MCP_URL=$MURL"
      echo "KB_TOKEN=$MTOK"
    } >"$CFG"
    MODE="MCP via $MURL (auto-discovered from your claude mcp registration)"
  elif [ -n "$CLAUDE_BIN_FOUND" ]; then
    # Zero-config: the claude.ai KB connector bridged into Claude Code carries
    # the auth (cloud-held OAuth). Hooks relay through headless `claude -p` —
    # small chunks (the model is the data path) and each flush costs Claude
    # usage; switch to MCP/REST modes for heavy sessions.
    {
      echo "# KB session hooks (CONNECTOR) — via the claude.ai KB connector, no token"
      echo "KB_CONNECTOR=1"
      echo "CLAUDE_BIN=$CLAUDE_BIN_FOUND"
      echo "# KB_CONNECTOR_CHUNK=24000   # bytes per relayed chunk"
      echo "# KB_CONNECTOR_MAX=240000    # bytes per hook firing"
    } >"$CFG"
    MODE="CONNECTOR via $CLAUDE_BIN_FOUND (claude.ai KB connector; zero config)"
  else
    {
      echo "# KB session-flush (REMOTE) — set KB_TOKEN and confirm the URL:"
      echo "KB_REST_URL=https://api.example.com"
      echo "KB_TOKEN="
    } >"$CFG"
    MODE="REMOTE — edit $CFG (set KB_TOKEN) then re-run"
  fi
  chmod 600 "$CFG"
else
  MODE="kept existing $CFG"
fi

[ -f "$SETTINGS" ] || echo '{}' >"$SETTINGS"
# Windows/Git Bash (KB item 496): Claude Code spawns hooks as exe+args, not via
# a shell, so a bare .sh path never runs — register bash.exe as the command with
# the script as its argument. kb_nativepath falls back to $EXEPATH, then to a
# bare "bash.exe" (resolved through the Windows PATH), when cygpath is absent.
BASHW=""
if kb_is_windows; then BASHW="$(kb_nativepath "$(command -v bash)")"; fi

# Append our entries (idempotent: any prior copy removed first) while preserving
# other hooks the user has. The brief hook self-filters resume/compact sources,
# so its matcher stays empty. The jq and python3 implementations below MUST
# produce byte-identical settings.json for the same input -- rig case 20 runs the
# installer once per backend and diffs the result.
tmp="$SETTINGS.kbtmp.$$"
if [ -n "$KB_JQ_BIN" ]; then
  if [ -n "$BASHW" ]; then
    entry=$("$KB_JQ_BIN" -n --arg b "$BASHW" --arg c "$HOOK" \
      '{matcher:"",hooks:[{type:"command",command:$b,args:[$c],timeout:15}]}')
    brief_entry=$("$KB_JQ_BIN" -n --arg b "$BASHW" --arg c "$BRIEF" \
      '{matcher:"startup|clear",hooks:[{type:"command",command:$b,args:[$c],timeout:30}]}')
    recall_entry=$("$KB_JQ_BIN" -n --arg b "$BASHW" --arg c "$BRIEF" \
      '{matcher:"",hooks:[{type:"command",command:$b,args:[$c],timeout:15}]}')
  else
    entry=$("$KB_JQ_BIN" -n --arg c "$HOOK" '{matcher:"",hooks:[{type:"command",command:$c,timeout:15}]}')
    brief_entry=$("$KB_JQ_BIN" -n --arg c "$BRIEF" '{matcher:"startup|clear",hooks:[{type:"command",command:$c,timeout:30}]}')
    recall_entry=$("$KB_JQ_BIN" -n --arg c "$BRIEF" '{matcher:"",hooks:[{type:"command",command:$c,timeout:15}]}')
  fi
  "$KB_JQ_BIN" --argjson e "$entry" --argjson b "$brief_entry" --argjson r "$recall_entry" \
    --arg c "$HOOK" --arg bc "$BRIEF" '
    def ours($p): (.hooks[0].command == $p) or ((.hooks[0].args[0]? // "") == $p);
    .hooks = (.hooks // {})
    | .hooks.PreCompact = (((.hooks.PreCompact // []) | map(select(ours($c) | not))) + [$e])
    | .hooks.SessionEnd = (((.hooks.SessionEnd // []) | map(select(ours($c) | not))) + [$e])
    | .hooks.SessionStart = (((.hooks.SessionStart // []) | map(select(ours($bc) | not))) + [$b])
    | .hooks.UserPromptSubmit = (((.hooks.UserPromptSubmit // []) | map(select(ours($bc) | not))) + [$r])
  ' "$SETTINGS" >"$tmp" && mv "$tmp" "$SETTINGS"
else
  "$KB_PY_BIN" - "$SETTINGS" "$tmp" "$HOOK" "$BRIEF" "$BASHW" <<'PY' && mv "$tmp" "$SETTINGS"
import json, sys

src, tmp, hook, brief, bashw = sys.argv[1:6]
try:
    data = json.load(open(src, encoding="utf-8"))
except Exception:
    data = {}
if not isinstance(data, dict):
    data = {}


def entry(script, matcher, timeout):
    """Same shape, same KEY ORDER as the jq branch (byte-identical output)."""
    inner = {"type": "command"}
    if bashw:
        inner["command"] = bashw
        inner["args"] = [script]
    else:
        inner["command"] = script
    inner["timeout"] = timeout
    return {"matcher": matcher, "hooks": [inner]}


def ours(item, script):
    if not isinstance(item, dict):
        return False
    hooks = item.get("hooks")
    h = hooks[0] if isinstance(hooks, list) and hooks and isinstance(hooks[0], dict) else {}
    if h.get("command") == script:
        return True
    args = h.get("args")
    first = args[0] if isinstance(args, list) and args else ""
    return first == script


hooks = data.get("hooks")
if not isinstance(hooks, dict):
    hooks = {}
data["hooks"] = hooks
for ev, script, matcher, timeout in (
    ("PreCompact", hook, "", 15),
    ("SessionEnd", hook, "", 15),
    ("SessionStart", brief, "startup|clear", 30),
    ("UserPromptSubmit", brief, "", 15),
):
    cur = hooks.get(ev)
    cur = cur if isinstance(cur, list) else []
    hooks[ev] = [x for x in cur if not ours(x, script)] + [entry(script, matcher, timeout)]

with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
PY
fi

echo "hook:     $HOOK"
echo "brief:    $BRIEF (SessionStart -> pre-brief; UserPromptSubmit -> per-prompt KB recall)"
echo "config:   $CFG  ($MODE)"
echo "json:     $JSON_MODE"
echo "settings: $SETTINGS  (PreCompact + SessionEnd + SessionStart + UserPromptSubmit registered)"
echo
echo "smoke test (feeds a fake PreCompact event to the hook):"
echo "  T=\$(ls -t \"\$HOME\"/.claude/projects/*/*.jsonl | head -1)"
echo "  printf '{\"transcript_path\":\"%s\",\"cwd\":\"%s\",\"session_id\":\"smoke\"}' \"\$T\" \"\$PWD\" | $HOOK"
echo "  sleep 2 && tail -3 $HOOKS/flush-session.log   # expect: local … | mcp … ok | remote … http=202"

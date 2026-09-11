#!/usr/bin/env bash
# KB-FLUSH-LINEAGE: never-delete-offsets v2
# Claude Code PreCompact/SessionEnd/Stop hook: flush this session's NEW
# transcript delta into the Knowledge Vault. Deltas are cut on an ABSOLUTE 700 KB
# line-boundary lattice (KB_FLUSH_CHUNK; never above KB_FLUSH_SEND_MAX), the
# per-session offset advances per accepted chunk and is NEVER deleted (a drained
# SessionEnd leaves offset==size; resumes continue from there); a transcript with
# no offset is sent from 0 only when it post-dates the box's checkpoint stamp
# (else refused as "unseeded"); the project is pinned ONCE per session from the
# transcript's first git cwd record. Stop fires every turn, so it passes a GATE
# first (size 4 MB / 5 min, age 30 min / 64 KB, or a due retry marker) and a
# closed gate costs nothing: no pin, no git, no token, no network, no log line.
# A failed REST chain leaves <okey>.retry (<epoch> <n> <code> <end>, end = the
# failed chunk's end byte, <= size) that the gate honours with an exponential
# back-off; the marker is REST-only (an MCP helper failure takes the plain
# "will resume" path). Detaches immediately so it never blocks compaction.
# Config lives in ~/.claude/kb-flush.env:
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
# KB_FLUSH_FOREGROUND=1 (the sweep's synchronous firing, hybrid_v1 s2.1) runs the
# drain in THIS session and waits for it: `env` is a no-op wrapper in place of
# setsid/nohup, and each `... &` block is followed by a conditional `wait`.
[ "${KB_FLUSH_FOREGROUND:-0}" = 1 ] && DETACH=env

# Only ONE drain may own a session's offset file at a time. PreCompact and
# SessionEnd both detach, and a large transcript keeps a child busy for minutes,
# so firings overlap routinely. Two children then read the same offset, relay the
# same chunk, and the slower one's late write can REWIND the offset and re-send
# bytes already accepted. The trio below is the whole lock discipline.
# >>> KB LOCK TRIO v1
# ONE session drains at a time. flock where it exists; a pid-owned mkdir dir
# otherwise (atomic everywhere, incl. Git Bash). This block is copied
# BYTE-IDENTICAL into kb-flush-backfill.sh (C7) and funnel-pinned, so it must be
# edited in ONE place. Reclaim a mkdir dir ONLY when its pid is DEAD (or absent)
# AND the dir has made no progress for KB_FLUSH_LOCK_STALE_MIN minutes — never on
# wall-clock age alone, so a healthy long drain (which touches the dir per chunk)
# is never stolen. The trap removes ONLY a dir this process owns.
# _kbl_mode/_kbl_f/_kbl_d/_kbl_own are the trio's shared globals for this shell.
kb_lock_take() { # <okey> -- 0 = we own it, 1 = someone else is draining
  _kbl_own=0
  _kbl_f="$STATE/${1:-default}.offset.lock"
  _kbl_d="$STATE/${1:-default}.offset.lock.d"
  if command -v flock >/dev/null 2>&1; then
    { exec 9>"$_kbl_f"; } 2>/dev/null || return 1
    flock -n 9 2>/dev/null || return 1
    _kbl_mode=flock
    return 0
  fi
  _kbl_mode=mkdir
  if mkdir "$_kbl_d" 2>/dev/null; then
    printf %s "$$" >"$_kbl_d/pid"
    _kbl_own=1
  else
    _kbl_p=$(cat "$_kbl_d/pid" 2>/dev/null)
    if { [ -z "$_kbl_p" ] || ! kill -0 "$_kbl_p" 2>/dev/null; } &&
      [ -n "$(find "$_kbl_d" -maxdepth 0 -mmin +"${KB_FLUSH_LOCK_STALE_MIN:-120}" 2>/dev/null)" ]; then
      rm -f "$_kbl_d/pid"
      rmdir "$_kbl_d" 2>/dev/null && mkdir "$_kbl_d" 2>/dev/null &&
        printf %s "$$" >"$_kbl_d/pid" && _kbl_own=1
    fi
  fi
  [ "${_kbl_own:-0}" = 1 ] || return 1
  # EXIT runs the cleanup exactly once; INT/TERM must EXIT (not merely clean up
  # and carry on relaying a chunk it can no longer checkpoint).
  trap '[ "$(cat "$_kbl_d/pid" 2>/dev/null)" = "$$" ] && rm -f "$_kbl_d/pid" && rmdir "$_kbl_d" 2>/dev/null' EXIT
  trap 'exit 143' INT TERM
  return 0
}
kb_lock_touch() { # keep a live mkdir dir fresh so it is never seen as stale
  [ "${_kbl_mode:-}" = mkdir ] && touch "$_kbl_d" 2>/dev/null
  return 0
}
kb_lock_owned() { # 0 = we still hold it (flock: always; mkdir: our pid)
  [ "${_kbl_mode:-}" = flock ] && return 0
  [ "$(cat "$_kbl_d/pid" 2>/dev/null)" = "$$" ]
}
kb_lock_release() { # give up the lock BEFORE the state sweep re-takes per candidate
  if [ "${_kbl_mode:-}" = flock ]; then
    exec 9>&-
  else
    [ "$(cat "$_kbl_d/pid" 2>/dev/null)" = "$$" ] && { rm -f "$_kbl_d/pid"; rmdir "$_kbl_d" 2>/dev/null; }
  fi
  trap - EXIT INT TERM
}
# <<< KB LOCK TRIO v1
export -f kb_lock_take kb_lock_touch kb_lock_owned kb_lock_release

# >>> KB FIRST RECORD v1
# The first record's <field> (cwd or timestamp) scanned from the head of a
# transcript. Copied BYTE-IDENTICAL into kb-flush-backfill.sh (C7) so the hook
# and the sweep derive a session's project and its first-seen time from the SAME
# bytes. jq when there is a jq; python3 otherwise (same chain as the hooks).
kb_first_record_field() { # <transcript> <cwd|timestamp> -> first record's value
  _kfrf=""
  _kfrk="${2:-cwd}"
  if [ -n "$KB_JQ_BIN" ]; then
    _kfrf=$(head -n "${KB_FLUSH_PIN_SCAN:-200}" "$1" 2>/dev/null |
      "$KB_JQ_BIN" -r --arg k "$_kfrk" 'select(type == "object") | .[$k] // empty' 2>/dev/null | head -n1)
  fi
  if [ -z "$_kfrf" ]; then
    _kfry="${KB_PY_BIN:-$(command -v python3 2>/dev/null || true)}"
    [ -n "$_kfry" ] && _kfrf=$(head -n "${KB_FLUSH_PIN_SCAN:-200}" "$1" 2>/dev/null | "$_kfry" -c 'import json,sys
k=sys.argv[1]
for line in sys.stdin:
    try: rec = json.loads(line)
    except Exception: continue
    if isinstance(rec, dict) and isinstance(rec.get(k), str) and rec[k].strip():
        sys.stdout.write(rec[k]); break' "$_kfrk" 2>/dev/null)
  fi
  [ -n "$_kfrf" ] && kb_winpath "$_kfrf"
}
# <<< KB FIRST RECORD v1
# kb_winpath lives in the portable block, which exports nothing itself; the
# detached child calls kb_first_record_field (which calls kb_winpath), so BOTH
# must reach it — an unexported function is "command not found" there, which
# once made the timestamp probe return empty and the unseeded refusal never fire.
export -f kb_first_record_field kb_winpath

# BRANCH / FORK SEED (C1, Appendix C). A copied/resumed transcript shares a byte
# prefix with the parent that already flushed it. Detect the sibling with the
# same first line, cmp to the first differing byte, snap DOWN to a newline, and
# seed the offset there so ONLY the fork's own bytes ever leave. Sets BSEED (>0)
# and BRANCH_OF (parent's 8-char id) when a parent is found; BSEED="" otherwise.
# One cmp per new transcript (stops at the first difference). Runs BEFORE the
# timestamp rule and BEFORE the shrank restart.
# Snap a byte count DOWN to the byte after the last newline at or before it.
# Bounded 1 MB windows walking backwards, so a 200 MB prefix is never read into
# memory. Counts newlines and takes `head -n <count>` rather than `tail -n1`:
# uutils coreutils (the owner box) truncates `tail -n1` of a newline-less 1 MB
# window to 983040 bytes, which put a seed mid-line. THE SAME LOOP as the
# backfill's bf_snap (kb-flush-backfill.sh) -- keep the arithmetic identical.
kb_snap_down() { # <transcript> <bytes> -> snapped bytes (0 when no newline precedes)
  _sm="$2"
  _snf=$(mktemp) || { printf '0'; return 0; }
  while [ "$_sm" -gt 0 ]; do
    _sw=$((_sm > 1048576 ? 1048576 : _sm))
    head -c "$_sm" "$1" | tail -c "$_sw" >"$_snf"
    _swb=$(stat -c%s "$_snf" 2>/dev/null || stat -f%z "$_snf" 2>/dev/null || wc -c <"$_snf" | tr -d ' ')
    case "$_swb" in '' | *[!0-9]*) _swb=0 ;; esac
    [ "$_swb" -gt 0 ] || break
    _snl=$(tr -cd '\n' <"$_snf" | wc -c | tr -d ' ')
    case "$_snl" in '' | *[!0-9]*) _snl=0 ;; esac
    if [ "$_snl" -gt 0 ]; then
      _supto=$(head -n "$_snl" "$_snf" | wc -c | tr -d ' ') # bytes through the last newline
      rm -f "$_snf"
      printf '%s' "$((_sm - _swb + _supto))"
      return 0
    fi
    _sm=$((_sm - _swb)) # no newline in this window: look further back
  done
  rm -f "$_snf"
  printf '0'
}
export -f kb_snap_down

# The chunk loop's last-newline trim: bytes of <file> through its LAST newline
# (0 when it has none). The SAME arithmetic as kb_snap_down/bf_snap -- count
# newlines, `head -n N | wc -c` -- never `tail -n1 | wc -c`, which uutils
# coreutils 0.8.0 prints as 0 for a newline-less 65536-byte window (so an
# unterminated fragment left as if it ended on a newline) and as 983040 for a
# 1 MB one (a 64 KiB mid-line cut).
kb_keep_to_last_nl() { # <file> -> bytes through the last newline (0 when none)
  _knl=$(tr -cd '\n' <"$1" | wc -c | tr -d ' ')
  case "$_knl" in '' | *[!0-9]*) _knl=0 ;; esac
  if [ "$_knl" -gt 0 ]; then head -n "$_knl" "$1" | wc -c | tr -d ' '; else printf '0'; fi
}
export -f kb_keep_to_last_nl

kb_branch_seed() {
  BSEED=""
  BRANCH_OF=""
  [ "${KB_FLUSH_BRANCH_SCAN:-1}" = 1 ] || return 0
  command -v cmp >/dev/null 2>&1 || return 0
  _first=$(head -c 4096 "$T" | head -n1)
  [ -n "$_first" ] || return 0
  _tsz=$(stat -c%s "$T" 2>/dev/null || stat -f%z "$T" 2>/dev/null || wc -c <"$T" | tr -d ' ')
  case "$_tsz" in '' | *[!0-9]*) _tsz=0 ;; esac
  _best=0
  _parent=""
  for _p in "$(dirname "$T")"/*.jsonl; do
    [ -e "$_p" ] || continue
    [ "$_p" = "$T" ] && continue
    [ "$(head -c 4096 "$_p" | head -n1)" = "$_first" ] || continue
    # Shared-prefix length, from cmp's EXIT STATUS first: 0 = identical files
    # (prefix = whole file); 1 = they differ, and stdout says where ("differ:
    # byte N" under a UTF-8 locale, "differ: char N" under C/POSIX and on BSD —
    # hence LC_ALL=C and a word-agnostic parse; N is the first DIFFERING byte,
    # 1-based, so the prefix is N-1) or stdout is EMPTY when one file is a
    # prefix of the other (EOF message on stderr) and the prefix is the whole
    # shorter file; >= 2 = cmp could not compare (unreadable sibling): no seed
    # from THIS sibling — a failed parse must never fall through to "identical".
    _cmpout=$(LC_ALL=C cmp "$T" "$_p" 2>/dev/null)
    _rc=$?
    [ "$_rc" -le 1 ] || continue
    _psz=$(stat -c%s "$_p" 2>/dev/null || stat -f%z "$_p" 2>/dev/null || wc -c <"$_p" | tr -d ' ')
    case "$_psz" in '' | *[!0-9]*) _psz=0 ;; esac
    if [ "$_rc" = 0 ]; then
      _off=$_psz
    else
      _diff=$(printf '%s' "$_cmpout" | sed -n 's/.*differ: [a-z]* \([0-9][0-9]*\).*/\1/p')
      if [ -n "$_diff" ]; then
        _off=$((_diff - 1))
      else
        _off=$((_psz < _tsz ? _psz : _tsz))
      fi
    fi
    case "$_off" in '' | *[!0-9]*) _off=0 ;; esac
    # Never seed past what the PARENT actually checkpointed: a parent whose tail
    # was never flushed (held partial line, undrained delta, itself refused as
    # unseeded) has bytes nobody sent, and a parent with NO offset file has no
    # flushed prefix at all — skip it, so the fork falls to the timestamp rule.
    _pk=$(printf '%s' "$(basename "$_p" .jsonl)" | tr -c 'A-Za-z0-9._-' '_' | tail -c 120) # same shape as OKEY
    _po=$(cat "$STATE/$_pk.offset" 2>/dev/null)
    case "$_po" in '' | *[!0-9]*) continue ;; esac
    [ "$_off" -gt "$_po" ] && _off=$_po
    [ "$_off" -gt "$_best" ] && { _best=$_off; _parent=$(basename "$_p" .jsonl); }
  done
  [ -n "$_parent" ] && [ "$_best" -gt 0 ] || return 0
  # Never seed past our own EOF (a parent longer than this fork).
  [ "${SIZE:-0}" -gt 0 ] && [ "$_best" -gt "$SIZE" ] && _best=$SIZE
  # Snap the shared-prefix length DOWN to the last newline at or before it
  # (Appendix C) with the SAME arithmetic as the backfill's bf_snap, so a hook
  # seed and an owner --seed land on the same byte.
  BSEED=$(kb_snap_down "$T" "$_best")
  case "$BSEED" in '' | *[!0-9]*) BSEED=0 ;; esac
  # The stamp value is pattern-checked server-side ([A-Za-z0-9._-]): sanitise.
  BRANCH_OF=$(printf '%s' "$_parent" | tr -c 'A-Za-z0-9._-' '-' | cut -c1-8)
  return 0
}
export -f kb_branch_seed

# THE offset writer (Appendix B invariant). Every offset write in the bearer
# child goes through here: it re-checks lock ownership FIRST (a lock lost
# mid-drain means another drain is authoritative — stop, never rewind), then
# writes atomically. `noclobber` is the first-sight W2 case (mv -n); every other
# writer (W1 advance, W2b seed-on-shrink, W3 shrank->0) REPLACES the file.
kb_offset_write() { # <value> [noclobber]
  kb_lock_owned || { echo "$(date -Is) flush $PROJECT lock lost — stopping ev=$EV sid=$SIDS"; exit 0; }
  printf %s "$1" >"$OFFF.t" || return 1
  if [ "${2:-}" = noclobber ]; then
    mv -n "$OFFF.t" "$OFFF" 2>/dev/null
    rm -f "$OFFF.t"
  else
    mv "$OFFF.t" "$OFFF"
  fi
}
export -f kb_offset_write

# Decide OFFSET before the first send, UNDER the lock (V1-m8 writer invariant).
# Sets OFFSET (and BRANCH_OF). Returns 1 when the firing must refuse (an
# unseeded pre-stamp transcript) — the caller then exits 0 with nothing sent.
# Order: branch seed FIRST (both on a fresh offset and on a shrink), then the
# trusted/unseeded timestamp rule, then the shrank restart.
# THE unseeded refusal, shared by the pre-stamp rule and the corrupt-offset
# path: ONE line per 24 h per session (rate-limited by the marker's mtime),
# naming the reason and the --seed procedure, and the <okey>.unseeded marker
# notify/the brief read. Never writes the offset.
kb_unseeded_refuse() { # <reason>
  _now=$(date +%s 2>/dev/null || echo 0)
  case "$_now" in '' | *[!0-9]*) _now=0 ;; esac
  _lm=$(stat -c %Y "$OFF_UNSEEDED" 2>/dev/null || stat -f %m "$OFF_UNSEEDED" 2>/dev/null || echo 0)
  case "$_lm" in '' | *[!0-9]*) _lm=0 ;; esac
  if [ "$((_now - _lm))" -ge 86400 ]; then
    echo "$(date -Is) flush $PROJECT unseeded: refusing to send $SIZE from 0 ($1) — seed it: kb-flush-backfill.sh --seed ${SID:-$OKEY} current|$SIZE ev=$EV sid=$SIDS"
    touch "$OFF_UNSEEDED" 2>/dev/null || true
  fi
  return 0
}
export -f kb_unseeded_refuse

kb_decide_offset() {
  BRANCH_OF=""
  if [ ! -f "$OFFF" ]; then
    kb_branch_seed
    if [ -n "$BSEED" ] && [ "$BSEED" -gt 0 ]; then
      kb_offset_write "$BSEED" noclobber
      OFFSET=$BSEED
      rm -f "$OFF_UNSEEDED"
      echo "$(date -Is) flush $PROJECT branch of $BRANCH_OF: seeded offset=$BSEED (copied history already flushed as the parent) ev=$EV sid=$SIDS"
      return 0
    fi
    # Trusted-vs-unseeded: a no-offset transcript is sent from 0 only when we can
    # SEE it is newer than the box's checkpoint stamp, or the owner forces it.
    # The stamp is written by install.sh at the wave-C install; a box without
    # one (a hand-copied hook) gets it created-if-absent HERE, as "now" — the
    # safe direction: every transcript that already existed is pre-stamp and is
    # refused (the old delete-lineage may already have flushed it), new sessions
    # proceed. A transcript that carries NO timestamp record cannot be dated and
    # proceeds — real Claude Code transcripts always carry one in the first
    # records; only synthetic/hand-fired fixtures do not, and refusing those
    # would strand every test session for no protection gained.
    _stamp="$STATE/.checkpoints-trusted-since"
    if [ ! -s "$_stamp" ]; then
      date -u +%Y-%m-%dT%H:%M:%SZ >"$_stamp.t" 2>/dev/null && mv -n "$_stamp.t" "$_stamp" 2>/dev/null
      rm -f "$_stamp.t"
    fi
    _fts=$(kb_first_record_field "$T" timestamp)
    _since=$(head -n1 "$_stamp" 2>/dev/null | tr -d ' \r')
    if [ "${KB_FLUSH_ALLOW_UNSEEDED:-0}" = 1 ] || [ -z "$_fts" ] ||
      { [ -n "$_since" ] && [ "$_fts" \> "$_since" ]; }; then
      OFFSET=0
      kb_offset_write 0 noclobber
      rm -f "$OFF_UNSEEDED"
      return 0
    fi
    kb_unseeded_refuse "transcript predates the checkpoint stamp"
    return 1
  fi
  OFFSET=$(cat "$OFFF" 2>/dev/null || echo 0)
  case "$OFFSET" in
    '' | *[!0-9]*)
      # A corrupt/empty offset file is NOT a licence to resend from 0 (KB
      # 33783: a from-0 resend is never automatic). Same refusal as a pre-stamp
      # transcript -- same marker, same one-line-per-24 h limit -- naming the
      # reason; the file itself is left untouched for the owner's --seed.
      kb_unseeded_refuse "offset unreadable: $OFFF"
      return 1
      ;;
  esac
  # A numeric offset exists: the session is seeded (by us, a branch seed or the
  # owner's --seed), so a stale unseeded marker must not keep notify saying
  # "unseeded".
  rm -f "$OFF_UNSEEDED"
  if [ "$SIZE" -lt "$OFFSET" ]; then
    kb_branch_seed
    if [ -n "$BSEED" ] && [ "$BSEED" -gt 0 ]; then
      kb_offset_write "$BSEED"
      OFFSET=$BSEED
      echo "$(date -Is) flush $PROJECT branch of $BRANCH_OF: seeded offset=$BSEED (copied history already flushed as the parent) ev=$EV sid=$SIDS"
    else
      echo "$(date -Is) flush $PROJECT transcript shrank ($SIZE < offset $OFFSET) reason=shrank — restarting from 0 ev=$EV sid=$SIDS"
      OFFSET=0
      kb_offset_write 0
    fi
  fi
  return 0
}
export -f kb_decide_offset

# State sweep (C8). Runs in the detached child AFTER kb_lock_release, once per
# 24 h. Retire a session's state set ONLY when the offset is older than 30 days
# AND the transcript is gone under BOTH config roots. One subshell per candidate
# so a re-taken lock is released on subshell exit; a .retry beside a retired
# offset means bytes were still pending -> a loss note.
kb_state_sweep() {
  _sl="$STATE/.state-sweep.last"
  _now=$(date +%s 2>/dev/null || echo 0)
  case "$_now" in '' | *[!0-9]*) _now=0 ;; esac
  _lm=$(stat -c %Y "$_sl" 2>/dev/null || stat -f %m "$_sl" 2>/dev/null || echo 0)
  case "$_lm" in '' | *[!0-9]*) _lm=0 ;; esac
  [ "$((_now - _lm))" -ge 86400 ] || return 0
  printf %s "$_now" >"$_sl.t" 2>/dev/null && mv "$_sl.t" "$_sl" 2>/dev/null || true
  _r1="$HOME/.claude/projects"
  _r2="${CLAUDE_CONFIG_DIR:-}/projects"
  for _of in "$STATE"/*.offset; do
    [ -e "$_of" ] || continue
    (
      _k=$(basename "$_of" .offset)
      [ -n "$(find "$_of" -maxdepth 0 -mmin +43200 2>/dev/null)" ] || exit 0
      _tr=""
      [ -d "$_r1" ] && _tr=$(find "$_r1" -mindepth 2 -maxdepth 2 -name "$_k.jsonl" -print -quit 2>/dev/null)
      if [ -z "$_tr" ] && [ -n "${CLAUDE_CONFIG_DIR:-}" ] && [ -d "$_r2" ]; then
        _tr=$(find "$_r2" -mindepth 2 -maxdepth 2 -name "$_k.jsonl" -print -quit 2>/dev/null)
      fi
      [ -n "$_tr" ] && exit 0 # transcript present, however old -> KEEP
      kb_lock_take "$_k" || exit 0
      _loss=""
      [ -f "$STATE/$_k.retry" ] && _loss=" (retry marker present: bytes were pending)"
      # The whole per-session state set of Appendix I (<okey>.<suffix>; only the
      # lock keeps the .offset infix) plus a leftover atomic-write temp. The lock
      # dir, when this box uses one, is ours now and kb_lock_release removes it.
      rm -f "$STATE/$_k.offset" "$STATE/$_k.offset.lock" "$STATE/$_k.offset.t" \
        "$STATE/$_k.last" "$STATE/$_k.gate" "$STATE/$_k.retry" "$STATE/$_k.project" \
        "$STATE/$_k.unseeded" "$STATE/$_k.sweep"
      kb_lock_release
      echo "$(date -Is) flush state sweep: retired sid=$_k transcript gone$_loss"
    )
  done
}
export -f kb_state_sweep

# Session pin (C4). Decide a session's project ONCE and cache it in
# STATE/<okey>.project as "<slug>\t<how>". Order: cache -> the git root of the
# transcript's FIRST cwd record (.kbproject there, else its basename) ->
# firing-time derivation from the event cwd. Sets PROJECT, PROJECT_BY (the
# how: record|kbproject|gitfirst|firing) and PROJECT_MINTABLE (1 only for a real
# git root, never for $HOME/$TMPDIR//tmp or a non-git dir). KB_FLUSH_PROJECT_PIN=0
# restores today's per-firing derivation.
kb_slugify() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed 's/-\{2,\}/-/g;s/^-//;s/-$//'; }
# Sets globals _KP_SLUG and _KP_HOW from a git-root dir (reads .kbproject when
# present). Sets globals rather than echoing so the how survives — a $(subshell)
# caller would discard it (the incident this whole file guards against).
kb_project_from_dir() { # <dir>
  _kpd="$1"
  _kpraw=$(basename "$_kpd")
  _KP_HOW=gitfirst
  if [ -r "$_kpd/.kbproject" ]; then
    _kpdecl=$(head -n1 "$_kpd/.kbproject" 2>/dev/null | tr -d '\r' | cut -c1-64)
    [ -n "$_kpdecl" ] && { _kpraw="$_kpdecl"; _KP_HOW=kbproject; }
  fi
  _KP_SLUG=$(kb_slugify "$_kpraw")
}
kb_ephemeral_dir() { # 0 = a dir we must never auto-mint from ($HOME, $TMPDIR, /tmp)
  _kbe="${TMPDIR:-}"
  _kbe="${_kbe%/}"
  case "$1" in
    "$HOME" | /tmp | /tmp/*) return 0 ;;
  esac
  [ -n "$_kbe" ] && [ "$1" = "$_kbe" ] && return 0
  return 1
}
# The DISTINCT cwd values of the first KB_FLUSH_PIN_SCAN records, in order —
# the pin scans these for the first one that is a git dir (a session launched
# in $HOME that then `cd`s into a repo pins to that repo, not to $HOME). Same
# jq-then-python chain as kb_first_record_field, which stays untouched (it is
# the block the backfill copies byte-for-byte).
kb_record_cwds() { # <transcript> -> one cwd per line
  _krc=""
  if [ -n "$KB_JQ_BIN" ]; then
    _krc=$(head -n "${KB_FLUSH_PIN_SCAN:-200}" "$1" 2>/dev/null |
      "$KB_JQ_BIN" -r 'select(type == "object") | .cwd // empty' 2>/dev/null | awk '!s[$0]++')
  fi
  if [ -z "$_krc" ]; then
    _krcy="${KB_PY_BIN:-$(command -v python3 2>/dev/null || true)}"
    [ -n "$_krcy" ] && _krc=$(head -n "${KB_FLUSH_PIN_SCAN:-200}" "$1" 2>/dev/null | "$_krcy" -c 'import json,sys
seen=set()
for line in sys.stdin:
    try: rec = json.loads(line)
    except Exception: continue
    c = rec.get("cwd") if isinstance(rec, dict) else None
    if isinstance(c, str) and c.strip() and c not in seen:
        seen.add(c); sys.stdout.write(c + "\n")' 2>/dev/null)
  fi
  [ -n "$_krc" ] && printf '%s\n' "$_krc"
}
# 0 when the EVENT cwd's own git root is a real (non-ephemeral) repo that names
# $PROJECT — the only condition under which a firing-derived slug may mint.
# Recomputed on EVERY firing, so the first derivation and every later cache hit
# reach the same answer (a pinned slug never mints on the strength of a
# DIFFERENT repo the event happens to fire from).
kb_firing_mintable() {
  _kfm=$(git -C "${CWD:-.}" rev-parse --show-toplevel 2>/dev/null) || return 1
  [ -n "$_kfm" ] || return 1
  kb_ephemeral_dir "$_kfm" && return 1
  kb_project_from_dir "$_kfm"
  [ "$_KP_SLUG" = "$PROJECT" ]
}
kb_pin_project() {
  PROJECT_MINTABLE=0
  _pc="$OFF_PROJECT"
  if [ "${KB_FLUSH_PROJECT_PIN:-1}" = 1 ] && [ -s "$_pc" ]; then
    PROJECT=$(cut -f1 "$_pc" 2>/dev/null)
    PROJECT_BY=$(cut -s -f2 "$_pc" 2>/dev/null) # -s: a tab-less line is a MISS, not how=<slug>
    if [ -n "$PROJECT" ] && [ -n "$PROJECT_BY" ]; then
      case "$PROJECT_BY" in
        gitfirst | kbproject) PROJECT_MINTABLE=1 ;;
        *) [ -n "${KB_FLUSH_NO_MINT:-}" ] || { kb_firing_mintable && PROJECT_MINTABLE=1; } ;; # moot on Stop/SessionEnd: no git call
      esac
      return 0
    fi
  fi
  # Derive. With pinning on, anchor to the FIRST recorded cwd that is a real git
  # dir (never $HOME/$TMPDIR//tmp, even when one of those is itself a repo); the
  # firing-time event cwd is the fallback, and the whole rule when pinning is off.
  _proot=""
  if [ "${KB_FLUSH_PROJECT_PIN:-1}" = 1 ]; then
    while IFS= read -r _pcwd; do
      [ -n "$_pcwd" ] || continue
      _pcwd=$(kb_winpath "$_pcwd")
      _proot=$(git -C "$_pcwd" rev-parse --show-toplevel 2>/dev/null) || _proot=""
      [ -n "$_proot" ] && ! kb_ephemeral_dir "$_proot" && break
      _proot=""
    done < <(kb_record_cwds "$T")
  fi
  if [ -n "$_proot" ]; then
    kb_project_from_dir "$_proot"
    PROJECT="$_KP_SLUG"
    PROJECT_BY="$_KP_HOW"
    PROJECT_MINTABLE=1
  else
    # Firing-time derivation from the event cwd (today's rule).
    _fdir=$(git -C "${CWD:-.}" rev-parse --show-toplevel 2>/dev/null) || _fdir=""
    if [ -n "$_fdir" ]; then
      kb_project_from_dir "$_fdir"
      PROJECT="$_KP_SLUG"
    else
      PROJECT=$(kb_slugify "$(basename "${CWD:-sessions}")")
    fi
    PROJECT_BY=firing
    [ -n "${KB_FLUSH_NO_MINT:-}" ] || { kb_firing_mintable && PROJECT_MINTABLE=1; } # never for $HOME/$TMPDIR//tmp or a non-git dir
  fi
  [ -n "$PROJECT" ] || { PROJECT=sessions; PROJECT_BY=firing; PROJECT_MINTABLE=0; }
  if [ "${KB_FLUSH_PROJECT_PIN:-1}" = 1 ]; then
    printf '%s\t%s\n' "$PROJECT" "$PROJECT_BY" >"$_pc.t" 2>/dev/null && mv "$_pc.t" "$_pc" 2>/dev/null || true
  fi
}


INPUT=$(cat)
T=$(printf '%s' "$INPUT" | kb_json_get transcript_path)
CWD=$(printf '%s' "$INPUT" | kb_json_get cwd)
SID=$(printf '%s' "$INPUT" | kb_json_get session_id)
EV=$(printf '%s' "$INPUT" | kb_json_get hook_event_name)
# The dispatcher passes the event as $1 (kb-hook-dispatch.sh): an event whose
# stdin arrived EMPTY (the residual Windows bug) still names itself there.
[ -n "$EV" ] || EV="${1:-}"
# Native Windows event paths (C:\... or C:/...) must be converted — basename on a
# backslash path yields the whole drive path (item 496). kb_winpath uses cygpath
# when present and a pure-shell C:\ -> /c/ fallback when it is not.
T=$(kb_winpath "$T")
CWD=$(kb_winpath "$CWD")
[ -n "$T" ] && [ -f "$T" ] || exit 0
# Sub-agent transcripts (<project>/<sid>/subagents/<agent>.jsonl) are an EXPLICIT
# exclusion — nothing at all, on every event — no longer an accident of a glob.
case "$T" in */subagents/*) exit 0 ;; esac

# The per-session offset key and its state paths. Computed ONCE here (moved up
# from the transport arms) so the pin cache, the offset writers and the lock all
# share one identity. OKEY is the sanitised session id (or the transcript path
# when there is none); the shape is pinned against notify-complete.sh by the
# funnel, so keep it byte-for-byte.
STATE="$HOME/.claude/hooks/state"
mkdir -p "$STATE" 2>/dev/null || true
LOG="$HOME/.claude/hooks/flush-session.log"
OKEY=$(printf '%s' "${SID:-$T}" | tr -c 'A-Za-z0-9._-' '_' | tail -c 120)
OFFF="$STATE/${OKEY:-default}.offset"
SIDS=$(printf '%s' "${SID:-$OKEY}" | cut -c1-8)
# The rest of the per-session state set (Appendix I), named ONCE: <okey>.<suffix>
# — only the lock keeps the .offset infix (<okey>.offset.lock[.d/pid]). notify,
# the brief, the Stop gate and the backfill all read these by the same names,
# and kb_state_sweep retires exactly this set.
OFF_LAST="$STATE/${OKEY:-default}.last"
OFF_GATE="$STATE/${OKEY:-default}.gate"
OFF_RETRY="$STATE/${OKEY:-default}.retry"
OFF_PROJECT="$STATE/${OKEY:-default}.project"
OFF_UNSEEDED="$STATE/${OKEY:-default}.unseeded"
OFF_SWEEP="$STATE/${OKEY:-default}.sweep"
export OFF_LAST OFF_GATE OFF_RETRY OFF_PROJECT OFF_UNSEEDED OFF_SWEEP

# Wave-C knobs, read in ONE place (Appendix contract defaults).
export KB_FLUSH_CHUNK="${KB_FLUSH_CHUNK:-700000}"
export KB_FLUSH_SEND_MAX="${KB_FLUSH_SEND_MAX:-950000}"
export KB_FLUSH_MAX="${KB_FLUSH_MAX:-209715200}"
export KB_FLUSH_ALLOW_UNSEEDED="${KB_FLUSH_ALLOW_UNSEEDED:-0}"
export KB_FLUSH_BRANCH_SCAN="${KB_FLUSH_BRANCH_SCAN:-1}"
export KB_FLUSH_LOCK_STALE_MIN="${KB_FLUSH_LOCK_STALE_MIN:-120}"
export KB_FLUSH_PROJECT_PIN="${KB_FLUSH_PROJECT_PIN:-1}"
export KB_FLUSH_PIN_SCAN="${KB_FLUSH_PIN_SCAN:-200}"
# Stage-2 knobs (C5 Stop gate, C6 transport + retry marker, the sweep piggyback).
export KB_FLUSH_STOP_MIN_BYTES="${KB_FLUSH_STOP_MIN_BYTES:-4000000}"
export KB_FLUSH_STOP_MIN_INTERVAL="${KB_FLUSH_STOP_MIN_INTERVAL:-300}"
export KB_FLUSH_STOP_MAX_AGE="${KB_FLUSH_STOP_MAX_AGE:-1800}"
export KB_FLUSH_STOP_MIN_BYTES_AGED="${KB_FLUSH_STOP_MIN_BYTES_AGED:-65536}"
export KB_FLUSH_RETRY_BASE="${KB_FLUSH_RETRY_BASE:-300}"
export KB_FLUSH_RETRY_MAX="${KB_FLUSH_RETRY_MAX:-3600}"
export KB_FLUSH_RETRY_GIVEUP="${KB_FLUSH_RETRY_GIVEUP:-20}"
export KB_FLUSH_CURL_MAX="${KB_FLUSH_CURL_MAX:-60}"
export KB_FLUSH_CURL_RETRY="${KB_FLUSH_CURL_RETRY:-2}"
export KB_FLUSH_CURL_RETRY_MAX="${KB_FLUSH_CURL_RETRY_MAX:-30}"
export KB_SWEEP_INTERVAL="${KB_SWEEP_INTERVAL:-1800}"
# CLAUDE_CONFIG_DIR is the second config root the state sweep probes (C8); export
# it (possibly empty) so the detached child sees the same value the parent did.
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-}"

kb_now() { # epoch seconds, 0 when date cannot say
  _kn=$(date +%s 2>/dev/null || echo 0)
  case "$_kn" in '' | *[!0-9]*) _kn=0 ;; esac
  printf '%s' "$_kn"
}
export -f kb_now

# Retry back-off (C6, Appendix D) for a <okey>.retry marker `<epoch> <n> <code>
# <end>`: 429/503 mean the server asked us to back off -> the long bound at
# once (KB_FLUSH_RETRY_MAX, not the exponential schedule: a server that says
# "slow down" is not a flap to probe every 5 minutes); anything else ->
# min(KB_FLUSH_RETRY_MAX, KB_FLUSH_RETRY_BASE * 2^(n-1)), i.e. 5, 10, 20, 40,
# 60, 60 ... minutes.
kb_retry_backoff() { # <n> <code> -> seconds
  _rbn="${1:-1}"
  case "$_rbn" in '' | *[!0-9]* | 0) _rbn=1 ;; esac
  case "${2:-}" in 429 | 503)
    printf '%s' "$KB_FLUSH_RETRY_MAX"
    return 0
    ;;
  esac
  _rbs="$KB_FLUSH_RETRY_BASE"
  _rbi=1
  while [ "$_rbi" -lt "$_rbn" ] && [ "$_rbs" -lt "$KB_FLUSH_RETRY_MAX" ]; do
    _rbs=$((_rbs * 2))
    _rbi=$((_rbi + 1))
  done
  [ "$_rbs" -gt "$KB_FLUSH_RETRY_MAX" ] && _rbs="$KB_FLUSH_RETRY_MAX"
  printf '%s' "$_rbs"
}

# THE STOP GATE (C5, hybrid_v1 s2.2, Appendix D). Stop fires on EVERY turn, so
# the decision is made from two stats and a few cats BEFORE any pin, git, token
# or mint work: 0 = open (the caller stamps <okey>.gate and detaches a drain),
# 1 = closed (the caller exits 0 with ZERO network and NO log line).
#   size path: (SIZE - OFFSET) >= KB_FLUSH_STOP_MIN_BYTES  and  now - .last >= KB_FLUSH_STOP_MIN_INTERVAL
#   age path : (SIZE - OFFSET) >= KB_FLUSH_STOP_MIN_BYTES_AGED and now - .last >= KB_FLUSH_STOP_MAX_AGE
#   retry    : a .retry marker whose back-off has elapsed (n < KB_FLUSH_RETRY_GIVEUP)
# A marker inside its back-off holds EVERY path; at give-up (n >= GIVEUP) the
# retry and age paths stay shut and only the size path remains (PreCompact,
# SessionEnd and the sweep ignore the marker entirely). .last is written by the
# CHILD when its drain starts, so a .gate NEWER than .last means the parent was
# killed before it could detach (the dispatcher's 3 s budget, rc 124): the gate
# then stays closed for one interval instead of re-opening on every turn.
kb_stop_gate() {
  { [ -n "${KB_MCP_URL:-}" ] || [ -n "${KB_REST_URL:-}" ]; } || return 1 # LOCAL writes no offset; CONNECTOR is usage-metered
  # A refused (unseeded) session -- no offset (pre-stamp), or a CORRUPT one --
  # carries an .unseeded marker: until the owner seeds it (which writes a
  # numeric offset) every open gate would only pin, lock, stamp .last and
  # refuse again -- one stat and one cat, so it costs nothing on Stop.
  # PreCompact/SessionEnd still reach the daily line.
  _sgo=$(cat "$OFFF" 2>/dev/null || echo 0)
  if [ -e "$OFF_UNSEEDED" ]; then
    [ -f "$OFFF" ] || return 1
    case "$_sgo" in '' | *[!0-9]*) return 1 ;; esac
  fi
  _sgn=$(kb_now)
  _sgs=$(stat -c%s "$T" 2>/dev/null || stat -f%z "$T" 2>/dev/null || echo 0)
  case "$_sgs" in '' | *[!0-9]*) _sgs=0 ;; esac
  [ "$_sgs" -gt 0 ] || return 1
  case "$_sgo" in '' | *[!0-9]*) _sgo=0 ;; esac # the CHILD decides trusted vs unseeded before sending anything
  _sgd=$((_sgs - _sgo))
  [ "$_sgd" -gt 0 ] || return 1 # a shrink is the drain's job, not the gate's
  _sgl=$(cat "$OFF_LAST" 2>/dev/null || echo 0)
  case "$_sgl" in '' | *[!0-9]*) _sgl=0 ;; esac
  _sgg=$(cat "$OFF_GATE" 2>/dev/null || echo 0)
  case "$_sgg" in '' | *[!0-9]*) _sgg=0 ;; esac
  if [ "$_sgg" -gt "$_sgl" ] && [ "$((_sgn - _sgg))" -lt "$KB_FLUSH_STOP_MIN_INTERVAL" ]; then
    return 1 # an open gate that never started its drain: back off one interval
  fi
  _sgrn=""
  _sgre=0
  _sgrc=""
  if [ -s "$OFF_RETRY" ]; then
    read -r _sgre _sgrn _sgrc _sgrx <"$OFF_RETRY" 2>/dev/null || true
    case "$_sgre" in '' | *[!0-9]*) _sgre=0 ;; esac
    case "$_sgrn" in *[!0-9]*) _sgrn="" ;; esac
  fi
  if [ -n "$_sgrn" ]; then
    [ "$((_sgn - _sgre))" -ge "$(kb_retry_backoff "$_sgrn" "$_sgrc")" ] || return 1 # back-off hold
    [ "$_sgrn" -lt "$KB_FLUSH_RETRY_GIVEUP" ] && return 0                            # retry due
    [ "$_sgd" -ge "$KB_FLUSH_STOP_MIN_BYTES" ] && [ "$((_sgn - _sgl))" -ge "$KB_FLUSH_STOP_MIN_INTERVAL" ] && return 0
    return 1 # given up: the age path is suppressed
  fi
  [ "$_sgd" -ge "$KB_FLUSH_STOP_MIN_BYTES" ] && [ "$((_sgn - _sgl))" -ge "$KB_FLUSH_STOP_MIN_INTERVAL" ] && return 0
  [ "$_sgd" -ge "$KB_FLUSH_STOP_MIN_BYTES_AGED" ] && [ "$((_sgn - _sgl))" -ge "$KB_FLUSH_STOP_MAX_AGE" ] && return 0
  return 1
}

# The sweep PIGGYBACK (hybrid_v1 s2.5): from any hook firing on a bearer box, at
# most once per KB_SWEEP_INTERVAL (0 = off), touch STATE/.sweep.trigger — a
# CLOSED Stop touches it too, so the sweep can see the box is alive — and, when
# the installed backfill understands --auto, detach one sweep run. The touch
# comes BEFORE the detach so the Backfill firings it provokes cannot re-trigger
# it. Cost on the hot path: one stat. Never from a Backfill firing itself.
kb_sweep_trigger() {
  [ "$EV" = Backfill ] && return 0
  [ "$KB_SWEEP_INTERVAL" -gt 0 ] 2>/dev/null || return 0
  { [ -n "${KB_MCP_URL:-}" ] || [ -n "${KB_REST_URL:-}" ]; } || return 0
  _swt="$STATE/.sweep.trigger"
  _swm=$(stat -c %Y "$_swt" 2>/dev/null || stat -f %m "$_swt" 2>/dev/null || echo 0)
  case "$_swm" in '' | *[!0-9]*) _swm=0 ;; esac
  [ "$(($(kb_now) - _swm))" -ge "$KB_SWEEP_INTERVAL" ] || return 0
  touch "$_swt" 2>/dev/null || return 0
  _swb="$HOOKDIR/kb-flush-backfill.sh"
  [ -f "$_swb" ] && grep -q -- '--auto' "$_swb" 2>/dev/null || return 0
  "$DETACH" bash "$_swb" --auto </dev/null >>"$LOG" 2>&1 &
  # Never a job of THIS shell: under KB_FLUSH_FOREGROUND=1 (DETACH=env) the
  # arms' trailing `wait` would otherwise block on the whole sweep run too.
  disown 2>/dev/null || true
  return 0
}

# Stop and SessionEnd never host the token auto-mint (C5): the Stop parent is
# budgeted at 3 s and a terminal firing is not the moment for a 45 s `uv run`.
# PreCompact (and Backfill) still mint — in the detached CHILD, never here.
KB_FLUSH_NO_MINT="${KB_FLUSH_NO_MINT:-}"
case "$EV" in Stop | SessionEnd) KB_FLUSH_NO_MINT=1 ;; esac
export KB_FLUSH_NO_MINT
kb_sweep_trigger
if [ "$EV" = Stop ]; then
  kb_stop_gate || exit 0 # closed: zero network, no log line, nothing computed below
  # Open: stamp .gate BEFORE the detach. The child writes .last when its drain
  # starts; a .gate left newer than .last is the "killed before detach" signal.
  printf '%s' "$(kb_now)" >"$OFF_GATE.t" 2>/dev/null && mv "$OFF_GATE.t" "$OFF_GATE" 2>/dev/null || true
fi

# Project = pinned ONCE per session from the transcript's first cwd record (C4),
# cached in STATE/<okey>.project. Falls back to the firing-time derivation (the
# git root of the event cwd, its .kbproject, else the slugified basename) when
# there is no usable cwd record or KB_FLUSH_PROJECT_PIN=0. Sets PROJECT,
# PROJECT_BY (record|kbproject|gitfirst|firing) and PROJECT_MINTABLE.
kb_pin_project
[ -z "$PROJECT" ] && PROJECT=sessions
# THE DRAIN CONTEXT IS COMPUTED IN THE CHILD (C5, DO-NOT-DO s7: no git work in
# the Stop parent). Every value below rides only the request body, so nothing in
# the parent needs it; on Git Bash each `git` spawn is 100-300 ms (seconds under
# Defender or a dead mount) and a Stop parent past its 3 s budget is killed --
# which leaves .gate newer than .last and backs the gate off for an interval.
# The names are still EXPORTED from here (empty) so the funnel's export-class
# guard keeps them in its set: a child that forgot to call kb_drain_context
# would send no git context rather than a stale one.
REPO=""; BRANCH=""; COMMIT=""; HOOKVER=""; MTIME=0; BOX=""
kb_drain_context() { # sets REPO BRANCH COMMIT HOOKVER MTIME BOX from the exported T/CWD
  MTIME=$(stat -c %Y "$T" 2>/dev/null || stat -f %m "$T" 2>/dev/null || echo 0)
  case "$MTIME" in '' | *[!0-9]*) MTIME=0 ;; esac
  BOX="${KB_BOX:-$(hostname 2>/dev/null || uname -n 2>/dev/null || echo box)}"
  BOX=$(printf '%s' "$BOX" | tr -c 'A-Za-z0-9._-' '-' | cut -c1-64)
  # Git context for code_ref anchoring: kb_ingest_session accepts repo/branch/
  # commit and the drain stamps a code_ref from them. Local plumbing reads only
  # (no network); a non-git CWD degrades silently to empty. The arg builders
  # append them as extra kb_json_build triples -- empty fields are OMITTED
  # entirely (never sent as "").
  REPO=$(git -C "${CWD:-.}" remote get-url origin 2>/dev/null)
  [ -n "$REPO" ] || REPO=$(git -C "${CWD:-.}" rev-parse --show-toplevel 2>/dev/null)
  REPO=${REPO%/}
  REPO=${REPO%.git}
  REPO=${REPO##*/}
  REPO=${REPO##*:}
  BRANCH=$(git -C "${CWD:-.}" rev-parse --abbrev-ref HEAD 2>/dev/null)
  [ "$BRANCH" = "HEAD" ] && BRANCH="" # detached HEAD names no branch
  COMMIT=$(git -C "${CWD:-.}" rev-parse --short HEAD 2>/dev/null)
  # Hook provenance for the fleet: the manifest version install.sh stamped into
  # ~/.claude/hooks/.setup-version rides the REST payload as hook_version, so the
  # queue row's meta can say WHICH shipped assets produced a submission (the same
  # additive-meta pattern as submitted_by; the server ignores the field until it
  # learns it). SHAPE-CHECKED like every other value we put on the wire: the
  # stamp is a 12-hex content digest, anything else is treated as absent, and
  # absence is itself the signal -- old hook code and unverified installs never stamp.
  HOOKVER=$(head -n1 "$HOME/.claude/hooks/.setup-version" 2>/dev/null | tr -d ' \r')
  case "$HOOKVER" in
    [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) : ;;
    *) HOOKVER="" ;;
  esac
}
export -f kb_drain_context
# EVERY variable the detached children read must be EXPORTED. The children are
# `setsid/nohup bash -c '<single-quoted>'` — a FRESH shell that inherits only the
# environment, so a var set in this parent but not exported expands to EMPTY
# there, silently and with no error under `set -u` (the child does not set -u).
# That is not a hypothetical: OKEY was computed here and used as the session
# identity in the child, so every network flush from every machine sent
# session_uri='session:' and 137 rows from six projects coalesced into
# cross-project mega-extractions. CFG is exported for the same reason — the
# connector child names it in two "here is how to fix this" lines that were
# printing a bare "in " with no path. tests/test_capture_funnel.py now derives
# the reference set mechanically from the script, so the whole CLASS is guarded,
# not the one instance.
export T CWD SID PROJECT LOG REPO BRANCH COMMIT CFG HOOKVER
# Wave-C additions the detached child reads: the session identity/paths (OKEY,
# OFFF, STATE), the pin result (PROJECT_BY) and the per-firing flush stamps
# (MTIME, BOX, SIDS). Every one is read inside a child, so every one is exported
# (the class this file exists to guard — see the OKEY incident below).
export OKEY OFFF STATE PROJECT_BY MTIME BOX SIDS
export -f kb_pin_project kb_slugify kb_project_from_dir kb_ephemeral_dir
# The detached child is a FRESH bash: hand it the JSON backend (which may be
# python3, with no jq anywhere on the box) as exported vars + functions.
export KB_JQ_BIN KB_PY_BIN KB_JSON_PROBED
export -f kb_json_build kb_json_valid
# kb_timeout must reach the detached child too: the CONNECTOR relay is the one
# network call in this script that was never bounded, and an unbounded call there
# cannot even report itself (every summary line is written AFTER the loop).
export KB_TIMEOUT_BIN KB_TIMEOUT_PROBED
export -f kb_timeout _kb_timeout_ok

# Feed the bearer to curl on STDIN (curl -K -), never on the command line: argv
# is world-visible through /proc/<pid>/cmdline and `ps`, and a credential has
# already leaked that way once on this system (2026-07-31). Same name and shape
# as kb-session-brief.sh's copy. EXPORTED, because the REST submission runs in
# the detached child -- the transcript-capture path carries the highest-value
# bearer on the box (the owner's master token where no project token exists),
# and this curl was the last one still holding it in argv. An empty bearer
# yields a config holding only the address-family pin, i.e. an unauthenticated
# request the server refuses loudly -- never a silent skip.
# `ipv4` pins every `curl -K -` consumer to the A record at this one layer: on
# this network the AAAA record resolves first and IPv6 egress fails, which took
# out flush/brief/enrol together during an AAAA-only transient while the ntfy
# push (long pinned -4) kept working. Emitted BEFORE the bearer guard so even a
# bearer-less request fails as a loud 401, never as a doomed IPv6 connect.
kb_curl_auth_config() { # -> curl config on stdout
  printf 'ipv4\n'
  [ -n "${KB_TOKEN:-}" ] || return 0
  printf 'header = "Authorization: Bearer %s"\n' "$KB_TOKEN"
}
export -f kb_curl_auth_config

# THE REST SUBMISSION (C6, Appendix G). ONE function, used by the first attempt
# and the script-level retry alike, so the two can never drift apart; it runs in
# the detached child. `--retry` covers timeouts and 408/429/5xx; KB_CURL_RETRY_ALL
# (probed in the CHILD: `--retry-all-errors` on curl >= 7.71, else empty) extends
# it to connection failures — curl (6)/(7), which were 30 of the 36 real failures.
# `--max-time` resets per retry; `--retry-max-time` bounds the whole chain.
# RE-POSTING IS SAFE ONLY AGAINST THE DEDUPING /api/ingest-session. Never copy this line to a non-idempotent endpoint.
# NO connect timeout option: on Git Bash/Schannel it aborts healthy slow requests (KB 1195). NO fail option: 401/403 must not be retried.
# shellcheck disable=SC2086
kb_rest_post() { # <body-file> <response-file> -> http code on stdout (000 = no response)
  kb_curl_auth_config | curl -sS -K - -o "$2" -w "%{http_code}" \
    --max-time "${KB_FLUSH_CURL_MAX:-60}" --retry "${KB_FLUSH_CURL_RETRY:-2}" --retry-delay 3 \
    --retry-max-time "${KB_FLUSH_CURL_RETRY_MAX:-30}" $KB_CURL_RETRY_ALL \
    -X POST "$KB_REST_URL/api/ingest-session" -H "Content-Type: application/json" --data @"$1"
}
export -f kb_rest_post

# The retry marker is removed ONLY by a drain whose final offset reached the
# end the marker recorded (Appendix D; field 4 = the failed chunk's end byte). Re-read at the end of the drain, so a
# marker another firing wrote meanwhile (a lock-deferred SessionEnd) is judged on
# ITS size, not on whatever this drain saw when it started.
kb_retry_clear() { # <final offset>
  [ -s "$OFF_RETRY" ] || return 0
  _rcs=$(awk 'NR == 1 { print $4 }' "$OFF_RETRY" 2>/dev/null)
  case "$_rcs" in '' | *[!0-9]*) _rcs=0 ;; esac
  [ "${1:-0}" -ge "$_rcs" ] && rm -f "$OFF_RETRY"
  return 0
}
export -f kb_retry_clear

# Opportunistic housekeeping — bound our OWN log growth so a daily-driver machine
# doesn't accumulate an unrotated multi-MB log. Runs in the detached CHILD (C5:
# nothing but the gate, the pin and the token file in the Stop parent) and
# rewrites the log IN PLACE (truncate + refill the same inode): the child's own
# `>>"$LOG"` descriptor and every sibling's stay valid, where a mv would leave
# them appending to an unlinked file. Offsets are NEVER deleted here
# (KB-FLUSH-LINEAGE never-delete-offsets v2): the 30-day `find … -delete` is gone
# and its job moved to kb_state_sweep (C8) in the detached child, which retires a
# session's state ONLY once its transcript is gone under both config roots.
kb_log_rotate() {
  [ -f "$LOG" ] || return 0
  _lrs=$(stat -c%s "$LOG" 2>/dev/null || stat -f%z "$LOG" 2>/dev/null || wc -c <"$LOG" | tr -d " ")
  case "$_lrs" in "" | *[!0-9]*) _lrs=0 ;; esac
  [ "$_lrs" -gt 1048576 ] || return 0
  tail -n 500 "$LOG" >"$LOG.t" 2>/dev/null && cat "$LOG.t" >"$LOG" 2>/dev/null
  rm -f "$LOG.t" 2>/dev/null
  return 0
}
export -f kb_log_rotate

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
KB_REPO="${KB_REPO:-$HOME/data-server}"
PROJ_KEY=$(printf '%s' "$PROJECT" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9' '_')
PROJ_TOKEN=$(eval "printf '%s' \"\${KB_TOKEN_${PROJ_KEY}:-}\"" 2>/dev/null || true)
# AUTO-MINT. A session that finds no token for its project is knowledge SILENTLY
# LOST — measured 2026-07-31: 8 of 22 flushes were refused (sungrow, rm3, gateway,
# bss-backend) because nobody had minted a token for those projects, and nothing
# surfaced it but a line in this log. Minting on demand removes a manual step that
# was never going to keep pace with new projects.
#
# STRICTLY LOCAL. This runs kbcore directly, so it only works on the OWNER's box
# where the repo exists and scope.OWNER applies. A machine that installed these
# hooks from the skill has no repo, falls through, and still gets the explicit
# "ask the KB for a token" message. A remote machine must never mint its own
# credentials (Invariant 12: authority comes from kbcore.scope, never from an
# argument). Verified by test: no repo -> no mint.
# NEVER auto-mint for a slug that came from $HOME, $TMPDIR, /tmp or a non-git
# dir (C4): PROJECT_MINTABLE is set by kb_pin_project and is 1 only for a real
# git-root project. A firing-derived or ephemeral slug must not spawn a token.
# THE MINT RUNS IN THE DETACHED CHILD (C5): the 45 s `uv run` used to sit in the
# hook parent, which is fine under a 300 s PreCompact budget and fatal under the
# 3 s Stop one. The parent only decides ELIGIBILITY (a bearer transport with no
# token at all, an owner repo, a mintable slug, and not a Stop/SessionEnd firing);
# the child mints, stores, then drains with the new token in the same breath.
MINT_ELIGIBLE=0
if [ -z "$PROJ_TOKEN" ] && [ -z "${KB_TOKEN:-}" ] && [ -z "${KB_FLUSH_NO_MINT:-}" ] &&
  [ -d "${KB_REPO:-/nonexistent}" ] && [ "${PROJECT_MINTABLE:-0}" = 1 ] &&
  { [ -n "${KB_MCP_URL:-}" ] || [ -n "${KB_REST_URL:-}" ]; }; then
  MINT_ELIGIBLE=1
fi
# The mint is DEFERRED, not refused, when the only thing standing between this
# firing and a mint is the event: the owner box (repo present), a bearer
# transport, no token yet, and a Stop/SessionEnd (KB_FLUSH_NO_MINT). The next
# PreCompact or Backfill firing decides (and mints, or says NEEDS TOKEN); saying
# NEEDS TOKEN / NOT CONFIGURED here would have the pre-brief tell the owner to
# mint by hand for a project the hook mints itself. Mintability of the slug is
# not consulted (it is the mint's own check, skipped under NO_MINT to spare the
# Stop parent a git call), so a $HOME-launched session gets its NEEDS TOKEN line
# from that later firing instead of from this one.
MINT_DEFERRED=0
if [ -z "$PROJ_TOKEN" ] && [ -z "${KB_TOKEN:-}" ] && [ -n "${KB_FLUSH_NO_MINT:-}" ] && [ -z "${KB_DIR:-}" ] &&
  [ -d "${KB_REPO:-/nonexistent}" ] && { [ -n "${KB_MCP_URL:-}" ] || [ -n "${KB_REST_URL:-}" ]; }; then
  MINT_DEFERRED=1 # never on a LOCAL box (KB_DIR wins the transport choice and needs no token)
fi
kb_mint_token() { # -> 0 with KB_TOKEN set (and stored, mode 600); 1 when nothing was minted
  [ "${MINT_ELIGIBLE:-0}" = 1 ] || return 1
  _mtd=$(cd "$KB_REPO" 2>/dev/null && kb_timeout 45 uv run python - "$PROJECT" "$(hostname 2>/dev/null || echo this-machine)" <<'MINTPY' 2>/dev/null
import sys
project, machine = sys.argv[1], sys.argv[2]
try:
    from kbcore import project_tokens, scope
    with scope.use_principal(scope.OWNER):
        print(project_tokens.issue(project, machine=machine, note="auto-minted by flush hook").token)
except Exception:
    sys.exit(1)
MINTPY
  )
  [ -n "$_mtd" ] || return 1
  (
    umask 077
    touch "$TOKENS" 2>/dev/null || true
    chmod 600 "$TOKENS" 2>/dev/null || true
    # Replace any stale line: issue() REVOKES the previous credential for this
    # (project, machine), so an old line would shadow the live one with a dead token.
    if [ -f "$TOKENS" ]; then
      grep -v "^KB_TOKEN_${PROJ_KEY}=" "$TOKENS" >"$TOKENS.t" 2>/dev/null && mv "$TOKENS.t" "$TOKENS"
    fi
    printf 'KB_TOKEN_%s=%s\n' "$PROJ_KEY" "$_mtd" >>"$TOKENS"
    chmod 600 "$TOKENS" 2>/dev/null || true
  )
  KB_TOKEN="$_mtd"
  export KB_TOKEN
  # The token is NEVER logged — only that one now exists.
  echo "$(date -Is) flush $PROJECT auto-minted a project token (stored in $TOKENS, mode 600)" >>"$LOG"
  return 0
}
export -f kb_mint_token
export TOKENS PROJ_KEY KB_REPO MINT_ELIGIBLE

if [ -n "$PROJ_TOKEN" ]; then
  KB_TOKEN="$PROJ_TOKEN"
fi

# A machine that has per-project tokens but none for THIS project is not
# misconfigured in general — it just has not been given access to this one yet.
# Say exactly that, and exactly how to fix it, rather than failing later with an
# opaque 403. Rate-limited like the other NOT CONFIGURED lines. Returns 1 when
# there is no token file at all (the generic NOT CONFIGURED notice applies then).
# Called by the parent when no mint is possible, by the child when one failed.
kb_no_token_notice() {
  [ -f "$TOKENS" ] || return 1
  _nts="$STATE/.notoken-$PROJECT.stamp"
  mkdir -p "$STATE" 2>/dev/null || true
  _ntn=$(kb_now)
  _ntl=$(cat "$_nts" 2>/dev/null || echo 0)
  case "$_ntl" in '' | *[!0-9]*) _ntl=0 ;; esac
  if [ "$((_ntn - _ntl))" -ge 3600 ]; then
    printf '%s\n' "$_ntn" >"$_nts" 2>/dev/null || true
    echo "$(date -Is) flush $PROJECT NEEDS TOKEN: no KB_TOKEN_$PROJ_KEY in $TOKENS and no shared KB_TOKEN, so this session was NOT captured. Ask the KB for one: kb_project_token(project=\"$PROJECT\", machine=\"$(hostname 2>/dev/null || echo this-machine)\") — then add the printed line to $TOKENS" >>"$LOG"
  fi
  return 0
}
export -f kb_no_token_notice
# NO transport is configured (no KB_DIR, no KB_MCP_URL/KB_REST_URL+KB_TOKEN, no
# CONNECTOR). Without this the hook fell off the end and exited 0 in SILENCE:
# the machine captured nothing, forever, with no signal anywhere — and because
# the SessionStart pre-brief surfaces failures by grepping THIS log, its warning
# stayed silent too. That breaks the skill's own never-silent-failure guarantee.
# Found by tests/hookrig case 12. Rate-limited to one line an hour so a
# deliberately-unconfigured box does not spam the log on every firing.
kb_notconfigured_notice() {
  _ncs="$STATE/.notconfigured.stamp"
  _ncn=$(kb_now)
  _ncl=$(cat "$_ncs" 2>/dev/null || echo 0)
  case "$_ncl" in '' | *[!0-9]*) _ncl=0 ;; esac
  if [ "$((_ncn - _ncl))" -ge 3600 ]; then
    printf '%s\n' "$_ncn" >"$_ncs" 2>/dev/null || true
    echo "$(date -Is) flush $PROJECT NOT CONFIGURED: no transport in $CFG (set KB_DIR, or KB_MCP_URL/KB_REST_URL + KB_TOKEN, or KB_CONNECTOR=1) — this session was NOT captured" >>"$LOG"
  fi
  return 0
}
export -f kb_notconfigured_notice
if [ "$MINT_DEFERRED" = 1 ]; then
  # Not a dropped-session token (FAILED / NEEDS TOKEN / NOT CONFIGURED): the
  # bytes stay pending behind the offset and the next mint-capable firing sends
  # them. One line per firing; an open Stop gate already rate-limits itself.
  echo "$(date -Is) flush $PROJECT no token yet: the auto-mint is deferred to the next PreCompact/Backfill firing (never on Stop/SessionEnd) ev=$EV sid=$SIDS" >>"$LOG"
  exit 0
fi
if [ -z "${KB_TOKEN:-}" ] && [ "${MINT_ELIGIBLE:-0}" != 1 ] && kb_no_token_notice; then
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
    kb_drain_context; kb_log_rotate   # git context + log bound: CHILD work, never the Stop parent (C5)
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
  [ "${KB_FLUSH_FOREGROUND:-0}" = 1 ] && wait
elif { [ -n "${KB_MCP_URL:-}" ] || [ -n "${KB_REST_URL:-}" ]; } && { [ -n "${KB_TOKEN:-}" ] || [ "${MINT_ELIGIBLE:-0}" = 1 ]; }; then
  # MCP or REMOTE: submit the delta over the network, CHUNKED. Large sessions
  # (up to 1 GB) are sent on an ABSOLUTE lattice cut at line boundaries (C2);
  # the per-session byte offset advances after EACH accepted chunk, so a failure
  # resumes mid-delta next firing. Chunks go via a temp file (kb_json_build's `f`
  # type reads it verbatim) — passing the text as an argv would hit the kernel's
  # ~128 KB per-argument limit long before 1 MB. STATE/OKEY/OFFF and the knobs
  # were computed once up top; the offset is NEVER deleted here.
  HELPER="$HOOKDIR/kb-mcp-call.sh"
  export KB_MCP_URL="${KB_MCP_URL:-}" KB_REST_URL="${KB_REST_URL:-}" KB_TOKEN OFFF OKEY HELPER EV
  "$DETACH" bash -c '
    kb_drain_context; kb_log_rotate   # git context + log bound: CHILD work, never the Stop parent (C5)
    # TOKEN AUTO-MINT lives HERE (C5), never in the hook parent. MINT_ELIGIBLE was
    # decided up top; a failed mint says NEEDS TOKEN / NOT CONFIGURED as before.
    if [ -z "$KB_TOKEN" ]; then
      kb_mint_token || { kb_no_token_notice || kb_notconfigured_notice; exit 0; }
    fi
    CHUNK="$KB_FLUSH_CHUNK"; SEND_MAX="$KB_FLUSH_SEND_MAX"; MAX_PER_FIRING="$KB_FLUSH_MAX"
    [ "$EV" = "SessionEnd" ] && MAX_PER_FIRING=1099511627776   # terminal firing: drain all
    _fsize() { stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || wc -c <"$1" | tr -d " "; }
    # ONE drain at a time. A held lock is NOT a loss any more — the offset is
    # never deleted, so the next Stop/PreCompact or the sweep resumes the delta.
    if ! kb_lock_take "$OKEY"; then
      if [ "$EV" = "SessionEnd" ]; then
        echo "$(date -Is) flush $PROJECT SessionEnd deferred (lock held by a running drain) — the next Stop or the sweep resumes it ev=$EV sid=$SIDS"
        # A deferred TERMINAL firing leaves a retry marker (n=0: nothing FAILED,
        # code=lock) so a resumed session finishes the tail on its first due Stop.
        [ -s "$OFF_RETRY" ] || { printf "%s 0 lock %s" "$(kb_now)" "$(_fsize "$T")" >"$OFF_RETRY.t" 2>/dev/null && mv "$OFF_RETRY.t" "$OFF_RETRY" 2>/dev/null; }
      elif [ "$EV" = "Backfill" ]; then
        # The sweep waits on this line: its verdict grep is `skipped (lock held)`
        # (kb-flush-backfill.sh, VERDICT=busy) -- keep the token byte-for-byte.
        # Stop/PreCompact stay silent here as before (the next firing resumes).
        echo "$(date -Is) flush $PROJECT Backfill skipped (lock held) ev=$EV sid=$SIDS"
      fi
      exit 0
    fi
    # .last = the Stop gate rate-limit clock, written by ANY drain start (C5).
    printf "%s" "$(kb_now)" >"$OFF_LAST.t" 2>/dev/null && mv "$OFF_LAST.t" "$OFF_LAST" 2>/dev/null
    SIZE=$(_fsize "$T"); case "$SIZE" in ""|*[!0-9]*) SIZE=0 ;; esac
    # SIZE=0 is UNREADABLE, not shrank (V1-m4): a failed stat must never clobber
    # a good checkpoint down to 0. Do not drain, do not write the offset.
    if [ "$SIZE" = 0 ]; then
      [ -f "$OFFF" ] && echo "$(date -Is) flush $PROJECT size unreadable ($SIZE) — not draining ev=$EV sid=$SIDS"
      exit 0
    fi
    # Decide OFFSET under the lock: branch seed, then trusted/unseeded, then
    # shrank. A refusal (unseeded pre-stamp transcript) returns 1 -> exit 0.
    BRANCH_OF=""
    kb_decide_offset || exit 0
    [ "$SIZE" -le "$OFFSET" ] && { kb_retry_clear "$OFFSET"; exit 0; }
    SENT=0; DUP=0; FAILED_THIS=0; HELD=0
    # RETRY MARKER (C6, Appendix D): <epoch> <n> <code> <end>. n counts
    # consecutive FAILED drains; <end> is where the FAILED chunk ended (its
    # offset + bytes, never the file size: a size recorded after the file grew
    # past the cell edge would make the next retry cut a DIFFERENT window and
    # hand the server a row overlapping the one it may already hold). <end>
    # caps this drain FIRST window, so a chunk whose 202 was lost on the wire
    # is re-cut byte-identically and the server answers duplicate:true (the
    # lattice guarantees the cut for every cell but the EOF one, which the cap
    # restores). A deferred SessionEnd writes the file size instead (nothing
    # was cut) -- the HELD-at-EOF clear below covers its trailing partial line.
    RETRY_N=0; RETRY_END=0
    if [ -s "$OFF_RETRY" ]; then
      read -r _re RETRY_N _rc RETRY_END _rx <"$OFF_RETRY" 2>/dev/null || true
      case "$RETRY_N" in ""|*[!0-9]*) RETRY_N=0 ;; esac
      case "$RETRY_END" in ""|*[!0-9]*) RETRY_END=0 ;; esac
    fi
    CAP1=0; [ "$RETRY_END" -gt "$OFFSET" ] && [ "$RETRY_END" -lt "$SIZE" ] && CAP1="$RETRY_END"
    # Probed in the CHILD so the parent never pays for it: curl >= 7.71 retries
    # connection failures only with --retry-all-errors. Safe ONLY because the
    # endpoint dedupes (see kb_rest_post) — never copy this to another call.
    KB_CURL_RETRY_ALL=""
    if [ -z "$KB_MCP_URL" ] && curl --help all 2>/dev/null | grep -q -- --retry-all-errors; then KB_CURL_RETRY_ALL=--retry-all-errors; fi
    set --   # git context, appended to every request body; empty fields omitted
    # THE SESSION IDENTITY. Every chunk of one session must carry the same value
    # or the server cannot tell that 45 rows are one conversation: it then runs
    # 45 separate 14B extractions instead of one, which is what let a 38 MB
    # session monopolise a 200-row nightly budget. OKEY is already the
    # sanitised session id (or the transcript path when there is none), so it is
    # stable across every firing for this session and unique between sessions.
    # BELT AND BRACES: never put a DEGENERATE identity on the wire. A bare
    # "session:" is not an identity, it is a wildcard — the server groups by it,
    # so every session that ever sent one would be concatenated with every other.
    # Omit the field entirely instead; the server then falls back to a per-row
    # key. (kbcore.ingest_queue also refuses a degenerate value at enqueue, so
    # this is two independent guards, not one.)
    [ -n "${OKEY:-}" ] && set -- "$@" session_uri s "session:$OKEY"
    [ -n "$REPO" ] && set -- "$@" repo s "$REPO"
    [ -n "$BRANCH" ] && set -- "$@" branch s "$BRANCH"
    [ -n "$COMMIT" ] && set -- "$@" commit s "$COMMIT"
    # hook_version rides ONLY the REST body: the REST request model ignores
    # fields it does not know (harmless today, fleet data the day the server
    # learns the field), while the MCP tool signature is CLOSED -- an unknown
    # argument there errors the call and would break every MCP-mode flush
    # against a current server. The transport is fixed for the whole loop
    # (KB_MCP_URL wins when set), so appending once here cannot leak into the
    # MCP arm.
    [ -z "$KB_MCP_URL" ] && [ -n "${HOOKVER:-}" ] && set -- "$@" hook_version s "$HOOKVER"
    while [ "$OFFSET" -lt "$SIZE" ] && [ "$SENT" -lt "$MAX_PER_FIRING" ]; do
      TMPC=$(mktemp) || { echo "$(date -Is) flush $PROJECT BUG: mktemp failed"; break; }
      # ABSOLUTE LATTICE (C2, Appendix A). The chunk end is the smallest lattice
      # point B_k > OFFSET, where B_k is the position after the last newline at or
      # before k*CHUNK measured from byte 0 — a function of the FILE only, so a
      # from-0 pass and a delta pass cut IDENTICAL chunks (bar the EOF/oversize
      # cells). Widen by one cell when the line at OFFSET straddles the edge.
      HELD=0
      LK=$(( OFFSET / CHUNK + 1 ))
      while :; do
        EDGE=$(( LK * CHUNK )); [ "$EDGE" -gt "$SIZE" ] && EDGE=$SIZE
        [ "$CAP1" -gt 0 ] && [ "$EDGE" -gt "$CAP1" ] && EDGE=$CAP1   # retry: the first window ends where the failed one did
        # tail feeds a reader that closes early (head); uutils coreutils tail (Ubuntu 25.10+,
        # the owner box) PANICS on that EPIPE and prints a Rust backtrace note to stderr —
        # the bytes are already delivered, so its stderr is dropped here (and below).
        tail -c +$((OFFSET+1)) "$T" 2>/dev/null | head -c $((EDGE - OFFSET)) >"$TMPC"
        BYTES=$(_fsize "$TMPC")
        [ "$BYTES" -gt 0 ] || { HELD=2; break; }
        if [ "$(tail -c1 "$TMPC")" = "" ]; then
          break                                        # window ends in \n: chunk end = B_k
        fi
        KEEP=$(kb_keep_to_last_nl "$TMPC")
        if [ "$KEEP" -gt 0 ]; then
          head -c "$KEEP" "$TMPC" >"$TMPC.k" && mv "$TMPC.k" "$TMPC"; BYTES=$KEEP
          break                                        # trimmed to the last \n < EDGE
        elif [ "$EDGE" -ge "$SIZE" ]; then
          HELD=2; break                                # partial last line at EOF: HELD on EVERY bearer event
        elif [ "$CAP1" -gt 0 ] && [ "$EDGE" -ge "$CAP1" ]; then
          CAP1=0                                       # the capped window holds no whole line: drop the cap, cut normally
        else
          LK=$((LK + 1))                               # line at OFFSET straddles the edge: widen one cell
        fi
      done
      CAP1=0                                           # only the FIRST window of a retry drain is capped
      if [ "$HELD" = 2 ]; then rm -f "$TMPC"; break; fi
      # OVERSIZE (> SEND_MAX): a > 250 KB straddle or a giant line. Trim to the
      # last \n <= OFFSET+SEND_MAX; if that leaves nothing, send OFFSET..+SEND_MAX
      # as an unterminated fragment (as today for a line longer than a chunk).
      if [ "$BYTES" -gt "$SEND_MAX" ]; then
        tail -c +$((OFFSET+1)) "$T" 2>/dev/null | head -c "$SEND_MAX" >"$TMPC"
        BYTES=$(_fsize "$TMPC")
        if [ "$(tail -c1 "$TMPC")" != "" ]; then
          KEEP=$(kb_keep_to_last_nl "$TMPC")
          if [ "$KEEP" -gt 0 ]; then
            head -c "$KEEP" "$TMPC" >"$TMPC.k" && mv "$TMPC.k" "$TMPC"; BYTES=$KEEP
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
        # CODE=err is deliberately OFF the transient set: the helper folds auth,
        # transport and tool rejections (bad project, queue full, oversize) into
        # one failure, so the retry marker and the Stop back-off are REST-only;
        # an MCP failure is resumed by the next PreCompact/SessionEnd/sweep.
        bash "$HELPER" kb_ingest_session "@$TMPC.args" >/dev/null 2>>"$LOG" && CODE=202 || CODE=err
        rm -f "$TMPC.args"
      else
        BODYF="$TMPC.body"
        # Flush telemetry stamps ride the REST body ONLY (the MCP tool signature
        # is closed). Per-chunk: numeric offset/bytes/size, the event, and the
        # per-firing box/mtime/pin/branch context. Built fresh each chunk because
        # offset/bytes change; empty-valued stamps are omitted so a value the
        # server schema pattern-checks is never sent blank.
        STAMPS="flush_offset n $OFFSET flush_bytes n $BYTES flush_size n $SIZE flush_mtime n $MTIME"
        [ -n "$EV" ] && STAMPS="$STAMPS flush_event s $EV"
        [ -n "$BOX" ] && STAMPS="$STAMPS flush_box s $BOX"
        [ -n "$PROJECT_BY" ] && STAMPS="$STAMPS flush_project_by s $PROJECT_BY"
        [ -n "$BRANCH_OF" ] && STAMPS="$STAMPS flush_branch_of s $BRANCH_OF"
        if command -v gzip >/dev/null 2>&1 && command -v base64 >/dev/null 2>&1 \
           && gzip -c "$TMPC" | base64 | tr -d "\n" >"$TMPC.gz64" && [ -s "$TMPC.gz64" ]; then
          kb_json_build "$BODYF" project s "$PROJECT" kind s session transcript_gz f "$TMPC.gz64" "$@" $STAMPS
          rm -f "$TMPC.gz64"
        else
          rm -f "$TMPC.gz64"
          kb_json_build "$BODYF" project s "$PROJECT" kind s session transcript f "$TMPC" "$@" $STAMPS
        fi
        if [ ! -s "$BODYF" ] || ! kb_json_valid "$BODYF"; then
          echo "$(date -Is) flush $PROJECT BUG: request body build failed (empty/invalid) at offset $OFFSET — NOT submitted, will retry"
          rm -f "$BODYF" "$TMPC"; break
        fi
        # kb_rest_post holds THE curl line (Appendix G: --max-time/--retry/
        # --retry-delay/--retry-max-time, no connect timeout, no fail flag). On a
        # transient answer (000, 5xx, 408, 429, 52x, 530) the SAME body is posted
        # ONCE more after 5 s — byte-identical, so a 202 that was lost on the
        # wire comes back duplicate:true. 401/403/4xx are configuration: never
        # retried, never a marker (Stop must not hammer a misconfigured box).
        ATTEMPT=0
        while :; do
          ATTEMPT=$((ATTEMPT + 1))
          CODE=$(kb_rest_post "$BODYF" "$TMPC.resp")
          # A flaked response read (000) whose body still carries a queue id was
          # ACCEPTED by the server (KB item 497) — count it as success.
          case "$CODE" in 2*) : ;; *) grep -q "queue_id" "$TMPC.resp" 2>/dev/null && CODE=202 ;; esac
          case "$CODE" in 000|5??|408|429|52?|530) [ "$ATTEMPT" -lt 2 ] || break; sleep 5 ;; *) break ;; esac
        done
        # Count chunks the server answered as already-present, for the dup= tally.
        # (Double-quoted pattern: this whole drain is one single-quoted bash -c, so
        # a single-quoted grep pattern with a space/glob would break the quoting.)
        case "$CODE" in 2*|202) grep -Eq "\"duplicate\": *true" "$TMPC.resp" 2>/dev/null && DUP=$((DUP + 1)) ;; esac
        rm -f "$BODYF" "$TMPC.resp"
      fi
      rm -f "$TMPC"
      case "$CODE" in
        2*|202)
          # W1. kb_offset_write re-checks ownership first: a lock lost mid-drain
          # (the holder was reclaimed) means another drain is authoritative now,
          # so it logs "lock lost — stopping" and exits rather than rewinding.
          OFFSET=$((OFFSET + BYTES)); kb_offset_write "$OFFSET"; SENT=$((SENT + BYTES)); kb_lock_touch ;;
        000|5??|408|429|52?|530)
          # Both chains failed: the marker records this drain (n+1), the code and
          # the failed chunk END (offset + bytes -- a capped retry that fails
          # again keeps the SAME end, so every retry re-cuts the same window);
          # the Stop gate backs off min(RETRY_MAX, BASE * 2^(n-1)), 429/503 the max.
          FAILED_THIS=1; RETRY_N=$((RETRY_N + 1))
          printf "%s %s %s %s" "$(kb_now)" "$RETRY_N" "$CODE" "$((OFFSET + BYTES))" >"$OFF_RETRY.t" 2>/dev/null && mv "$OFF_RETRY.t" "$OFF_RETRY" 2>/dev/null
          echo "$(date -Is) flush $PROJECT chunk FAILED ($CODE) at offset $OFFSET — will resume retry#$RETRY_N ev=$EV sid=$SIDS"; break ;;
        *) FAILED_THIS=1; echo "$(date -Is) flush $PROJECT chunk FAILED ($CODE) at offset $OFFSET — will resume ev=$EV sid=$SIDS"; break ;;
      esac
    done
    # The marker outlives every drain that did not reach the end it recorded. A
    # drain that stopped only because the trailing partial line is HELD at EOF
    # has sent every complete line there is: judge it on SIZE, or a marker that
    # recorded a file size ending mid-line (a deferred SessionEnd) would re-open
    # the Stop gate every turn until the line completes.
    if [ "$FAILED_THIS" != 1 ]; then
      if [ "$HELD" = 2 ]; then kb_retry_clear "$SIZE"; else kb_retry_clear "$OFFSET"; fi
    fi
    # Truthful summary: only a FULLY drained firing (OFFSET>=SIZE) logs the
    # " ok bytes=" success line the pre-brief matches. A firing that broke early
    # (chunk FAILED, held partial line, or per-firing cap) logs a "partial" line
    # that does NOT match that pattern — so a failed chunk followed by earlier
    # successes can no longer masquerade as success and strand the tail forever
    # on the terminal SessionEnd firing (HIGH-1).
    if [ "$SENT" -gt 0 ]; then
      if [ "$OFFSET" -ge "$SIZE" ]; then
        echo "$(date -Is) flush $PROJECT ok bytes=$SENT (offset $OFFSET/$SIZE) dup=$DUP ev=$EV sid=$SIDS"
      else
        echo "$(date -Is) flush $PROJECT partial bytes=$SENT (offset $OFFSET/$SIZE) — will resume dup=$DUP ev=$EV sid=$SIDS"
      fi
    fi
    # The offset is NEVER deleted on a terminal event (never-delete-offsets v2):
    # a drained SessionEnd leaves offset==SIZE and a resume continues from there.
    # Release the lock, then sweep dead state (transcript gone under both roots).
    kb_lock_release
    kb_state_sweep
  ' </dev/null >>"$LOG" 2>&1 &
  [ "${KB_FLUSH_FOREGROUND:-0}" = 1 ] && wait
elif [ "${KB_CONNECTOR:-0}" = "1" ] && [ -n "${CLAUDE_BIN:-}" ]; then
  # CONNECTOR: zero-config — uses the claude.ai KB connector bridged into
  # Claude Code (cloud-held OAuth; nothing local). A headless `claude -p` call
  # relays each chunk into kb_ingest_session. The MODEL is the data path, so
  # chunks are small (~24 KB echo fidelity) and each flush costs Claude usage —
  # bearer modes (MCP/REST) are the efficient path; this is the no-setup one.
  # STATE/OKEY/OFFF were computed once up top; the offset is NEVER deleted here.
  export CLAUDE_BIN OFFF OKEY EV
  export KB_CONNECTOR_CHUNK="${KB_CONNECTOR_CHUNK:-24000}"
  export KB_CONNECTOR_MAX="${KB_CONNECTOR_MAX:-240000}"
  # Same class as OKEY: the child bounds its relay with this, and an unexported
  # value meant a KB_CONNECTOR_TIMEOUT set in kb-flush.env was silently ignored
  # and every box quietly used 120.
  export KB_CONNECTOR_TIMEOUT="${KB_CONNECTOR_TIMEOUT:-120}"
  # An ABSOLUTE ceiling per firing, including the terminal one. SessionEnd used
  # to lift the cap to 1 TB so the tail could never be stranded — correct for a
  # normal session, catastrophic for a large one: at 24 KB per model call a
  # 300 MB transcript is ~13,000 headless `claude -p` invocations, i.e. days of
  # background work silently spending the user'"'"'s Claude usage, detached and
  # untracked. CONNECTOR is the zero-config transport, not the bulk one.
  export KB_CONNECTOR_SESSION_MAX="${KB_CONNECTOR_SESSION_MAX:-2000000}"
  "$DETACH" bash -c '
    export KB_HOOK_GUARD=1   # children (claude -p) must not re-trigger KB hooks
    kb_drain_context; kb_log_rotate   # git context + log bound: CHILD work, never the Stop parent (C5)
    _fsize() { stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || wc -c <"$1" | tr -d " "; }
    # Losing the lock mid-session is fine (the other firing is draining). On the
    # TERMINAL firing it is not: there is no next firing, so whatever the winner
    # does not reach is lost — and staying silent lets its " ok bytes=" line be
    # read as a fully captured session.
    if ! kb_lock_take "$OKEY"; then
      [ "$EV" = "SessionEnd" ] && echo "$(date -Is) connector $PROJECT NOT CAPTURED: the final firing could not take the session lock (another drain was still running), so any remaining bytes were not relayed — re-ingest $T if this session matters"
      # Same token the bearer arm logs, so a sweep on a CONNECTOR box reads busy.
      [ "$EV" = "Backfill" ] && echo "$(date -Is) connector $PROJECT Backfill skipped (lock held) ev=$EV sid=$SIDS"
      exit 0
    fi
    OFFSET=$(cat "$OFFF" 2>/dev/null || echo 0)
    SIZE=$(_fsize "$T"); case "$SIZE" in ""|*[!0-9]*) SIZE=0 ;; esac
    # A corrupt/empty checkpoint is NOT a licence to relay from 0 (KB 33783:
    # a from-0 resend is never automatic, on ANY arm): the same refusal,
    # marker and one-line-per-24 h limit as the bearer arm; the file is left
    # untouched for the owner --seed. A numeric one clears a stale marker.
    case "$OFFSET" in ""|*[!0-9]*) kb_unseeded_refuse "offset unreadable: $OFFF"; exit 0 ;; esac
    rm -f "$OFF_UNSEEDED"
    # SIZE=0 is UNREADABLE, not shrank (V1-m4): never clobber a good checkpoint
    # down to 0 on a failed stat. Same writer invariant as the bearer arms.
    if [ "$SIZE" = 0 ]; then
      [ -f "$OFFF" ] && echo "$(date -Is) connector $PROJECT size unreadable ($SIZE) — not draining ev=$EV sid=$SIDS"
      exit 0
    fi
    if [ "$SIZE" -lt "$OFFSET" ]; then
      echo "$(date -Is) connector $PROJECT transcript shrank ($SIZE < offset $OFFSET) reason=shrank — restarting from 0 ev=$EV sid=$SIDS"
      kb_lock_owned || { echo "$(date -Is) connector $PROJECT lock lost — stopping ev=$EV sid=$SIDS"; exit 0; }
      OFFSET=0
      printf "%s" 0 >"$OFFF.t" 2>/dev/null && mv "$OFFF.t" "$OFFF" 2>/dev/null || true
    fi
    [ "$SIZE" -le "$OFFSET" ] && exit 0
    SENT=0
    # THE SESSION IDENTITY (and the git context) on the CONNECTOR transport.
    # The bearer branches put these in the request body; here the data path is a
    # MODEL, so they have to travel as an instruction instead — but they must
    # travel. Without them every connector chunk reached the queue with no
    # source_uri, so a 40-chunk session became 40 degenerate singleton rows: 40
    # separate 14B extractions, no cohort, and no code_ref anchor on anything it
    # learned. That is the exact shape the OKEY export closed for MCP/REST, and
    # it stayed open here because nothing on this branch ever named the fields.
    # OKEY/REPO/BRANCH/COMMIT were already exported into this child; only the
    # prompt was silent.
    #
    # EMPTY FIELDS ARE OMITTED, never sent blank. A bare session: is not an
    # identity, it is a wildcard the server would group every session by. kbcore
    # normalises such a value away at enqueue, and this must not lean on that:
    # the field simply does not exist when there is nothing to put in it.
    #
    # ESCAPING. These values are interpolated into a natural-language prompt that
    # the model has to turn back into a JSON tool call, and a repo directory or
    # branch containing a space, a quote or a backslash would otherwise break the
    # JSON it emits. Same discipline as the bearer branches: never hand-write the
    # JSON — let kb_json_build encode it (it is exported into this child) and
    # hand the model the encoded object verbatim. jq pretty-prints, so the
    # newlines are folded to spaces to keep the prompt one line; only formatting
    # whitespace is affected, since every value is JSON-escaped by then.
    set --
    [ -n "${OKEY:-}" ] && set -- "$@" session_uri s "session:$OKEY"
    [ -n "$REPO" ] && set -- "$@" repo s "$REPO"
    [ -n "$BRANCH" ] && set -- "$@" branch s "$BRANCH"
    [ -n "$COMMIT" ] && set -- "$@" commit s "$COMMIT"
    META=""
    [ "$#" -gt 0 ] && META=$(kb_json_build - "$@" | tr "\n" " ")
    METAI=""
    [ -n "$META" ] && METAI=" Pass these EXTRA ARGUMENTS to the same tool call, copying every key and value from the JSON object below exactly as written, adding no key that is not in it and dropping none that is -- EXTRA ARGUMENTS (JSON): $META ."
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
      ADV=$(tail -c +$((NEWOFF+1)) "$T" 2>/dev/null | head -c "$KB_CONNECTOR_CHUNK" | head -1 | wc -c | tr -d " ")
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
      tail -c +$((OFFSET+1)) "$T" 2>/dev/null | head -c "$KB_CONNECTOR_CHUNK" >"$TMPC"
      BYTES=$(stat -c%s "$TMPC" 2>/dev/null || wc -c <"$TMPC")
      [ "$BYTES" -gt 0 ] || { rm -f "$TMPC"; break; }
      if [ "$(tail -c1 "$TMPC")" != "" ]; then
        KEEP=$(kb_keep_to_last_nl "$TMPC")
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
      OUT=$(kb_timeout "${KB_CONNECTOR_TIMEOUT:-120}" "$CLAUDE_BIN" -p "The stdin content is a session transcript delta. Call the kb_ingest_session tool (mcp__claude_ai_Knowledge_Base__kb_ingest_session, or any tool named kb_ingest_session) with project='"'"'$PROJECT'"'"', kind='"'"'session'"'"', transcript = the stdin content VERBATIM.$METAI Reply ONLY with the queue id number, or the single word DUPLICATE if the tool reports it was already queued, or FAILED."             --allowedTools "mcp__claude_ai_Knowledge_Base__kb_ingest_session,mcp__kb__kb_ingest_session"             <"$TMPC" 2>>"$LOG" | tail -1)
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
          kb_lock_owned || { echo "$(date -Is) connector $PROJECT lock lost — stopping ev=$EV sid=$SIDS"; exit 0; }
          OFFSET=$((OFFSET + BYTES)); printf "%s" "$OFFSET" >"$OFFF.t" && mv "$OFFF.t" "$OFFF"; SENT=$((SENT + BYTES)); kb_lock_touch ;;
        *[0-9]*)
          kb_lock_owned || { echo "$(date -Is) connector $PROJECT lock lost — stopping ev=$EV sid=$SIDS"; exit 0; }
          OFFSET=$((OFFSET + BYTES)); printf '%s' "$OFFSET" >"$OFFF.t" && mv "$OFFF.t" "$OFFF"; SENT=$((SENT + BYTES)); kb_lock_touch ;;
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
        echo "$(date -Is) connector $PROJECT ok bytes=$SENT (offset $OFFSET/$SIZE)$SKIPNOTE ev=$EV sid=$SIDS"
      elif [ "$EV" = "SessionEnd" ]; then
        echo "$(date -Is) connector $PROJECT NOT CAPTURED $((SIZE-OFFSET)) bytes (offset $OFFSET/$SIZE)$SKIPNOTE — SessionEnd is the FINAL firing for this session, so these bytes will NOT resume. Re-ingest $T, or switch to a bearer transport in $CFG"
      else
        echo "$(date -Is) connector $PROJECT partial bytes=$SENT (offset $OFFSET/$SIZE)$SKIPNOTE — will resume ev=$EV sid=$SIDS"
      fi
    fi
    [ "$OFFSET" -lt "$SIZE" ] && [ "$SENT" -ge "$CAP" ] && [ "$EV" != "SessionEnd" ] &&       echo "$(date -Is) connector $PROJECT per-firing cap reached ($CAP); $((SIZE-OFFSET)) bytes resume next firing"
    # Offset NEVER deleted (never-delete-offsets v2): release the lock, then sweep
    # dead state. The CONNECTOR transport has no bearer sweep, so a never-resumed
    # SessionEnd tail is covered by the NOT CAPTURED line above, not by a delete.
    kb_lock_release
    kb_state_sweep
  ' </dev/null >>"$LOG" 2>&1 &
  [ "${KB_FLUSH_FOREGROUND:-0}" = 1 ] && wait
else
  # NO transport is configured: say so, loudly and rate-limited (case 12).
  kb_notconfigured_notice
fi
exit 0

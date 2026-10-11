#!/usr/bin/env bash
# kb-flush-backfill.sh -- THE SWEEP: feed every Claude Code transcript's UNFLUSHED
# remainder on this box through the REAL flush hook (wave C item C7, KB 33784).
#
# WHY THIS EXISTS. The flush hook only ever sees the sessions that fire it. A
# terminal that was killed, an IDE that crashed, a laptop that slept through
# SessionEnd, a box whose project token arrived after the session ended -- all of
# that is knowledge sitting on disk that the KB has never been told about. The
# sweep walks the transcripts and hands each unflushed remainder to the hook.
#
# ACCOUNTING IS OFFSET-BASED ONLY. The hook's per-session checkpoint
# (state/<okey>.offset) is the single source of truth since the never-delete
# lineage (KB-FLUSH-LINEAGE never-delete-offsets v2): REMAIN = SIZE - offset. A
# transcript with NO checkpoint follows the HOOK'S OWN RULE (kb_decide_offset):
# when its first record post-dates state/.checkpoints-trusted-since it is `ready`
# at 0 -- the stamp proves the old, offset-deleting lineage never touched it, and
# a session killed before its first drain (most short ones, with the Stop gate
# closed) would otherwise never be swept. A no-checkpoint transcript that PREDATES
# the stamp, or carries no timestamp, is `unknown`: the old lineage may already
# have drained it, so nothing here ever sends it -- the owner seeds it (--seed) or
# pushes it deliberately (--push --include-unknown). The old `.backfill-done`
# ledger is INERT: neither read nor written, left as a relic.
#
# NOT A HOOK. install.sh drops it beside the hooks and registers NO event for
# it. Two triggers run `--auto`: the hooks' piggyback stamp (state/.sweep.trigger)
# and, on Linux, the kb-flush-sweep systemd --user timer install.sh renders. The
# owner runs the other modes by hand.
#
# WHAT IT DELIBERATELY DOES NOT DO. It reimplements nothing. Offsets, chunking,
# the absolute lattice, the session lock, per-project token selection (and the
# auto-mint), scrubbing, the transport and every retry rule belong to
# flush-session-to-kb.sh. This script decides WHICH transcripts still have bytes
# to send and feeds the hook ONE `Backfill` event per transcript. The event is
# never SessionEnd: the hook treats Backfill like any mid-session firing -- the
# per-firing cap is honoured, the trailing partial line is HELD, the offset is
# kept. Nothing here opens a socket or prints a credential.
#
#   kb-flush-backfill.sh                        # DRY RUN (default): the table, sends nothing
#   kb-flush-backfill.sh --push [--limit N] [--project P] [--include-unknown] [--no-quiet]
#   kb-flush-backfill.sh --auto                 # the timer / piggyback run: gated, silent, exit-code contract
#   kb-flush-backfill.sh --seed <sid> <bytes|current>   # OWNER: the only offset write the sweep performs
#   kb-flush-backfill.sh --status               # the table + sweep health
#
# EXIT CODES
#   0  ran: nothing to do, or everything fireable was fired. Per-transcript
#      outcomes live in the table, the summary and the flush log -- a failed
#      chunk is REPORTED, never turned into a non-zero exit the timer would count
#      as a unit failure every 30 min.
#   1  usage / configuration: a bad flag, no JSON tool, no hook, no kb-flush.env
#   2  refused: the hook-lineage TRIPWIRE, or a --seed the rules refuse
#   3  the sweep lock (state/.sweep.lock) is held by another run -- SILENT
#      under --auto, one line otherwise
#
# STATES (first match wins; the table names them)
#   skipped    no cwd in the first records and the directory slug decodes to
#              nothing that exists -- never guessed into a project
#   branch     no checkpoint, but a sibling transcript with the same first line
#              and a checkpoint exists -- the hook will seed at the shared prefix
#              and send only the fork's own bytes (KB_FLUSH_BRANCH_SCAN=0 turns
#              this off here exactly as it does in the hook)
#   unknown    no checkpoint AND (first record predates the trust stamp, or no
#              timestamp, or no stamp): never sent by --auto or a bare --push
#   stale-copy 0 < SIZE < offset AND another transcript of the SAME session id
#              (<root>/*/<sid>.jsonl: a session resumed from another directory,
#              KB 49064) is at least offset long and this file is a byte-prefix
#              of it (tail check) -- the frozen copy whose checkpoint the live
#              copy's flushes carried past it. NEVER fired and never restarted
#              (the hook skips it too); not counted as unflushed. The reason
#              names the covering copy's directory
#   diverged   0 < SIZE < offset, a copy reaches the checkpoint but this file's
#              tail DIFFERS from it (the copy kept growing after the other
#              carried the shared checkpoint past it) -- or no copy reaches it
#              and state/<okey>.diverged names this file -- or that marker names
#              it at ANY size (grown to the checkpoint or past it: the
#              checkpoint is another copy's, a drain from it would start
#              mid-record). Its records past the shared prefix were never sent
#              and the shared checkpoint cannot carry them: NEVER fired, never
#              restarted, counted as unflushed (REMAIN is the whole file -- the
#              unique bytes are unknown without a full compare). The reason
#              names what the owner can do: while another copy reaches the
#              checkpoint only moving this copy out settles it (a --seed is
#              undone by the next pass); with none, --seed current ACCEPTS the
#              loss -- it never means the records were sent. A marked path
#              that now carries the checkpoint holder's prefix (moved out,
#              then the session resumed there) is RELEASED, not diverged: it
#              tables ready/flushed, and --push/--auto clear the marker
#   behind     0 < SIZE < offset, no copy reaches the checkpoint, and the file
#              is FROZEN (last modified before the checkpoint's last write) or
#              state/<okey>.behind names it -- or that marker names it at ANY
#              size -- the frozen copy of a session whose live copy is gone,
#              never a rewrite. NEVER fired, never restarted; an OWNER decision
#              (--seed), own count. REMAIN = SIZE - the marker's frozen= (0
#              while frozen; a copy RESUMED since it was marked holds that many
#              unsent bytes, counted as unflushed, and its reason names the
#              exact seed --seed <sid> <frozen> -- never `current`)
#   shrank     0 < SIZE < offset, no copy covers it, no marker names it and the
#              file was modified after the checkpoint's last write (a genuine
#              rewrite): the hook restarts from 0 (logged reason=shrank)
#   flushed    offset >= SIZE: nothing to send
#   busy       the session lock is held (a drain is running right now)
#   live       modified within KB_SWEEP_QUIET_SECONDS -- someone is typing
#   tail-held  the remainder is a single unterminated line the hook would hold
#   stalled    --auto ONLY: KB_SWEEP_STALL_RUNS consecutive runs without
#              progress; retried every KB_SWEEP_STALL_BACKOFF seconds, never
#              abandoned. An explicit --push is the owner's retry: it ignores
#              the back-off (and, with --no-quiet, the quiet window too).
#   ready      bytes past the checkpoint, fireable -- or NO checkpoint with a
#              first record newer than the trust stamp (sent from 0, the hook's
#              own rule; the hook writes the 0 itself, noclobber)
#
# TRIPWIRE. Before firing anything, the hook this script will exec must carry
# `# KB-FLUSH-LINEAGE: never-delete-offsets v2` on its line 2 and must not
# contain `rm -f "$OFFF"`. An older hook deletes the checkpoint after a drained
# SessionEnd, which is exactly the lineage that made every resume re-send from 0
# (KB 33783); feeding it a sweep would resend whole transcripts. One `BUG:` line,
# exit 2, zero requests.
#
# --seed <sid> <bytes|current>. Writes the checkpoint by hand, snapped DOWN to
# the last newline at or before <bytes> (`current` = the transcript's size). On
# an OLD-hook box (relative lattice) seed only at a logged chunk end or at
# `current`; on a NEW-hook box (absolute lattice) a low seed costs at most one
# extra row -- the server refuses on-lattice resends. Refused while the session
# lock is held, when <bytes> exceeds the size, and under --auto. A session with
# several transcripts (resumed from another directory, KB 49064) is seeded from
# its LARGEST copy, named on stdout -- never from a stale one. A seed is the
# owner's decision for the WHOLE session: it clears the session's diverged /
# behind markers (and their log stamps), said on stdout. It settles a `behind`
# copy (seeded at the marker's frozen= size once that copy was resumed) or a
# `diverged` one that NO copy reaches. A diverged copy that another copy still
# covers is judged from the files, not the marker: the next sweep pass or
# firing marks it again, so the seed prints a `note:` line -- only moving that
# copy out of ~/.claude/projects settles it.
#
# MARKERS (KB 49064). state/<okey>.diverged and state/<okey>.behind, ONE line
# `<iso-utc> <path> size=<SIZE> checkpoint=<OFFSET>[ cover=<copy>] frozen=<F>`,
# written by the hook on every refusing firing and by --push/--auto for the
# rows they table diverged/behind (a dry run or --status writes nothing).
# frozen= (LAST) is the path's size when a marker FIRST named it, carried by
# every rewrite naming the same path (a marker without it lends its size=):
# where a resumed copy's unsent records begin. The path a marker names is
# refused by the hook at ANY size and never fired here. notify and the brief
# surface them. Cleared ONLY by the hook's 30-day state sweep (with the
# offset), by --seed for that session, and by a hook firing or a --push/--auto
# pass that finds the path the marker names gone (one log line).
#
# KNOBS (ENVIRONMENT only, all optional -- this script never sources
# ~/.claude/kb-flush.env, and the rendered timer unit passes nothing but PATH, so
# under the timer these are the defaults; the hook reads kb-flush.env itself).
# kb-flush.env must NOT pin KB_FLUSH_MAX or KB_FLUSH_ALLOW_UNSEEDED: the hook
# sources that file BEFORE it applies the values the sweep exports per firing
# (the --auto byte budget rides on KB_FLUSH_MAX; --push --include-unknown on
# KB_FLUSH_ALLOW_UNSEEDED), so a line there silently overrides both.
#   KB_SWEEP_QUIET_SECONDS   300        skip transcripts modified this recently
#   KB_SWEEP_BUDGET_BYTES    67108864   --auto: bytes drained per run (64 MB)
#   KB_SWEEP_MAX_TRANSCRIPTS 20         --auto: transcripts fired per run
#   KB_SWEEP_MAX_SECONDS     1500       --auto: wall-clock budget per run
#   KB_SWEEP_STALL_RUNS      6          no-progress runs before ONE STALLED line
#   KB_SWEEP_STALL_BACKOFF   21600      seconds between retries once stalled
#   KB_SWEEP_INTERVAL        1800       0 = the sweep is OFF (--auto exits 0)
#   KB_BACKFILL_TIMEOUT      600        per-transcript ceiling on the wait
#   KB_BACKFILL_NOW          (unset)    epoch override for the clock (the rig)
#   KB_BACKFILL_PROJECTS / _STATE / _HOOK / _LOG   the paths (see below)
set -uo pipefail

# The anti-recursion guard belongs to the HOOKS; this script is invoked by a
# PERSON or a timer. Inheriting KB_HOOK_GUARD (the connector relay exports it
# into its `claude -p` child) would make every firing below exit 0 in silence --
# the exact "captured nothing, said nothing" failure this tool exists to repair.
unset KB_HOOK_GUARD

# >>> KB PORTABLE BLOCK v1 — BYTE-IDENTICAL in install.sh, flush-session-to-kb.sh,
# kb-session-brief.sh, kb-mcp-call.sh and kb-flush-backfill.sh. tests/hookrig/
# cases/26 extracts it from all five and diffs, so never hand-edit one copy. It exists because a locked-down
# box (no sudo, no jq) used to be a dead end: install.sh aborted with "apt install
# jq" and both hooks exited 0 in SILENCE, capturing nothing forever.
# Provides: platform detection, native-path conversion, a TRUSTWORTHY timeout, a
# detach command, and a JSON backend chain -- jq on PATH, a jq co-located with the
# hooks, $KB_JQ, then python3 -- plus the loud, sudo-free failure when none exist.

# Git Bash/MSYS/Cygwin spawned as a NON-login shell (Claude Code running a raw
# usr\bin\bash.exe) inherits the WINDOWS PATH: no /usr/bin, no /mingw64/bin, a
# native GnuWin32 tr first -- so uname, cut, tr, awk and grep miss or misbehave
# (KB 40411). Put the MSYS dirs back first. $OSTYPE is bash's own, so this needs
# no external binary; on Linux/macOS it is a no-op.
case "${OSTYPE:-}" in msys* | cygwin*)
  for _kbpd in /usr/bin /mingw64/bin; do [ -d "$_kbpd" ] && PATH="$_kbpd:$PATH"; done
  export PATH
  ;;
esac
KB_UNAME=$(uname -s 2>/dev/null || echo unknown)
# $OSTYPE first: uname is itself a /usr/bin binary, so under that PATH it was
# missing and KB_UNAME read `unknown` -- a uname-only test is circular there.
kb_is_windows() {
  case "${OSTYPE:-}" in msys* | cygwin*) return 0 ;; esac
  case "$KB_UNAME" in MINGW* | MSYS* | CYGWIN*) return 0 ;; *) return 1 ;; esac
}

# Native Windows path (C:\x or C:/x) -> POSIX. cygpath when it exists, else a
# PURE-BASH fallback (C:\Users\First Last\p -> /c/Users/First Last/p) so a Git
# Bash without cygpath still resolves event paths. Anything else (a UNC
# \\server\share, an already-POSIX /c/x) passes through. No cut/tr: MSYS
# rewrites a native tr's lone `/` argument to `C:/Program Files/Git/`, which
# garbled every transcript path (KB 40411). ${x,} is bash 4+ -- Git Bash ships
# 5.x, and only a drive-letter path ever reaches that line.
kb_winpath() {
  case "${1:-}" in [A-Za-z]:\\* | [A-Za-z]:/*) ;; *)
    printf '%s' "${1:-}"
    return 0
    ;;
  esac
  _kbp=""
  if command -v cygpath >/dev/null 2>&1; then _kbp=$(cygpath -u "$1" 2>/dev/null) || _kbp=""; fi
  if [ -z "$_kbp" ]; then
    _kbp=${1//\\//}
    _kbp=${_kbp,}
    _kbp="/${_kbp:0:1}${_kbp:2}"
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
# The hook's state dir under the name the shared blocks below read it by.
STATE="$BF_STATE"
# Ceiling on the wait for ONE transcript's detached drain: a 38 MB session at
# 700 KB a chunk is minutes of work. The normal path returns the moment the
# drain releases the session lock; this only bounds a wedged run.
BF_TIMEOUT="${KB_BACKFILL_TIMEOUT:-600}"
BF_QUIET="${KB_SWEEP_QUIET_SECONDS:-300}"
BF_BUDGET="${KB_SWEEP_BUDGET_BYTES:-67108864}"
BF_MAXN="${KB_SWEEP_MAX_TRANSCRIPTS:-20}"
BF_MAXSEC="${KB_SWEEP_MAX_SECONDS:-1500}"
BF_STALL_RUNS="${KB_SWEEP_STALL_RUNS:-6}"
BF_STALL_BACKOFF="${KB_SWEEP_STALL_BACKOFF:-21600}"
BF_SEND_MAX="${KB_FLUSH_SEND_MAX:-950000}"
# THE CLOCK. One value for the whole run; the rig pins it (KB_BACKFILL_NOW) so
# the quiet window and the stall back-off can be asserted without sleeping.
NOW="${KB_BACKFILL_NOW:-$(date +%s 2>/dev/null || echo 0)}"
# The in-memory index is US-separated (0x1F), NOT tab-separated. Bash treats tab
# as IFS *whitespace*, so `IFS=<tab> read` COLLAPSES a run of tabs into one
# delimiter: an empty column shifts every column after it left. That silently ate
# the skip REASON -- the one field whose whole job is to explain why a transcript
# was not pushed. 0x1F is not IFS whitespace, so empty columns survive, and no
# path or project slug can contain it.
SEP=$(printf '\037')
BF_LINEAGE='# KB-FLUSH-LINEAGE: never-delete-offsets v2'

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

# >>> KB SESSION COPIES v1
# The OTHER transcripts of one session (KB 49064). Resuming a session from
# another directory leaves TWO files with ONE session id -- the frozen copy in
# the old project dir and the live one in the new -- and both share the
# session's checkpoint (state/<okey>.offset). A copy is "<root>/*/<sid>.jsonl"
# under the projects roots the hooks know: $HOME/.claude/projects,
# ${CLAUDE_CONFIG_DIR}/projects when that is set (the two kb_state_sweep
# probes) and an optional extra root (the sweep passes its --projects dir);
# one directory level, the judged transcript itself excluded. A glob and a
# stat per match. Copied BYTE-IDENTICAL into kb-flush-backfill.sh and pinned by
# tests/test_hook_stale_copy.py, so the hook and the sweep can never disagree
# about what a copy is.
kb_session_copies() { # <sid> <judged transcript> [extra root] -> "<size> <path>" per copy
  case "${1:-}" in '' | *[!A-Za-z0-9._-]*) return 0 ;; esac
  _ksr2=""
  [ -n "${CLAUDE_CONFIG_DIR:-}" ] && _ksr2="${CLAUDE_CONFIG_DIR%/}/projects"
  _ksseen="|"
  for _ksr in "$HOME/.claude/projects" "$_ksr2" "${3:-}"; do
    [ -n "$_ksr" ] && [ -d "$_ksr" ] || continue
    case "$_ksseen" in *"|$_ksr|"*) continue ;; esac
    _ksseen="$_ksseen$_ksr|"
    for _ksc in "$_ksr"/*/"$1.jsonl"; do
      [ -f "$_ksc" ] || continue
      [ "$_ksc" = "${2:-}" ] && continue
      [ "$_ksc" -ef "${2:-}" ] && continue
      _kss=$(stat -c%s "$_ksc" 2>/dev/null || stat -f%z "$_ksc" 2>/dev/null || wc -c <"$_ksc" | tr -d ' ')
      case "$_kss" in '' | *[!0-9]*) continue ;; esac
      printf '%s %s\n' "$_kss" "$_ksc"
    done
  done
  return 0
}
# The LARGEST copy whose size reaches <offset>: "<size> <path>"; status 1 when
# none does. A copy that reaches the checkpoint is the one whose flushes carried
# it there, so a transcript BEHIND that checkpoint is the frozen copy -- never a
# shrink to restart from 0.
kb_stale_cover() { # <sid> <judged transcript> <offset> [extra root]
  _kcbs=-1
  _kcbp=""
  while IFS= read -r _kcl; do
    _kcs="${_kcl%% *}"
    case "$_kcs" in '' | *[!0-9]*) continue ;; esac
    [ "$_kcs" -ge "${3:-0}" ] && [ "$_kcs" -gt "$_kcbs" ] && {
      _kcbs=$_kcs
      _kcbp="${_kcl#* }"
    }
  done <<KBCOPIES
$(kb_session_copies "${1:-}" "${2:-}" "${4:-}")
KBCOPIES
  [ -n "$_kcbp" ] || return 1
  printf '%s %s' "$_kcbs" "$_kcbp"
}
# Is <stale> a byte-prefix of <copy>? Judged cheaply: its last min(4096, size)
# bytes against the copy's bytes at the same position. Neither file is read
# whole -- `tail -c N` seeks from the end, `tail -c +K` seeks to K on a regular
# file and `head -c` stops it after N bytes (the EPIPE that ends tail is
# expected: uutils' tail panics on it, so its stderr is dropped). cksum is
# POSIX: GNU, uutils, BSD/macOS and Git Bash all carry it.
kb_tail_matches() { # <stale> <stale size> <copy> -> 0 when the tails agree
  case "${2:-}" in '' | *[!0-9]* | 0) return 1 ;; esac
  _ktn=$(($2 < 4096 ? $2 : 4096))
  _kta=$(tail -c "$_ktn" "$1" 2>/dev/null | cksum 2>/dev/null)
  _ktb=$(tail -c +"$(($2 - _ktn + 1))" "${3:-}" 2>/dev/null | head -c "$_ktn" 2>/dev/null | cksum 2>/dev/null)
  # cksum prints "<crc> <bytes>": both sides must have read the FULL window, or a
  # pair of failed/short reads (cksum of nothing == cksum of nothing) would agree.
  case "$_kta" in *" $_ktn") ;; *) return 1 ;; esac
  [ "$_kta" = "$_ktb" ]
}
# Does <t> carry <copy>'s bytes just below <pos>? The window [pos-n, pos),
# n = min(4096, pos), of each file by cksum: the same seek-and-cut as
# kb_tail_matches (tail's EPIPE stderr dropped), neither file read whole. A
# CONTENT check, never an inode one (inodes are unreliable on Git Bash): a
# copy that diverged below <pos> differs inside that window.
kb_window_matches() { # <t> <pos> <copy> -> 0 when both hold the same bytes in [pos-n, pos)
  case "${2:-}" in '' | *[!0-9]* | 0) return 1 ;; esac
  [ -f "${1:-}" ] && [ -f "${3:-}" ] || return 1
  _kwn=$(($2 < 4096 ? $2 : 4096))
  _kwa=$(tail -c +"$(($2 - _kwn + 1))" "$1" 2>/dev/null | head -c "$_kwn" 2>/dev/null | cksum 2>/dev/null)
  _kwb=$(tail -c +"$(($2 - _kwn + 1))" "$3" 2>/dev/null | head -c "$_kwn" 2>/dev/null | cksum 2>/dev/null)
  case "$_kwa" in *" $_kwn") ;; *) return 1 ;; esac # a full window, never a short read
  [ "$_kwa" = "$_kwb" ]
}
# Epoch mtime of <file> in whole seconds; NOTHING (status 1) when it cannot be
# read. GNU/uutils `stat -c %Y`, BSD/macOS `stat -f %m`, then `date -r`.
kb_mtime_s() { # <file> -> epoch seconds
  _kms=$(stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || date -r "$1" +%s 2>/dev/null)
  case "$_kms" in '' | *[!0-9]*) return 1 ;; esac
  printf '%s' "$_kms"
}
# FROZEN behind its checkpoint: <transcript> was last modified strictly BEFORE
# <offset file> was last written (whole seconds) -- the checkpoint moved after
# this file last changed, so it is a frozen copy, not a rewritten transcript.
# 0 = frozen, 1 = modified at/after that write (a genuine rewrite), 2 = an mtime
# could not be read (the caller keeps the shrink rules and says so).
kb_frozen_behind() { # <transcript> <offset file>
  _kfa=$(kb_mtime_s "$1") || return 2
  _kfb=$(kb_mtime_s "$2") || return 2
  [ "$_kfa" -lt "$_kfb" ]
}
# The per-session markers <okey>.diverged / <okey>.behind are ONE line:
#   <iso-utc> <path> size=<SIZE> checkpoint=<OFFSET>[ cover=<copy>] frozen=<F>
# frozen= is the size the path had when a marker FIRST named it, carried
# forward by every rewrite that names the same path (a marker written before
# the field existed lends its size=): where a resumed copy's UNSENT records
# begin, so the owner's seed is exact. It is the LAST field, so a reader that
# cuts the path at ' size=' is unchanged.
kb_marker_write() { # <marker file> <path> <size> <checkpoint> [cover]
  _kmf=$3
  if _kmo=$(kb_marker_path "$1") && { [ "$_kmo" = "$2" ] || [ "$_kmo" -ef "$2" ]; }; then
    _kmf=$(kb_marker_frozen "$1") || _kmf=$3
  fi
  printf '%s %s size=%s checkpoint=%s%s frozen=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)" \
    "$2" "$3" "$4" "${5:+ cover=$5}" "$_kmf" >"$1.t" 2>/dev/null && mv "$1.t" "$1" 2>/dev/null ||
    rm -f "$1.t" 2>/dev/null
  return 0
}
kb_marker_frozen() { # <marker file> -> its frozen= bytes (a pre-frozen marker: size=); status 1: none
  [ -f "$1" ] || return 1
  _kmz=""
  IFS= read -r _kmz <"$1" 2>/dev/null || [ -n "$_kmz" ] || return 1
  _kmq=""
  case "$_kmz" in *" frozen="*) _kmq="${_kmz##* frozen=}" ;; esac
  case "$_kmq" in '' | *[!0-9]*)
    _kmq="${_kmz##* size=}"
    _kmq="${_kmq%% *}"
    ;;
  esac
  case "$_kmq" in '' | *[!0-9]*) return 1 ;; esac
  printf '%s' "$_kmq"
}
kb_marker_path() { # <marker file> -> the transcript path it names (status 1: none)
  [ -f "$1" ] || return 1
  _kmp=""
  IFS= read -r _kmp <"$1" 2>/dev/null || [ -n "$_kmp" ] || return 1
  _kmp="${_kmp#* }"
  _kmp="${_kmp%% size=*}"
  [ -n "$_kmp" ] || return 1
  printf '%s' "$_kmp"
}
kb_marker_names() { # <state dir> <okey> <transcript> -> diverged | behind (status 1: neither)
  for _kmk in diverged behind; do
    _kmn=$(kb_marker_path "$1/$2.$_kmk") || continue
    if [ "$_kmn" = "$3" ] || [ "$_kmn" -ef "$3" ]; then
      printf '%s' "$_kmk"
      return 0
    fi
  done
  return 1
}
# THE SHRINK VERDICT for a transcript BEHIND its session's checkpoint
# (0 < SIZE < OFFSET), one function so the hook and the sweep always agree.
# Sets KB_SV and KB_SV_WHY / KB_SV_COVER:
#   stale-copy  a copy reaches the checkpoint and this file is its byte-prefix
#   diverged    a copy reaches it but the tails differ (WHY=cover), or no copy
#               does and <okey>.diverged names this file (WHY=marker)
#   behind      no copy reaches it and <okey>.behind names this file
#               (WHY=marker), or the file is FROZEN (WHY=frozen)
#   shrank      none of these (WHY=none, or mtime-unreadable when the frozen
#               check could not run): the shrink rules apply unchanged
# A path a marker names is never restarted from 0, even after it is resumed,
# and never drained from the shared checkpoint once it grows past it (the
# callers refuse a marked path at ANY size: that checkpoint is another copy's)
# -- unless a .diverged path now carries the checkpoint holder's prefix
# (kb_window_matches: the owner moved that copy out and the session was later
# resumed there, so the file at that path is the live transcript again).
kb_shrink_verdict() { # <sid> <transcript> <size> <offset> <offset file> <state dir> <okey> [extra root]
  KB_SV=shrank
  KB_SV_WHY=none
  KB_SV_COVER=""
  if _ksv=$(kb_stale_cover "${1:-}" "${2:-}" "${4:-0}" "${8:-}"); then
    KB_SV_COVER="${_ksv#* }"
    KB_SV_WHY=cover
    KB_SV=diverged
    kb_tail_matches "${2:-}" "${3:-}" "$KB_SV_COVER" && KB_SV=stale-copy
    return 0
  fi
  if _ksm=$(kb_marker_names "${6:-}" "${7:-}" "${2:-}"); then
    KB_SV=$_ksm
    KB_SV_WHY=marker
    return 0
  fi
  kb_frozen_behind "${2:-}" "${5:-}"
  case $? in
    0)
      KB_SV=behind
      KB_SV_WHY=frozen
      ;;
    2) KB_SV_WHY=mtime-unreadable ;;
  esac
  return 0
}
# <<< KB SESSION COPIES v1

MODE=dry
ONLY=""
LIMIT=0
INCLUDE_UNKNOWN=0
NO_QUIET=0
SEED_SID=""
SEED_AT=""

usage() {
  cat <<'USAGE'
kb-flush-backfill.sh -- the sweep: push every Claude Code transcript's unflushed
remainder into the Knowledge Vault, through the flush hook installed on this box.

  (no flags)          DRY RUN: one row per transcript, plus totals. Sends nothing.
  --push              fire the flush hook for every fireable transcript, oldest first
  --auto              the timer's mode: --push with every gate on and EMPTY stdout
                      (quiet window, budgets, stall back-off, sweep lock -> exit 3)
  --seed <sid> <bytes|current>
                      write a session's checkpoint by hand, snapped DOWN to the
                      last newline at or before <bytes> (`current` = the size).
                      On an OLD-hook box seed at a logged chunk end or `current`;
                      on a NEW-hook box a low seed costs at most one extra row.
                      Refused under a held session lock, past the size, under --auto.
                      Several copies of one session: seeds from the largest.
  --status            the table plus sweep health (last run, stalls, trigger, lock)
  --project <slug>    only transcripts that land in this project
  --limit <n>         --push: fire at most n transcripts this run
  --include-unknown   --push: ALSO fire `unknown` transcripts (no checkpoint AND
                      older than the trust stamp, or undatable) from 0.
                      DISCOURAGED: such a transcript may already have been
                      drained by an older hook, and only on-lattice resends are
                      refused by the server -- prefer --seed. Never under --auto.
                      (A no-checkpoint transcript NEWER than the stamp is simply
                      `ready`: the hook's own rule sends it from 0.)
  --no-quiet          --push: ignore the KB_SWEEP_QUIET_SECONDS window. The stall
                      back-off is --auto-only: any --push retries a stalled one.
  --hook <path>       flush hook to fire   (default ~/.claude/hooks/flush-session-to-kb.sh)
  --projects <dir>    where transcripts live (default ~/.claude/projects)
  --timeout <secs>    per-transcript ceiling on the wait for the hook's drain
  -h, --help          this text

Exit codes: 0 ran, 1 usage/config, 2 refused (tripwire, --seed), 3 sweep lock held.
Re-running is safe: the hook owns the checkpoints and advances them per accepted
chunk, so a transcript already drained has nothing left to send.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --push) [ "$MODE" = auto ] || MODE=push ;;
    --auto) MODE=auto ;;
    --status) MODE=status ;;
    --seed)
      # Guard the shift: with fewer than two operands bash would print its own
      # `shift count out of range` before the usage line.
      [ "$#" -ge 3 ] || {
        printf 'kb-flush-backfill: --seed needs <sid> <bytes|current>\n' >&2
        exit 1
      }
      [ "$MODE" = auto ] || MODE=seed
      SEED_SID="$2"
      SEED_AT="$3"
      shift 2
      ;;
    --include-unknown) INCLUDE_UNKNOWN=1 ;;
    --no-quiet) NO_QUIET=1 ;;
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
      exit 1
      ;;
  esac
  shift
done
case "$LIMIT" in '' | *[!0-9]*)
  printf 'kb-flush-backfill: --limit takes a number, got [%s]\n' "$LIMIT" >&2
  exit 1
  ;;
esac
for _v in BF_TIMEOUT:600 BF_QUIET:300 BF_BUDGET:67108864 BF_MAXN:20 BF_MAXSEC:1500 \
  BF_STALL_RUNS:6 BF_STALL_BACKOFF:21600 BF_SEND_MAX:950000 NOW:0; do
  _n="${_v%%:*}"
  _d="${_v##*:}"
  eval "_c=\"\${$_n}\""
  case "$_c" in '' | *[!0-9]*) eval "$_n=$_d" ;; esac
done
# --seed under --auto is refused BEFORE anything else: the timer must never
# write a checkpoint, whatever argv a misrendered unit hands it.
if [ "$MODE" = auto ] && [ -n "$SEED_SID" ]; then
  printf 'kb-flush-backfill: --seed is refused under --auto (the timer never writes a checkpoint)\n' >&2
  exit 2
fi
if [ "$MODE" = auto ] && [ "${KB_SWEEP_INTERVAL:-1800}" = 0 ]; then
  exit 0 # the documented OFF switch: nothing to be late against
fi

# No JSON backend means we can neither read a transcript's own cwd nor build the
# event the hook parses. Say so the sudo-free way rather than guessing.
kb_json_probe || {
  kb_nojson_hint >&2
  exit 1
}
mkdir -p "$STATE" 2>/dev/null || true

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

# THE SAME state the flush hook uses: $STATE/<okey>.offset, where <okey> is the
# session id sanitised exactly as the hook sanitises it (its OKEY line).
bf_okey() { # <session id> -> the hook's offset-file key
  printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_' | tail -c 120
}
bf_sid8() { printf '%s' "$1" | cut -c1-8; } # the ` sid=` tag the hook logs

bf_offset_raw() { # <okey> -> the checkpoint's text, or `none` when there is no file
  [ -f "$STATE/$1.offset" ] || {
    printf 'none'
    return 0
  }
  cat "$STATE/$1.offset" 2>/dev/null
}
bf_offset() { # <okey> -> checkpointed bytes as a number (0 when absent/corrupt)
  _bfo=$(bf_offset_raw "$1")
  case "$_bfo" in '' | *[!0-9]*) _bfo=0 ;; esac
  printf '%s' "$_bfo"
}

# Is the session lock free? Probe with the hook's OWN trio in a subshell: the
# take-and-release happens there, so the trio's traps never touch this shell
# and a mkdir-path dir is removed on subshell exit (its pid file is $$).
bf_lock_free() { # <okey> -> 0 when nobody is draining this session
  (kb_lock_take "$1") 2>/dev/null
}
# The same question WITHOUT the trio's side effects (a take creates the flock
# file; on the mkdir path it may reclaim a dead-pid stale dir). DRY RUN and
# --status announce that they move nothing, so they read the trio's own rules
# instead: flock -> held iff the file exists and is locked; mkdir -> held iff
# the dir exists and its pid is alive, or is dead but younger than the stale
# window the trio would wait out before reclaiming.
bf_lock_free_ro() { # <okey> -> 0 when nobody is draining this session (read-only)
  _rof="$STATE/${1:-default}.offset.lock"
  _rod="$STATE/${1:-default}.offset.lock.d"
  if command -v flock >/dev/null 2>&1; then
    [ -e "$_rof" ] || return 0
    (
      exec 9<"$_rof"
      flock -n -s 9
    ) 2>/dev/null
    return $?
  fi
  [ -d "$_rod" ] || return 0
  _rop=$(cat "$_rod/pid" 2>/dev/null)
  [ -n "$_rop" ] && kill -0 "$_rop" 2>/dev/null && return 1
  [ -n "$(find "$_rod" -maxdepth 0 -mmin +"${KB_FLUSH_LOCK_STALE_MIN:-120}" 2>/dev/null)" ]
}
bf_lock_probe() { # <okey> -> the mode-appropriate probe
  case "$MODE" in push | auto) bf_lock_free "$1" ;; *) bf_lock_free_ro "$1" ;; esac
}

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
bf_log() { # one line into the flush log (the brief's scanner reads `backfill <sid8> <proj> ...`)
  mkdir -p "$(dirname "$BF_LOG")" 2>/dev/null || true
  printf '%s backfill %s\n' "$(date -Is 2>/dev/null || date)" "$*" >>"$BF_LOG" 2>/dev/null || true
}
say() { [ "$MODE" = auto ] || printf '%s\n' "$*"; } # stdout is EMPTY under --auto

# LAST RESORT ONLY, and deliberately weak. Claude Code names a transcript's
# directory by replacing every '/' of the cwd with '-', which is NOT reversible:
# a directory whose own name contains a dash ("data-server") decodes to a path
# that never existed ("/home/usama/data/server"), and two different cwds can
# collapse to the same slug. So the naive decode is accepted ONLY when it names a
# directory that actually exists on this box. Anything else is reported SKIPPED
# with its reason -- a transcript is never guessed into the wrong project.
bf_slug_cwd() { # <project directory name> -> an EXISTING directory, or nothing
  _bfd="/$(printf '%s' "${1#-}" | tr '-' '/')"
  [ -d "$_bfd" ] && printf '%s' "$_bfd"
}

# The project: the hook's OWN pin cache (state/<okey>.project, `<slug>\t<how>`)
# when the session has one, else the same derivation from the first recorded cwd
# (git root, .kbproject, slugify). DISPLAY AND --project FILTERING ONLY: the hook
# pins authoritatively, so a drift here can mislead a human, never misfile.
bf_project_of() { # <okey> <cwd> -> project slug
  if [ -s "$STATE/$1.project" ]; then
    _bfc=$(cut -f1 "$STATE/$1.project" 2>/dev/null | head -n1 | tr -d '\r')
    [ -n "$_bfc" ] && {
      printf '%s' "$_bfc"
      return 0
    }
  fi
  [ -n "${2:-}" ] || return 0
  _bfr=$(git -C "$2" rev-parse --show-toplevel 2>/dev/null) || _bfr=""
  [ -n "$_bfr" ] || _bfr="$2"
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

# Would the hook branch-seed this no-checkpoint transcript (Appendix C)? A
# sibling in the same directory with the SAME first line and a checkpoint > 0
# is a parent whose flushed prefix this fork copied. Prints `<parent8> <prefix>`
# (the shared-prefix length, clamped to the parent's checkpoint) or nothing.
# Classification only -- the hook computes the real seed under its lock.
bf_branch_of() { # <transcript> -> "parent8 prefix" | nothing
  # The hook's kb_branch_seed honours this knob; with it off the row must fall
  # through to `unknown` exactly as the hook would refuse (or from-0) the fork.
  [ "${KB_FLUSH_BRANCH_SCAN:-1}" = 1 ] || return 0
  command -v cmp >/dev/null 2>&1 || return 0
  _bb1=$(head -c 4096 "$1" | head -n1)
  [ -n "$_bb1" ] || return 0
  _bbbest=0
  _bbpar=""
  for _bbp in "$(dirname "$1")"/*.jsonl; do
    [ -e "$_bbp" ] && [ "$_bbp" != "$1" ] || continue
    [ "$(head -c 4096 "$_bbp" | head -n1)" = "$_bb1" ] || continue
    _bbpo=$(bf_offset_raw "$(bf_okey "$(basename "$_bbp" .jsonl)")")
    case "$_bbpo" in '' | *[!0-9]* | 0) continue ;; esac
    _bbout=$(LC_ALL=C cmp "$1" "$_bbp" 2>/dev/null)
    _bbrc=$?
    [ "$_bbrc" -le 1 ] || continue
    _bbdiff=$(printf '%s' "$_bbout" | sed -n 's/.*differ: [a-z]* \([0-9][0-9]*\).*/\1/p')
    if [ "$_bbrc" = 0 ]; then _bboff=$(bf_size "$_bbp")
    elif [ -n "$_bbdiff" ]; then _bboff=$((_bbdiff - 1))
    else
      _bboff=$(bf_size "$_bbp")
      _bbt=$(bf_size "$1")
      [ "$_bbt" -lt "$_bboff" ] && _bboff=$_bbt
    fi
    [ "$_bboff" -gt "$_bbpo" ] && _bboff=$_bbpo
    [ "$_bboff" -gt "$_bbbest" ] && {
      _bbbest=$_bboff
      _bbpar=$(printf '%s' "$(basename "$_bbp" .jsonl)" | tr -c 'A-Za-z0-9._-' '-' | cut -c1-8)
    }
  done
  [ -n "$_bbpar" ] && [ "$_bbbest" -gt 0 ] && printf '%s %s' "$_bbpar" "$_bbbest"
  return 0
}

# Snap a byte count DOWN to the byte after the last newline at or before it --
# the hook cuts on line boundaries, so a checkpoint must sit on one. Bounded
# 1 MB windows walking backwards, so a 200 MB prefix is never read into memory.
# Counts newlines and takes `head -n <count>` rather than `tail -n1`: uutils
# coreutils (the owner box) truncates `tail -n1` of a newline-less 1 MB window
# to 983040 bytes, which would have put a seed mid-line.
bf_snap() { # <transcript> <bytes> -> snapped bytes (0 when no newline precedes)
  _sm="$2"
  while [ "$_sm" -gt 0 ]; do
    _sw=$((_sm > 1048576 ? 1048576 : _sm))
    head -c "$_sm" "$1" | tail -c "$_sw" >"$BF_TMP/snap"
    _swb=$(bf_size "$BF_TMP/snap")
    [ "$_swb" -gt 0 ] || break
    _snl=$(tr -cd '\n' <"$BF_TMP/snap" | wc -c | tr -d ' ')
    case "$_snl" in '' | *[!0-9]*) _snl=0 ;; esac
    if [ "$_snl" -gt 0 ]; then
      _supto=$(head -n "$_snl" "$BF_TMP/snap" | wc -c | tr -d ' ') # bytes through the last newline
      printf '%s' "$((_sm - _swb + _supto))"
      return 0
    fi
    _sm=$((_sm - _swb)) # no newline in this window: look further back
  done
  printf '0'
}

# The hook's per-project NEEDS TOKEN stamp (flush-session-to-kb.sh
# kb_no_token_notice: state/.notoken-<slug>.stamp, one epoch, hourly) is fresh.
bf_notoken_fresh() { # <slug> -> 0 when the stamp is younger than 3600 s
  [ -n "${1:-}" ] && [ "$1" != "?" ] || return 1
  _bnt=$(cat "$STATE/.notoken-$1.stamp" 2>/dev/null || echo)
  case "$_bnt" in '' | *[!0-9]*) return 1 ;; esac
  [ "$((NOW - _bnt))" -lt 3600 ]
}
# The hook's once-per-24 h stamps (flush-session-to-kb.sh kb_stale_copy_skip:
# state/<okey>.stalecopy, <okey>.diverged.stamp, <okey>.behind.stamp -- their
# MTIME) are fresh by the real clock -- a firing the hook refused silently
# because it said so already today.
bf_stamp_fresh() { # <stamp file> -> 0 when it is younger than 86400 s
  [ -e "$1" ] || return 1
  _bsc=$(($(date +%s 2>/dev/null || echo 0) - $(bf_mtime "$1")))
  [ "$_bsc" -ge 0 ] && [ "$_bsc" -lt 86400 ]
}
bf_stalecopy_fresh() { bf_stamp_fresh "$STATE/$1.stalecopy"; } # <okey>
# Which refusal did the hook make of THIS firing (it moved no byte and left the
# shared checkpoint above the transcript)? Its stale-copy line in SEG names it;
# a rate-limited (silent) one is read from that kind's fresh stamp. Sets KIND
# to stale-copy | diverged | behind; status 1 when it was none of them.
bf_stale_kind() { # <okey> <sid8> <transcript>
  KIND=""
  _bsk=$(printf '%s\n' "$SEG" | grep -E "stale copy: .* sid=$2\$" | sed -n '$p')
  case "$_bsk" in
    *" reason=stale-copy-diverged "*) KIND=diverged ;;
    *" reason=behind "*) KIND=behind ;;
    *" reason=stale-copy "*) KIND=stale-copy ;;
  esac
  [ -n "$KIND" ] && return 0
  _bsn=$(kb_marker_names "$STATE" "$1" "$3") || _bsn=""
  if [ -n "$_bsn" ] && bf_stamp_fresh "$STATE/$1.$_bsn.stamp"; then
    KIND=$_bsn
  elif bf_stalecopy_fresh "$1"; then
    KIND=stale-copy
  fi
  [ -n "$KIND" ]
}
# A row tabled diverged/behind gets its marker (re)written by --push/--auto:
# nothing ever FIRES such a row, so without this a frozen copy would carry no
# marker and a later resume of it (mtime now newer) would read as a rewrite.
# A dry run and --status never write state.
bf_mark() { # <kind> <okey> <transcript> <size> <checkpoint> [cover]
  case "$MODE" in push | auto) ;; *) return 0 ;; esac
  kb_marker_write "$STATE/$2.$1" "$3" "$4" "$5" "${6:-}"
}
# Does a diverged/behind marker still hold <transcript> at <checkpoint> or past
# it? Prints its kind. A .diverged marker whose path now carries the checkpoint
# holder's prefix is RELEASED, exactly as the hook releases it
# (kb_marker_prefix_release): the owner moved the diverged copy out and the
# session was later resumed from that directory, so the file there is the live
# transcript again -- judged by CONTENT, [checkpoint-4096, checkpoint) against
# the copy that reaches the checkpoint (kb_window_matches). --push/--auto
# remove the marker and its stamp with ONE line; a dry run and --status only
# table the row as it will be after that, never writing state. A released path
# is judged again (a .behind marker naming it still holds it).
bf_marker_holds() { # <okey> <sid> <transcript> <checkpoint> -> diverged | behind (status 1: none)
  _bmh=$(kb_marker_names "$STATE" "$1" "$3") || return 1
  if [ "$_bmh" = diverged ] && _bmhc=$(kb_stale_cover "$2" "$3" "$4" "$BF_PROJECTS") &&
    kb_window_matches "$3" "$4" "${_bmhc#* }"; then
    # The sweep RELEASES (removes the marker) only at the hook's own post-drain
    # state -- the transcript ends exactly at <checkpoint> -- and only under
    # --push/--auto. Anywhere else it merely TABLES the row as the release would
    # leave it and the marker stays for the hook, which re-judges the window under
    # the session lock at the OFFSET it actually drains from: a discovery-time
    # release (no lock, at BASE) followed by a gated row (live/busy/budget) let the
    # next firing drain from a checkpoint another fork had moved -- mid-record.
    if { [ "$MODE" = push ] || [ "$MODE" = auto ]; } && [ "$(bf_size "$3")" = "$4" ]; then
      _bmhp=$(kb_marker_path "$STATE/$1.diverged") || _bmhp=$3
      rm -f "$STATE/$1.diverged" "$STATE/$1.diverged.stamp" 2>/dev/null
      bf_log "$(bf_sid8 "$2") ${PROJ:-?} marker cleared: state/$1.diverged named $_bmhp, which now carries the checkpoint holder's prefix"
    fi
    _bmh=$(kb_marker_path "$STATE/$1.behind") && { [ "$_bmh" = "$3" ] || [ "$_bmh" -ef "$3" ]; } || return 1
    _bmh=behind
  fi
  printf '%s' "$_bmh"
}
# Where a behind copy's UNSENT records begin: the frozen= of state/<okey>.behind
# when it names <transcript>, else <size> (a copy frozen right now: none).
bf_frozen_of() { # <okey> <transcript> <size> -> bytes
  _bfz=$(kb_marker_path "$STATE/$1.behind" 2>/dev/null) &&
    { [ "$_bfz" = "$2" ] || [ "$_bfz" -ef "$2" ]; } &&
    _bfz=$(kb_marker_frozen "$STATE/$1.behind") && {
    printf '%s' "$_bfz"
    return 0
  }
  printf '%s' "$3"
}
# The owner's remedy for a behind copy. `current` ("its bytes were sent") is
# offered ONLY while the copy still holds exactly its frozen bytes: resumed
# since it was marked, a seed at `current` would skip the records it wrote
# after the resume -- the exact seed is the marker's frozen= size.
bf_behind_owner() { # <sid> <size> <frozen>
  if [ "$2" -gt "$3" ]; then
    printf 'resumed since it was marked: its records past %s were NOT sent -- kb-flush-backfill.sh --seed %s %s sends exactly them' "$3" "$1" "$3"
  else
    printf 'Owner decision: kb-flush-backfill.sh --seed %s current (its bytes were sent: accept) or --seed %s 0 (re-send it from 0)' "$1" "$1"
  fi
}
# The owner's remedy for a diverged copy. While ANOTHER copy reaches the
# checkpoint no --seed can settle it: the verdict reads the files, not the
# marker, so the next pass or firing marks it again (and a lower seed re-sends
# the live copy). With no such copy, `current` is never "sent": it ACCEPTS the
# loss of the records past the divergence point. A session later resumed from
# the moved copy's directory needs no owner step: the file written there
# carries the live copy's prefix and the marker releases itself (bf_marker_holds).
bf_diverged_owner() { # <sid> <checkpoint> <cover path or empty>
  if [ -n "$3" ]; then
    printf 'Owner: a --seed cannot settle this while a copy reaches the checkpoint (the next --push/--auto or firing marks it again, and a seed below %s re-sends the live copy). To accept the loss, move this copy out of ~/.claude/projects (the marker then clears); to keep its records, leave it in place until the per-path checkpoint wave. Resuming the session from that directory later is safe: the file Claude Code writes there carries the live copy'"'"'s prefix, and the marker releases itself' "$2"
  else
    printf 'Owner: kb-flush-backfill.sh --seed %s current ACCEPTS the loss of those records (it clears the marker; it does not mean they were sent), or --seed %s <bytes> re-sends this copy from <bytes> (anything below the divergence point is sent twice)' "$1" "$1"
  fi
}
# The token NAME's key, exactly as the hook derives PROJ_KEY (never a value).
bf_proj_key() { # <slug> -> PROJ_KEY
  printf '%s' "$1" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9' '_'
}

# The per-session stall record: state/<okey>.sweep = `fails=<n> last=<epoch> verdict=<w>`
bf_stall_fails() { # <okey> -> consecutive no-progress runs (0 when none)
  _sf=$(sed -n 's/^fails=\([0-9]*\).*/\1/p' "$STATE/$1.sweep" 2>/dev/null | head -n1)
  case "$_sf" in '' | *[!0-9]*) _sf=0 ;; esac
  printf '%s' "$_sf"
}
bf_stall_last() { # <okey> -> epoch of the last recorded run (0 when none)
  _sl=$(sed -n 's/.* last=\([0-9]*\).*/\1/p' "$STATE/$1.sweep" 2>/dev/null | head -n1)
  case "$_sl" in '' | *[!0-9]*) _sl=0 ;; esac
  printf '%s' "$_sl"
}

# The sweep lock (box-level, state/.sweep.lock): one run at a time. flock on fd 8
# where it exists, a pid-owned dir otherwise, reclaimed ONLY when its pid is dead
# (or absent) past the run's own wall budget -- the same discipline as the trio.
BF_SWEEP_LOCK=""
# ONE age expression for the mkdir path, shared by the taker and the --status
# probe so they can never disagree: a dead-pid dir is reclaimable only past the
# run's own wall budget (a run that died mid-drain may still have a child
# holding a session lock for up to that long).
bf_sweep_stale_min() { printf '%s' "$(((BF_MAXSEC + BF_TIMEOUT) / 60 + 1))"; }
bf_sweep_dir_reclaimable() { # <dir> -> 0 when the pid is dead/absent AND the dir is past the window
  _bsp=$(cat "$1/pid" 2>/dev/null)
  { [ -z "$_bsp" ] || ! kill -0 "$_bsp" 2>/dev/null; } &&
    [ -n "$(find "$1" -maxdepth 0 -mmin +"$(bf_sweep_stale_min)" 2>/dev/null)" ]
}
bf_sweep_lock() { # 0 = ours, 1 = another run holds it
  _bsl="$STATE/.sweep.lock"
  _bsd="$STATE/.sweep.lock.d"
  if command -v flock >/dev/null 2>&1; then
    { exec 8>"$_bsl"; } 2>/dev/null || return 1
    flock -n 8 2>/dev/null || return 1
    BF_SWEEP_LOCK=flock
    return 0
  fi
  if mkdir "$_bsd" 2>/dev/null; then
    printf %s "$$" >"$_bsd/pid"
    BF_SWEEP_LOCK=mkdir
    return 0
  fi
  if bf_sweep_dir_reclaimable "$_bsd"; then
    rm -f "$_bsd/pid"
    rmdir "$_bsd" 2>/dev/null && mkdir "$_bsd" 2>/dev/null && printf %s "$$" >"$_bsd/pid" && {
      BF_SWEEP_LOCK=mkdir
      return 0
    }
  fi
  return 1
}
# A read-only probe for --status: 0 = held by a live run, 2 = a dead-pid mkdir
# dir the taker would still refuse (prints the minutes until it is reclaimable),
# 1 = free. The flock path has no stale state: a dead holder's lock is gone.
BF_SWEEP_STALE_IN=""
bf_sweep_lock_held() {
  if command -v flock >/dev/null 2>&1; then
    [ -e "$STATE/.sweep.lock" ] || return 1
    (
      exec 8<"$STATE/.sweep.lock"
      flock -n -s 8
    ) 2>/dev/null && return 1
    return 0
  fi
  _bsd="$STATE/.sweep.lock.d"
  [ -d "$_bsd" ] || return 1
  _bsp=$(cat "$_bsd/pid" 2>/dev/null)
  [ -n "$_bsp" ] && kill -0 "$_bsp" 2>/dev/null && return 0
  bf_sweep_dir_reclaimable "$_bsd" && return 1
  _bsage=$(((NOW - $(bf_mtime "$_bsd")) / 60))
  [ "$_bsage" -ge 0 ] || _bsage=0
  BF_SWEEP_STALE_IN=$(($(bf_sweep_stale_min) - _bsage))
  [ "$BF_SWEEP_STALE_IN" -ge 1 ] || BF_SWEEP_STALE_IN=1
  return 2
}
bf_cleanup() {
  if [ "$BF_SWEEP_LOCK" = mkdir ] && [ "$(cat "$STATE/.sweep.lock.d/pid" 2>/dev/null)" = "$$" ]; then
    rm -f "$STATE/.sweep.lock.d/pid"
    rmdir "$STATE/.sweep.lock.d" 2>/dev/null
  fi
  rm -rf "$BF_TMP"
}

# The TRIPWIRE (see the header). Checked against the hook this script will exec.
bf_lineage_ok() { # <hook path> -> 0 when the hook is a never-delete lineage
  [ -f "$1" ] || return 1
  [ "$(sed -n 2p "$1" 2>/dev/null | tr -d '\r')" = "$BF_LINEAGE" ] || return 1
  ! grep -qF 'rm -f "$OFFF"' "$1" 2>/dev/null
}

BF_TMP=$(mktemp -d "${TMPDIR:-/tmp}/kb-backfill.XXXXXX") || {
  printf 'kb-flush-backfill: could not create a temp dir\n' >&2
  exit 1
}
trap 'bf_cleanup' EXIT
trap 'exit 143' INT TERM

# ---------------------------------------------------------------------- --seed
if [ "$MODE" = seed ]; then
  if [ -z "$SEED_SID" ] || [ -z "$SEED_AT" ]; then
    printf 'kb-flush-backfill: --seed needs <sid> <bytes|current>\n' >&2
    exit 1
  fi
  shopt -s nullglob
  set -- "$BF_PROJECTS"/*/"$SEED_SID.jsonl"
  [ "$#" -gt 0 ] || set -- "$BF_PROJECTS"/*/"$SEED_SID"*.jsonl
  shopt -u nullglob
  # COPIES OF ONE SESSION (KB 49064): several matches that all carry the SAME
  # file name are one session resumed from different directories, not an
  # ambiguous prefix. Seed from the LARGEST (the live copy; a stale one is a
  # prefix of it) -- never from whichever the glob listed first.
  if [ "$#" -gt 1 ]; then
    _sdn=$(basename "$1")
    _sdbig="$1"
    for _sdm in "$@"; do
      [ "$(basename "$_sdm")" = "$_sdn" ] || {
        _sdbig=""
        break
      }
      [ "$(bf_size "$_sdm")" -gt "$(bf_size "$_sdbig")" ] && _sdbig="$_sdm"
    done
    [ -n "$_sdbig" ] && set -- "$_sdbig"
  fi
  if [ "$#" -ne 1 ]; then
    if [ "$#" -eq 0 ]; then
      printf 'kb-flush-backfill: --seed refused: no transcript %s.jsonl under %s\n' "$SEED_SID" "$BF_PROJECTS" >&2
    else
      printf 'kb-flush-backfill: --seed refused: %s matches %s transcripts -- give the full session id\n' "$SEED_SID" "$#" >&2
    fi
    exit 2
  fi
  T="$1"
  SID=$(basename "$T" .jsonl)
  OKEY=$(bf_okey "$SID")
  SIDS=$(bf_sid8 "$SID")
  SIZE=$(bf_size "$T")
  # ...and the copies under the hook's own roots (kb_session_copies): the
  # checkpoint is the SESSION's, so it is seeded from its largest transcript.
  _sdcnt=1
  _sdall="$T"
  while IFS= read -r _sdl; do
    _sds="${_sdl%% *}"
    case "$_sds" in '' | *[!0-9]*) continue ;; esac
    _sdcnt=$((_sdcnt + 1))
    [ "$_sds" -gt "$SIZE" ] && {
      SIZE=$_sds
      T="${_sdl#* }"
    }
  done <<KBSEED
$(kb_session_copies "$SID" "$_sdall" "$BF_PROJECTS")
KBSEED
  [ "$_sdcnt" -gt 1 ] &&
    printf 'copies: %s transcripts carry session %s -- seeding from the largest, %s (%s bytes); a smaller copy is a stale one (KB 49064)\n' "$_sdcnt" "$SIDS" "$T" "$SIZE"
  if [ "$SEED_AT" = current ]; then
    AT="$SIZE"
  else
    case "$SEED_AT" in '' | *[!0-9]*)
      printf 'kb-flush-backfill: --seed refused: <bytes> must be a number or `current`, got [%s]\n' "$SEED_AT" >&2
      exit 2
      ;;
    esac
    AT="$SEED_AT"
  fi
  if [ "$AT" -gt "$SIZE" ]; then
    printf 'kb-flush-backfill: --seed refused: %s exceeds the transcript size (%s bytes) -- a high seed loses lines\n' "$AT" "$SIZE" >&2
    exit 2
  fi
  SNAP=$(bf_snap "$T" "$AT")
  OLD=$(bf_offset_raw "$OKEY")
  # W4 (Appendix B): the session lock is held ACROSS the write, in a subshell so
  # the trio's traps stay out of this shell; a held lock means a drain is
  # running and its own writes are authoritative -- refuse, never race it.
  (
    kb_lock_take "$OKEY" || exit 3
    printf %s "$SNAP" >"$STATE/$OKEY.offset.t" && mv "$STATE/$OKEY.offset.t" "$STATE/$OKEY.offset"
  ) 2>/dev/null
  case "$?" in
    0) ;;
    3)
      printf 'kb-flush-backfill: --seed refused: the session lock for %s is held (a drain is running) -- retry when it finishes\n' "$SIDS" >&2
      exit 2
      ;;
    *)
      printf 'kb-flush-backfill: --seed failed: could not write %s\n' "$STATE/$OKEY.offset" >&2
      exit 1
      ;;
  esac
  rm -f "$STATE/$OKEY.unseeded" "$STATE/$OKEY.offset.t" 2>/dev/null
  PROJ=$(bf_project_of "$OKEY" "$(kb_first_record_field "$T" cwd)")
  [ -n "$PROJ" ] || PROJ="?"
  printf 'seeded %s offset=%s (snapped from %s)\n' "$SIDS" "$SNAP" "$AT"
  # The seed IS the owner's decision for this session (KB 49064): its diverged
  # / behind markers and their log stamps go with it, said here.
  _sdclr=""
  for _sdk in diverged behind; do
    if [ -e "$STATE/$OKEY.$_sdk" ]; then
      _sdp=$(kb_marker_path "$STATE/$OKEY.$_sdk") || _sdp='?'
      printf 'cleared: state/%s.%s (it named %s) -- the seed is the owner decision for this session\n' \
        "$OKEY" "$_sdk" "$_sdp"
      _sdclr="$_sdclr $_sdk"
      # MUST-FIX 2 (review 2): a diverged copy ANOTHER copy still covers is
      # judged from the files, not the marker -- the next pass re-marks it.
      if [ "$_sdk" = diverged ] && [ -f "$_sdp" ] && [ "$(bf_size "$_sdp")" -lt "$SNAP" ] &&
        kb_stale_cover "$SID" "$_sdp" "$SNAP" "$BF_PROJECTS" >/dev/null; then
        printf 'note: %s is still behind the checkpoint and another copy reaches it -- the next sweep or firing marks it again; move it out of ~/.claude/projects to accept the loss\n' "$_sdp"
      fi
    fi
    rm -f "$STATE/$OKEY.$_sdk" "$STATE/$OKEY.$_sdk.stamp" 2>/dev/null
  done
  bf_log "$SIDS $PROJ seed offset=$SNAP (was $OLD) size=$SIZE${_sdclr:+ cleared:$_sdclr}"
  exit 0
fi

# ---------------------------------------------------------------------- preflight
if [ ! -d "$BF_PROJECTS" ]; then
  say "kb-flush-backfill: nothing to do -- $BF_PROJECTS does not exist (no Claude Code transcripts on this box)"
  exit 0
fi
if [ "$MODE" = push ] || [ "$MODE" = auto ]; then
  if [ ! -f "$BF_HOOK" ]; then
    printf 'kb-flush-backfill: no flush hook at %s -- run install.sh first (or pass --hook)\n' "$BF_HOOK" >&2
    exit 1
  fi
  if [ ! -f "$BF_CFG" ]; then
    printf 'kb-flush-backfill: %s is missing, so the hook would exit 0 on every transcript and capture nothing. Run install.sh (or enrol this machine) first.\n' "$BF_CFG" >&2
    exit 1
  fi
  if ! bf_lineage_ok "$BF_HOOK"; then
    printf 'BUG: hook lineage mismatch — refusing to sweep with a delete-lineage hook (%s lacks line-2 "%s" or still deletes offsets)\n' "$BF_HOOK" "$BF_LINEAGE"
    # The stdout line is per run (the ONE line the contract asks for); the LOG
    # copy is rate-limited by a stamp exactly like kb_nojson_warn -- the timer
    # would otherwise write 48 identical lines a day into a 500-line log whose
    # brief scanner reads the last 40, burying every per-session failure.
    _tws="$STATE/.tripwire.stamp"
    _twl=$(cat "$_tws" 2>/dev/null || echo 0)
    case "$_twl" in '' | *[!0-9]*) _twl=0 ;; esac
    if [ "$((NOW - _twl))" -ge 3600 ] || [ "$((NOW - _twl))" -lt 0 ]; then
      printf '%s\n' "$NOW" >"$_tws" 2>/dev/null || true
      bf_log "BUG: hook lineage mismatch — refusing to sweep with a delete-lineage hook ($BF_HOOK)"
    fi
    exit 2
  fi
  if ! bf_sweep_lock; then
    say "kb-flush-backfill: another sweep holds $STATE/.sweep.lock -- exiting 3"
    exit 3
  fi
fi

# A diverged/behind marker whose named path no longer exists (the owner moved
# or removed that copy) is cleared by a --push/--auto pass, ONE log line each
# (KB 49064; the hook's own firings and --seed are the other clearers).
if [ "$MODE" = push ] || [ "$MODE" = auto ]; then
  for _bmk in "$STATE"/*.diverged "$STATE"/*.behind; do
    [ -f "$_bmk" ] || continue
    _bmp=$(kb_marker_path "$_bmk") || continue
    [ -e "$_bmp" ] && continue
    _bmx="${_bmk##*.}"
    _bmo=$(basename "$_bmk" ".$_bmx")
    rm -f "$_bmk" "$_bmk.stamp" 2>/dev/null
    _bmj=$(bf_project_of "$_bmo" "")
    bf_log "$(bf_sid8 "$_bmo") ${_bmj:-?} marker cleared: state/$_bmo.$_bmx named $_bmp, which no longer exists"
  done
fi

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
  MT=$(bf_mtime "$T")
  OFFRAW=$(bf_offset_raw "$OKEY")

  STATUS=ready
  REASON=""
  CWD=$(kb_first_record_field "$T" cwd)
  if [ -z "$CWD" ]; then
    CWD=$(bf_slug_cwd "$(basename "$(dirname "$T")")")
    [ -n "$CWD" ] && REASON="cwd recovered from the ambiguous directory slug (it resolved to a real directory)"
  fi
  if [ -z "$CWD" ]; then
    STATUS=skipped
    REASON="no cwd in the first ${KB_FLUSH_PIN_SCAN:-200} records and the directory slug names no existing directory"
  fi
  PROJ=$(bf_project_of "$OKEY" "$CWD")
  [ -n "$PROJ" ] || PROJ="?"

  # THE ACCOUNTING: the checkpoint only. `-` in the OFFSET column = no checkpoint.
  BASE="$OFFRAW"
  REMAIN=0
  if [ "$STATUS" = skipped ]; then
    [ "$BASE" = none ] && BASE="-"
    case "$BASE" in '' | *[!0-9]*) BASE="-" ;; esac
    if [ "$BASE" = "-" ]; then REMAIN=$SIZE; elif [ "$SIZE" -gt "$BASE" ]; then REMAIN=$((SIZE - BASE)); fi
  elif [ "$BASE" = none ]; then
    BASE="-"
    REMAIN=$SIZE
    BR=$(bf_branch_of "$T")
    if [ -n "$BR" ]; then
      STATUS=branch
      BASE="${BR#* }"
      REMAIN=$((SIZE - BASE))
      [ "$REMAIN" -ge 0 ] || REMAIN=0
      REASON="branch of ${BR%% *}: the hook will seed at the shared prefix (~$BASE bytes) and send only the fork's own bytes"
    else
      # THE HOOK'S OWN RULE (kb_decide_offset): a no-checkpoint transcript whose
      # first record post-dates the trust stamp is sent from 0 on any firing --
      # the stamp proves the delete-lineage cannot have touched it (no offset
      # could have been created-then-deleted for a session that started after
      # it). So it is `ready` here: the firing passes FIRE_ALLOW=0 and the hook
      # decides and writes the 0 itself (noclobber). Pre-stamp and undatable
      # transcripts stay `unknown`. (The hook also trusts a transcript with NO
      # timestamp; the sweep does not send those on its own -- real Claude Code
      # transcripts always carry one, so that case is a hand-made fixture.)
      _fts=$(kb_first_record_field "$T" timestamp)
      _since=$(head -n1 "$STATE/.checkpoints-trusted-since" 2>/dev/null | tr -d ' \r')
      if [ -n "$_fts" ] && [ -n "$_since" ] && [ "$_fts" \> "$_since" ]; then
        STATUS=ready
        BASE=0
        REMAIN=$SIZE
        REASON="no checkpoint but first record $_fts post-dates the trust stamp: sent from 0 (the hook's own rule)"
      elif [ -n "$_fts" ]; then
        STATUS=unknown
        REASON="no checkpoint; predates the trust stamp (${_since:-none}) -- an older hook may have drained it: kb-flush-backfill.sh --seed $SID current|<bytes>"
      else
        STATUS=unknown
        REASON="no checkpoint and no timestamp in the first records -- the hook starts it from 0 at its next firing (--push --include-unknown fires it now; --seed $SID current|<bytes> to skip what an older hook already sent)"
      fi
    fi
  else
    case "$BASE" in
      '' | *[!0-9]*)
        # A corrupt/empty checkpoint: the hook REFUSES it as unseeded (never a
        # restart from 0, KB 33783), so firing it would only book a refusal and
        # count towards a stall. An owner decision, like a pre-stamp transcript.
        STATUS=unknown
        REASON="corrupt checkpoint [$(printf '%.20s' "$BASE")]: the hook refuses it as unseeded -- kb-flush-backfill.sh --seed $SID current|<bytes>"
        BASE=0
        REMAIN=$SIZE
        ;;
      *)
        if [ "$SIZE" -eq 0 ]; then
          STATUS=flushed
          REASON="empty transcript (size 0): the hook does not drain it"
        elif [ "$SIZE" -lt "$BASE" ]; then
          # BEHIND THE CHECKPOINT (KB 49064): the hook's own verdict function,
          # so the table and the firing can never disagree. A session resumed
          # from another directory keeps its frozen transcript beside the live
          # one, and the live one's flushes carried the SHARED checkpoint past
          # it; a restart from 0 would re-send what was already sent (rm1,
          # 2026-10-08: 282 MB, ~3,750 duplicate items). Only a transcript
          # modified after the checkpoint's last write is a genuine shrink.
          kb_shrink_verdict "$SID" "$T" "$SIZE" "$BASE" "$STATE/$OKEY.offset" "$STATE" "$OKEY" "$BF_PROJECTS"
          case "$KB_SV" in
            stale-copy)
              STATUS=stale-copy
              REASON="stale copy: checkpoint $BASE is past the size, and a copy of this session in $(dirname "$KB_SV_COVER") ($(bf_size "$KB_SV_COVER") bytes) covers it -- a byte-prefix of that copy; no restart, never fired"
              REMAIN=0
              ;;
            diverged)
              STATUS=diverged
              REMAIN=$SIZE
              if [ "$KB_SV_WHY" = cover ]; then
                REASON="diverged copy: checkpoint $BASE is past the size and a copy of this session in $(dirname "$KB_SV_COVER") ($(bf_size "$KB_SV_COVER") bytes) reaches it, but this file's tail differs: its records past the shared prefix were NOT sent and the shared checkpoint cannot carry them (REMAIN is the whole file: the unique bytes are unknown without a full compare). Never fired, never restarted. $(bf_diverged_owner "$SID" "$BASE" "$KB_SV_COVER")"
              else
                REASON="diverged copy: checkpoint $BASE is past the size, no copy reaches it now and state/$OKEY.diverged names this file: its records past the shared prefix were NOT sent (REMAIN is the whole file). Never fired, never restarted. $(bf_diverged_owner "$SID" "$BASE" "")"
              fi
              bf_mark diverged "$OKEY" "$T" "$SIZE" "$BASE" "$KB_SV_COVER"
              ;;
            behind)
              STATUS=behind
              # 0 for a copy still frozen; a copy resumed since it was marked
              # holds SIZE - frozen= bytes that were never sent (unflushed).
              _bhf=$SIZE
              [ "$KB_SV_WHY" = marker ] && _bhf=$(bf_frozen_of "$OKEY" "$T" "$SIZE")
              REMAIN=$((SIZE - _bhf))
              [ "$REMAIN" -ge 0 ] || REMAIN=0
              _bhw="the file is frozen (last modified before the checkpoint's last write)"
              [ "$KB_SV_WHY" = marker ] && _bhw="state/$OKEY.behind names this file"
              if [ "$SIZE" -gt "$_bhf" ]; then
                REASON="behind: checkpoint $BASE is past the size, no copy of this session reaches it, and $_bhw -- never restarted from 0, never fired; $(bf_behind_owner "$SID" "$SIZE" "$_bhf")"
              else
                REASON="behind: checkpoint $BASE is past the size, no copy of this session reaches it, and $_bhw -- a frozen copy whose live copy is gone, never restarted from 0, never fired. $(bf_behind_owner "$SID" "$SIZE" "$_bhf")"
              fi
              bf_mark behind "$OKEY" "$T" "$SIZE" "$BASE"
              ;;
            *)
              STATUS=shrank
              REASON="checkpoint $BASE is past the size: the hook restarts from 0 (reason=shrank) -- the file changed after the checkpoint's last write (a rewrite, not a frozen copy)"
              [ "$KB_SV_WHY" = mtime-unreadable ] &&
                REASON="checkpoint $BASE is past the size: the hook restarts from 0 (reason=shrank) -- the frozen-copy check was skipped: an mtime could not be read"
              BASE=0
              REMAIN=$SIZE
              ;;
          esac
        elif _bmk=$(bf_marker_holds "$OKEY" "$SID" "$T" "$BASE"); then
          # A path a marker names, grown to the checkpoint or past it (KB 49064):
          # that checkpoint is ANOTHER copy's, so the hook refuses this path at
          # any size -- a drain from it would start mid-record and lose the
          # records between the marker's frozen= size and the checkpoint. Never
          # fireable; the marker is rewritten (frozen= kept) by --push/--auto.
          # A .diverged path that now carries the checkpoint holder's prefix is
          # released instead (bf_marker_holds) and tables ready / flushed below.
          STATUS=$_bmk
          _bmc=""
          _bmv=$(kb_stale_cover "$SID" "$T" "$BASE" "$BF_PROJECTS") && _bmc="${_bmv#* }"
          if [ "$_bmk" = diverged ]; then
            REMAIN=$SIZE
            REASON="diverged copy: state/$OKEY.diverged names this file and it has grown to checkpoint $BASE or past it -- that checkpoint is another copy's, so a drain from it would start mid-record: its records past the shared prefix were NOT sent (REMAIN is the whole file). Never fired, never restarted. $(bf_diverged_owner "$SID" "$BASE" "$_bmc")"
          else
            _bhf=$(bf_frozen_of "$OKEY" "$T" "$SIZE")
            REMAIN=$((SIZE - _bhf))
            [ "$REMAIN" -ge 0 ] || REMAIN=0
            REASON="behind: state/$OKEY.behind names this file and it has grown to checkpoint $BASE or past it -- that checkpoint is another copy's, so a drain from it would start mid-record: never fired, never restarted from 0; $(bf_behind_owner "$SID" "$SIZE" "$_bhf")"
          fi
          bf_mark "$_bmk" "$OKEY" "$T" "$SIZE" "$BASE"
        elif [ "$SIZE" -eq "$BASE" ]; then
          STATUS=flushed
        else
          REMAIN=$((SIZE - BASE))
        fi
        ;;
    esac
  fi

  # The gates, on FIREABLE rows only (a push-mode probe creates the lock file, so
  # it is not run for rows nothing would fire; dry/status use the read-only one).
  case "$STATUS" in
    ready | shrank | branch | unknown)
      _fire=1
      [ "$STATUS" = unknown ] && [ "$INCLUDE_UNKNOWN" != 1 ] && _fire=0
      [ "$MODE" = auto ] && [ "$STATUS" = unknown ] && _fire=0
      if [ "$_fire" = 1 ]; then
        if ! bf_lock_probe "$OKEY"; then
          STATUS=busy
          REASON="the session lock is held (a drain is running now)"
        elif [ "$NO_QUIET" != 1 ] && [ "$NOW" -gt 0 ] && [ "$((NOW - MT))" -lt "$BF_QUIET" ] && [ "$((NOW - MT))" -ge 0 ]; then
          STATUS=live
          REASON="modified $((NOW - MT))s ago (< KB_SWEEP_QUIET_SECONDS=$BF_QUIET): someone is still writing it"
        elif [ "$STATUS" = ready ] && [ "$REMAIN" -le "$BF_SEND_MAX" ] &&
          [ "$(tail -c +"$((BASE + 1))" "$T" 2>/dev/null | tr -cd '\n' | wc -c | tr -d ' ')" = 0 ]; then
          STATUS=tail-held
          REASON="the remainder is one unterminated line: the hook holds it until the record is complete"
        elif [ "$MODE" != push ] && [ "$(bf_stall_fails "$OKEY")" -ge "$BF_STALL_RUNS" ] && [ "$((NOW - $(bf_stall_last "$OKEY")))" -lt "$BF_STALL_BACKOFF" ]; then
          # The back-off gates --auto (and the table shows it); an explicit
          # --push is the owner's retry and ignores it (nothing else would lift
          # it short of deleting <okey>.sweep by hand).
          STATUS=stalled
          REASON="$(bf_stall_fails "$OKEY") runs without progress; --auto retries every $((BF_STALL_BACKOFF / 3600))h, --push retries now (state/$OKEY.sweep)"
        fi
      fi
      ;;
  esac

  if [ -n "$ONLY" ] && [ "$PROJ" != "$ONLY" ]; then continue; fi
  printf '%s\037%s\037%s\037%s\037%s\037%s\037%s\037%s\037%s\037%s\037%s\n' \
    "$MT" "$STATUS" "$PROJ" "$SID" "$SIZE" "$BASE" "$REMAIN" \
    "$OKEY" "$CWD" "$T" "$REASON" >>"$IDX"
done
shopt -u nullglob

if [ ! -s "$IDX" ] && [ "$MODE" != auto ] && [ "$MODE" != status ]; then
  if [ -n "$ONLY" ]; then
    say "kb-flush-backfill: no transcripts under $BF_PROJECTS land in project $ONLY"
  else
    say "kb-flush-backfill: no transcripts under $BF_PROJECTS"
  fi
  exit 0
fi
sort -n "$IDX" >"$IDX.sorted" 2>/dev/null && mv "$IDX.sorted" "$IDX"

# -------------------------------------------------------------------- the table
N_TOTAL=0
N_READY=0
N_FLUSHED=0
N_UNKNOWN=0
N_LIVE=0
N_BUSY=0
N_STALLED=0
N_OTHER=0
N_STALE=0
N_DIVERGED=0
N_BEHIND=0
B_REMAIN=0
B_UNKNOWN=0
B_UNFLUSHED=0
[ "$MODE" = auto ] || printf '%-22s %-40s %12s %12s %12s  %s\n' PROJECT SESSION SIZE OFFSET REMAINING STATE
while IFS="$SEP" read -r MT STATUS PROJ SID SIZE BASE REMAIN OKEY CWD T REASON; do
  N_TOTAL=$((N_TOTAL + 1))
  case "$STATUS" in
    ready | shrank | branch)
      N_READY=$((N_READY + 1))
      B_REMAIN=$((B_REMAIN + REMAIN))
      B_UNFLUSHED=$((B_UNFLUSHED + REMAIN))
      ;;
    flushed) N_FLUSHED=$((N_FLUSHED + 1)) ;;
    unknown)
      N_UNKNOWN=$((N_UNKNOWN + 1))
      B_UNKNOWN=$((B_UNKNOWN + REMAIN))
      ;;
    live)
      N_LIVE=$((N_LIVE + 1))
      B_UNFLUSHED=$((B_UNFLUSHED + REMAIN))
      ;;
    busy)
      N_BUSY=$((N_BUSY + 1))
      B_UNFLUSHED=$((B_UNFLUSHED + REMAIN))
      ;;
    stalled)
      N_STALLED=$((N_STALLED + 1))
      B_UNFLUSHED=$((B_UNFLUSHED + REMAIN))
      ;;
    tail-held)
      N_OTHER=$((N_OTHER + 1))
      B_UNFLUSHED=$((B_UNFLUSHED + REMAIN))
      ;;
    stale-copy) N_STALE=$((N_STALE + 1)) ;; # a newer copy holds its bytes: never unflushed
    diverged) # its unique records were never sent: unflushed (REMAIN = the whole file)
      N_DIVERGED=$((N_DIVERGED + 1))
      B_UNFLUSHED=$((B_UNFLUSHED + REMAIN))
      ;;
    behind) # an owner --seed decision; REMAIN = what a copy resumed since it was marked holds past frozen= (0 while frozen)
      N_BEHIND=$((N_BEHIND + 1))
      B_UNFLUSHED=$((B_UNFLUSHED + REMAIN))
      ;;
    *) N_OTHER=$((N_OTHER + 1)) ;;
  esac
  [ "$MODE" = auto ] && continue
  printf '%-22.22s %-40.40s %12s %12s %12s  %s\n' "$PROJ" "$SID" "$SIZE" "$BASE" "$REMAIN" "$STATUS"
  [ -n "$REASON" ] && printf '%-22s %s\n' '' "-- $REASON"
done <"$IDX"

say ""
say "$N_TOTAL transcript(s): $N_READY with unflushed bytes ($B_REMAIN bytes), $N_FLUSHED flushed, $N_UNKNOWN unknown ($B_UNKNOWN bytes, never swept -- see --seed), $((N_LIVE + N_BUSY + N_STALLED + N_OTHER)) other (live/busy/tail-held/stalled/skipped)$([ "$N_STALE" -gt 0 ] && printf ', %s stale copy(ies) (a newer copy of the session covers them -- never restarted)' "$N_STALE")$([ "$N_DIVERGED" -gt 0 ] && printf ', %s diverged copy(ies) (records past a shared checkpoint were not sent -- never fired; see the reason)' "$N_DIVERGED")$([ "$N_BEHIND" -gt 0 ] && printf ', %s behind the checkpoint (frozen, no copy reaches it -- never restarted; an owner --seed decision)' "$N_BEHIND")"

bf_age() { # <epoch> -> 12m | 3h | 2d
  _ba=$((NOW - $1))
  [ "$_ba" -ge 0 ] || _ba=0
  if [ "$_ba" -lt 3600 ]; then printf '%sm' "$((_ba / 60))"
  elif [ "$_ba" -lt 172800 ]; then printf '%sh' "$((_ba / 3600))"
  else printf '%sd' "$((_ba / 86400))"; fi
}
if [ "$MODE" = status ]; then
  printf '\nsweep health\n'
  if [ -f "$STATE/.sweep-last" ]; then
    _ts=$(sed -n 's/^ts=\([0-9]*\).*/\1/p' "$STATE/.sweep-last" 2>/dev/null | head -n1)
    case "$_ts" in '' | *[!0-9]*) _ts=$(bf_mtime "$STATE/.sweep-last") ;; esac
    printf '  last sweep : %s ago -- %s\n' "$(bf_age "$_ts")" "$(head -n1 "$STATE/.sweep-last" 2>/dev/null | cut -c1-160)"
  else
    printf '  last sweep : never (no %s)\n' "$STATE/.sweep-last"
  fi
  if [ -e "$STATE/.sweep.trigger" ]; then
    printf '  trigger    : %s ago (a sweep was asked for; the next --auto consumes it)\n' "$(bf_age "$(bf_mtime "$STATE/.sweep.trigger")")"
  else
    printf '  trigger    : none pending\n'
  fi
  bf_sweep_lock_held
  case "$?" in
    0) printf '  sweep lock : HELD (a run is in progress)\n' ;;
    2) printf '  sweep lock : stale (dead pid in %s; --auto/--push reclaim it in %sm)\n' "$STATE/.sweep.lock.d" "$BF_SWEEP_STALE_IN" ;;
    *) printf '  sweep lock : free\n' ;;
  esac
  _st=0
  for _sw in "$STATE"/*.sweep; do
    [ -e "$_sw" ] || continue
    [ "$(bf_stall_fails "$(basename "$_sw" .sweep)")" -ge "$BF_STALL_RUNS" ] && _st=$((_st + 1))
  done
  printf '  stalled    : %s session(s) at >= %s runs without progress\n' "$_st" "$BF_STALL_RUNS"
  printf '  trust stamp: %s\n' "$(head -n1 "$STATE/.checkpoints-trusted-since" 2>/dev/null || echo none)"
  if bf_lineage_ok "$BF_HOOK"; then
    printf '  hook       : %s -- lineage ok (never-delete-offsets v2)\n' "$BF_HOOK"
  else
    printf '  hook       : %s -- LINEAGE MISMATCH: --push/--auto would refuse (tripwire)\n' "$BF_HOOK"
  fi
  exit 0
fi

if [ "$MODE" = dry ]; then
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
#
# THE WAIT. The hook returns the moment its child is detached, so completion is
# read from the SESSION LOCK: the child takes it first thing and releases it
# after its summary line, so "free on two consecutive probes after a short grace"
# means the drain is over -- whatever it did (drained, held a half line, was
# refused at the token step, failed a chunk). The ` sid=<sid8>` ok/partial line
# is an early exit. The verdict then comes from the CHECKPOINT alone: it is the
# only record the hook keeps, and it is never deleted, so an offset that is gone
# after a drain is a bug, not a completion.
bf_wait_drain() { # <okey> <sid8> <pre-lines> <size-at-fire> -> sets CUR, TIMED_OUT, SEG
  TIMED_OUT=0
  _wi=0
  _wfree=0
  while [ "$_wi" -lt "$((BF_TIMEOUT * 4))" ]; do
    SEG=$(bf_log_since "$3")
    if printf '%s\n' "$SEG" | grep -qE " (ok|partial) bytes=.* sid=$2\$"; then break; fi
    if [ "$_wi" -ge 8 ]; then # 2 s grace: the detached child has long taken its lock by then
      if bf_lock_free "$1"; then
        _wfree=$((_wfree + 1))
        [ "$_wfree" -ge 2 ] && break
      else
        _wfree=0
      fi
    fi
    _wi=$((_wi + 1))
    sleep 0.25
  done
  [ "$_wi" -ge "$((BF_TIMEOUT * 4))" ] && TIMED_OUT=1
  SEG=$(bf_log_since "$3")
  CUR=$(bf_offset "$1")
}

PUSHED=0
PARTIAL=0
FAILED=0
DEFERRED=0
BUSY=0
LIMITED=0
BYTES=0
SPENT=0
FIRED=0
P_STALE=0
P_DIVERGED=0
P_BEHIND=0
T_START=$(date +%s 2>/dev/null || echo 0)
case "$T_START" in '' | *[!0-9]*) T_START=0 ;; esac
say ""
while IFS="$SEP" read -r MT STATUS PROJ SID SIZE BASE REMAIN OKEY CWD T REASON <&3; do
  case "$STATUS" in
    ready | shrank | branch) ;;
    unknown) [ "$INCLUDE_UNKNOWN" = 1 ] && [ "$MODE" != auto ] || continue ;;
    *) continue ;;
  esac
  [ "$BASE" = "-" ] && BASE=0
  # The run budgets: --limit for the owner; bytes / count / wall clock for --auto.
  if [ "$MODE" != auto ] && [ "$LIMIT" -gt 0 ] && [ "$FIRED" -ge "$LIMIT" ]; then
    LIMITED=$((LIMITED + 1))
    continue
  fi
  if [ "$MODE" = auto ]; then
    _el=$(($(date +%s 2>/dev/null || echo 0) - T_START))
    if [ "$FIRED" -ge "$BF_MAXN" ] || [ "$SPENT" -ge "$BF_BUDGET" ] || [ "$_el" -ge "$BF_MAXSEC" ]; then
      DEFERRED=$((DEFERRED + 1))
      continue
    fi
    FIRE_MAX=$((BF_BUDGET - SPENT))
  else
    FIRE_MAX="${KB_FLUSH_MAX:-209715200}"
  fi
  FIRE_ALLOW="${KB_FLUSH_ALLOW_UNSEEDED:-0}"
  [ "$STATUS" = unknown ] && FIRE_ALLOW=1
  SIDS=$(bf_sid8 "$SID")

  # THE EVENT, never hand-written. kb_json_build is the encoder these hooks
  # already own, so a cwd or session id carrying a quote, a backslash or a
  # newline reaches the hook exactly as Claude Code would have delivered it.
  # hook_event_name is Backfill: the hook holds the trailing partial line,
  # honours the per-firing cap and keeps the checkpoint -- the SessionEnd
  # branch (cap lifted, connector half-line sent) is never taken by a sweep.
  EVF="$BF_TMP/event.json"
  kb_json_build "$EVF" transcript_path s "$T" cwd s "$CWD" session_id s "$SID" \
    hook_event_name s Backfill
  if [ ! -s "$EVF" ] || ! kb_json_valid "$EVF"; then
    say "$(printf '%-22.22s %-40.40s FAILED: could not build the hook event' "$PROJ" "$SID")"
    bf_log "$SIDS $PROJ failed base=$BASE size=$SIZE sent=0 took=0s -- could not build the hook event"
    FAILED=$((FAILED + 1))
    continue
  fi
  # A drain may have started since discovery (a Stop firing, the piggyback):
  # busy is a fact, never a failure -- no stall increment, no NOT CAPTURED.
  if ! bf_lock_free "$OKEY"; then
    BUSY=$((BUSY + 1))
    say "$(printf '%-22.22s %-40.40s busy (a drain is running)' "$PROJ" "$SID")"
    continue
  fi

  PRE=$(bf_log_lines)
  # The RAW value, so a checkpoint that held 0 and then vanished mid-drain is
  # still caught by the lineage BUG detector below (bf_offset reads 0 for both).
  OFF0RAW=$(bf_offset_raw "$OKEY")
  T0=$(date +%s 2>/dev/null || echo 0)
  [ "$MODE" = auto ] || printf '%-22.22s %-40.40s %s bytes ... ' "$PROJ" "$SID" "$REMAIN"
  # fd 8 (the box-level sweep flock) and fd 3 (the index) are CLOSED for the
  # hook -- the same fd-inheritance class as the hook's own OKEY-export incident:
  # the hook's setsid-detached drain would otherwise carry fd 8 for as long as
  # it runs, keeping .sweep.lock HELD after this run exits (the next --auto then
  # exits 3 in silence and --status says HELD). Redirections on the function
  # call apply to everything kb_timeout runs, the perl watchdog path included.
  KB_FLUSH_MAX="$FIRE_MAX" KB_FLUSH_ALLOW_UNSEEDED="$FIRE_ALLOW" kb_timeout "$BF_TIMEOUT" bash "$BF_HOOK" Backfill <"$EVF" >"$BF_TMP/hook.out" 8>&- 3<&- 2>&1
  HRC=$?
  FIRED=$((FIRED + 1))
  bf_wait_drain "$OKEY" "$SIDS" "$PRE" "$SIZE"
  TOOK=$(($(date +%s 2>/dev/null || echo 0) - T0))

  SENT=$((CUR - BASE))
  [ "$SENT" -ge 0 ] || SENT=0
  DETAIL=""
  KIND=""
  TSIZE=$(bf_size "$T")
  # UNMOVED: the checkpoint is still the one this firing started from AND above
  # the file -- a shrank row's hook stopped before it decided (NEEDS TOKEN, a
  # failed mint, the lock held). CUR-BASE is then the OLD checkpoint, never
  # bytes sent: SENT=0, never `pushed`, never charged -- the busy / NEEDS TOKEN
  # / no-progress greps below name the firing.
  UNMOVED=0
  if [ "$TSIZE" -lt "$CUR" ] && [ "$CUR" = "$OFF0RAW" ]; then
    UNMOVED=1
    SENT=0
  fi
  if [ "$(bf_offset_raw "$OKEY")" = none ] && [ "$OFF0RAW" != none ]; then
    VERDICT=failed
    DETAIL="BUG: offset file disappeared during a drain"
  elif [ "$TIMED_OUT" = 1 ]; then
    VERDICT=timeout
    DETAIL="no completion within ${BF_TIMEOUT}s (hook exit $HRC); the drain may still be running -- the next run resumes from its checkpoint"
  elif { [ "$TSIZE" -lt "$CUR" ] || bf_marker_holds "$OKEY" "$SID" "$T" "$CUR" >/dev/null; } &&
    bf_stale_kind "$OKEY" "$SIDS" "$T"; then
    # The hook REFUSED this transcript as behind the shared checkpoint (KB
    # 49064) -- typically covered by the live copy between discovery and this
    # firing -- or as a path a marker names (marked by a race, at any size).
    # The checkpoint it left is the OTHER copy's, so `CUR -ge SIZE` must not
    # read as pushed and CUR-BASE (the whole checkpoint for a shrank row) was
    # never sent: SENT=0, never charged to the budget.
    SENT=0
    case "$KIND" in
      stale-copy)
        VERDICT=skipped
        DETAIL="reason=stale-copy: the hook skipped a stale copy of this session (a newer copy covers checkpoint $CUR) -- nothing sent, no restart"
        ;;
      diverged)
        VERDICT=refused
        _pkw="behind checkpoint $CUR and its tail differs from the copy that reaches it"
        [ "$TSIZE" -lt "$CUR" ] || _pkw="state/$OKEY.diverged names this copy and checkpoint $CUR is another copy's"
        DETAIL="reason=stale-copy-diverged: $_pkw -- nothing sent, no restart; records past the shared prefix may be unflushed (state/$OKEY.diverged)"
        ;;
      *)
        VERDICT=refused
        _pkw="a frozen copy behind checkpoint $CUR that no copy reaches"
        [ "$TSIZE" -lt "$CUR" ] || _pkw="state/$OKEY.behind names this copy and checkpoint $CUR is another copy's"
        _pkf=$(bf_frozen_of "$OKEY" "$T" "$TSIZE")
        _pko="an owner decision: kb-flush-backfill.sh --seed $SID current|<bytes>"
        [ "$TSIZE" -gt "$_pkf" ] && _pko=$(bf_behind_owner "$SID" "$TSIZE" "$_pkf")
        DETAIL="reason=behind: $_pkw -- nothing sent, no restart; $_pko (state/$OKEY.behind)"
        ;;
    esac
  elif [ "$UNMOVED" = 0 ] && [ "$CUR" -ge "$SIZE" ] && [ "$CUR" -gt 0 ]; then
    VERDICT=pushed
  elif [ "$SENT" -gt 0 ]; then
    VERDICT=partial
    DETAIL="advanced to $CUR of $SIZE"
    printf '%s\n' "$SEG" | grep -q "chunk FAILED" && DETAIL="$DETAIL after a refused chunk (see the chunk FAILED line above)"
  elif printf '%s\n' "$SEG" | grep -q "skipped (lock held)"; then
    VERDICT=busy
    DETAIL="the hook found the session lock held"
  elif printf '%s\n' "$SEG" | grep -q "chunk FAILED"; then
    VERDICT=failed
    DETAIL=$(printf '%s\n' "$SEG" | grep -o "chunk FAILED ([^)]*)" | tail -n1)
  elif printf '%s\n' "$SEG" | grep -qE "flush $PROJ .*(NEEDS TOKEN|NOT CONFIGURED)"; then
    VERDICT=refused
    DETAIL="the hook refused this project (NEEDS TOKEN / NOT CONFIGURED); see $BF_LOG"
  elif printf '%s\n' "$SEG" | grep -qE "unseeded: refusing .* sid=$SIDS\$"; then
    VERDICT=refused
    DETAIL="the hook refused an unseeded transcript"
  elif printf '%s\n' "$SEG" | grep -qE "lock lost .* sid=$SIDS\$"; then
    VERDICT=failed
    DETAIL="the drain lost its lock and stopped"
  elif bf_notoken_fresh "$PROJ"; then
    # The hook's NEEDS TOKEN line is hourly (.notoken-<slug>.stamp) and the sweep
    # half-hourly, so every other run saw a SILENT refusal and called it
    # no-progress (KB 40411). A fresh stamp IS that refusal: say so, by NAME.
    VERDICT=refused
    DETAIL="NEEDS TOKEN (rate-limited): no KB_TOKEN_$(bf_proj_key "$PROJ") on this box; see $BF_LOG"
  else
    VERDICT=no-progress
    DETAIL="the hook logged nothing for this session and moved no checkpoint (a rate-limited refusal? check $BF_LOG)"
  fi

  case "$KIND:$VERDICT" in
    stale-copy:*)
      P_STALE=$((P_STALE + 1))
      say "$VERDICT ($DETAIL)"
      ;;
    diverged:*)
      P_DIVERGED=$((P_DIVERGED + 1))
      say "$VERDICT ($DETAIL)"
      ;;
    behind:*)
      P_BEHIND=$((P_BEHIND + 1))
      say "$VERDICT ($DETAIL)"
      ;;
    :pushed)
      PUSHED=$((PUSHED + 1))
      say "ok"
      ;;
    :partial)
      PARTIAL=$((PARTIAL + 1))
      say "partial ($DETAIL)"
      ;;
    :busy)
      BUSY=$((BUSY + 1))
      say "busy ($DETAIL)"
      ;;
    :timeout)
      say "timeout ($DETAIL)"
      ;;
    *)
      FAILED=$((FAILED + 1))
      say "FAILED ($VERDICT: $DETAIL)"
      ;;
  esac
  # A stale-copy / diverged / behind firing sent nothing: never charged.
  if [ -z "$KIND" ]; then
    BYTES=$((BYTES + SENT))
    SPENT=$((SPENT + SENT))
  fi
  bf_log "$SIDS $PROJ $VERDICT base=$BASE size=$SIZE sent=$SENT took=${TOOK}s${DETAIL:+ -- $DETAIL}"

  # Stall accounting (state/<okey>.sweep): progress resets, a no-progress run
  # increments, busy/timeout leave it alone. Crossing KB_SWEEP_STALL_RUNS logs
  # ONE `STALLED` line -- deliberately none of the brief's failure tokens, and
  # never again for the same stall (the count keeps rising past it).
  # A stale-copy / diverged / behind refusal is no stall: the next run tables
  # the row as such and never fires it again.
  case "$KIND:$VERDICT" in
    :pushed | :partial) _fails=0 ;;
    :failed | :refused | :no-progress) _fails=$(($(bf_stall_fails "$OKEY") + 1)) ;;
    *) _fails="" ;;
  esac
  if [ -n "$_fails" ]; then
    printf 'fails=%s last=%s verdict=%s\n' "$_fails" "$NOW" "$VERDICT" >"$STATE/$OKEY.sweep.t" 2>/dev/null &&
      mv "$STATE/$OKEY.sweep.t" "$STATE/$OKEY.sweep" 2>/dev/null || rm -f "$STATE/$OKEY.sweep.t" 2>/dev/null
    if [ "$_fails" -eq "$BF_STALL_RUNS" ]; then
      bf_log "$SIDS $PROJ STALLED after $_fails runs without progress (base=$BASE size=$SIZE) -- retried every $((BF_STALL_BACKOFF / 3600))h; the hook's own lines for sid=$SIDS say why"
      say "$(printf '%-22.22s %-40.40s STALLED after %s runs without progress' "$PROJ" "$SID" "$_fails")"
    fi
  fi
done 3<"$IDX"

# Stalled sessions AFTER this run's accounting (the count notify/brief show).
N_STALLED=0
for _sw in "$STATE"/*.sweep; do
  [ -e "$_sw" ] || continue
  [ "$(bf_stall_fails "$(basename "$_sw" .sweep)")" -ge "$BF_STALL_RUNS" ] && N_STALLED=$((N_STALLED + 1))
done

# The rows that ended behind a shared checkpoint (KB 49064): tabled so, plus
# the ones the hook refused at fire time (covered between discovery and firing).
T_STALE=$((N_STALE + P_STALE))
T_DIVERGED=$((N_DIVERGED + P_DIVERGED))
T_BEHIND=$((N_BEHIND + P_BEHIND))

say ""
say "kb-flush-backfill: pushed=$PUSHED partial=$PARTIAL failed=$FAILED deferred=$DEFERRED busy=$BUSY skipped=$((N_TOTAL - N_READY - N_UNKNOWN)) bytes=$BYTES stale=$T_STALE diverged=$T_DIVERGED behind=$T_BEHIND"
[ "$LIMITED" -gt 0 ] && say "$LIMITED transcript(s) left for the next run by --limit $LIMIT."

# One line per COMPLETED sweep (--auto and the owner's explicit --push alike:
# on a timer-less box a hand sweep is the only thing that would ever refresh
# it), overwritten, read by notify-complete.sh and the brief.
# `stale=`, `diverged=` and `behind=` (KB 49064) are LAST and purely additive:
# every reader keys on its own ` <name>=` (` stalled=` cannot match ` stale=`).
printf 'ts=%s rc=0 mode=%s ready=%s pushed=%s failed=%s deferred=%s stalled=%s unknown=%s live=%s unflushed=%s busy=%s partial=%s stale=%s diverged=%s behind=%s\n' \
  "$NOW" "$MODE" "$N_READY" "$PUSHED" "$FAILED" "$((DEFERRED + LIMITED))" "$N_STALLED" "$N_UNKNOWN" "$N_LIVE" "$B_UNFLUSHED" "$((N_BUSY + BUSY))" "$PARTIAL" "$T_STALE" "$T_DIVERGED" "$T_BEHIND" \
  >"$STATE/.sweep-last.t" 2>/dev/null && mv "$STATE/.sweep-last.t" "$STATE/.sweep-last" 2>/dev/null || rm -f "$STATE/.sweep-last.t" 2>/dev/null
if [ "$MODE" = auto ]; then
  # The piggyback trigger is consumed so the firings this run provoked cannot
  # re-trigger it (a --push may be partial: --limit/--project, so it leaves the
  # trigger alone). A fully quiet run writes NOTHING to the log (48 idle lines
  # a day would rotate the 500-line log past the brief's scan window).
  rm -f "$STATE/.sweep.trigger" 2>/dev/null
  if [ "$N_READY" -gt 0 ] || [ "$FAILED" -gt 0 ] || [ "$N_STALLED" -gt 0 ] || [ "$N_UNKNOWN" -gt 0 ]; then
    bf_log "run: ready=$N_READY pushed=$PUSHED partial=$PARTIAL failed=$FAILED deferred=$((DEFERRED + LIMITED)) busy=$((N_BUSY + BUSY)) stalled=$N_STALLED unknown=$N_UNKNOWN live=$N_LIVE unflushed=$B_UNFLUSHED stale=$T_STALE diverged=$T_DIVERGED behind=$T_BEHIND"
  fi
fi
exit 0

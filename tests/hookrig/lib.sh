#!/usr/bin/env bash
# Shared helpers for the hook-rig cases. Sourced INSIDE the container only.
#
# Every case runs in a fresh container (fresh HOME, fresh filesystem) and talks
# only to the stub on the --internal docker network. Nothing here may reach the
# host or the live KB.
#
# Contract for a case script: source this file, run assertions, call `finish`.
# Exit 0 = PASS, non-zero = FAIL.

STUB="${STUB:-http://hookrig-stub:8000}"
RIG_TOKEN="${RIG_TOKEN:-kbrig_0000000000000000000000000000}"
SKILL="${SKILL:-/skill}"
# The RIG's own JSON tools. On the base image these are the PATH jq/python3; on
# the nojq/nojson image variants the image sets them to /rigbin/... -- a location
# deliberately NOT on any PATH, so the harness can still build fixtures and
# assert on a box where the code UNDER TEST can find no JSON tool at all.
# Everything in this file must use these, never a bare `jq`/`python3`.
RIG_JQ="${RIG_JQ:-jq}"
RIG_PY="${RIG_PY:-python3}"
FAILURES=0
CHECKS=0

# ------------------------------------------------------------------ assertions
pass() { CHECKS=$((CHECKS + 1)); printf '    ok   %s\n' "$*"; }
fail() {
  CHECKS=$((CHECKS + 1))
  FAILURES=$((FAILURES + 1))
  printf '    FAIL %s\n' "$*"
}
note() { printf '    --   %s\n' "$*"; }
step() { printf '  [%s]\n' "$*"; }

assert_eq() { # expected actual label
  if [ "$1" = "$2" ]; then pass "$3"; else fail "$3 -- expected [$1], got [$2]"; fi
}
assert_ne() {
  if [ "$1" != "$2" ]; then pass "$3"; else fail "$3 -- did not expect [$1]"; fi
}
assert_contains() { # haystack needle label
  case "$1" in *"$2"*) pass "$3" ;; *) fail "$3 -- [$2] not found in [$(printf '%.200s' "$1")]" ;; esac
}
assert_not_contains() {
  case "$1" in *"$2"*) fail "$3 -- [$2] unexpectedly present" ;; *) pass "$3" ;; esac
}
assert_file_contains() { # file needle label
  if [ -f "$1" ] && grep -qF -- "$2" "$1"; then pass "$3"; else fail "$3 -- [$2] not in $1"; fi
}
assert_file_missing() { if [ -e "$1" ]; then fail "$2 -- $1 exists"; else pass "$2"; fi; }
# "Not advanced" for an OFFSET file: since wave C the hook writes a 0 checkpoint at the
# first trusted sight of a transcript (Appendix B writer W2, KB 34161), so a failed first
# chunk leaves the file present with 0 rather than absent. Either is "nothing accepted".
assert_offset_not_advanced() { # offset-file label
  if [ ! -e "$1" ] || [ "$(cat "$1" 2>/dev/null)" = "0" ]; then pass "$2"; else fail "$2 -- $1 = $(cat "$1" 2>/dev/null)"; fi
}
assert_empty_file() { # file label
  local n
  n=$(wc -c <"$1" 2>/dev/null || echo missing)
  if [ "$n" = 0 ]; then pass "$2"; else fail "$2 -- $1 has $n bytes: [$(head -c 200 "$1")]"; fi
}
assert_le() { # actual limit label
  if [ "$1" -le "$2" ]; then pass "$3"; else fail "$3 -- $1 > $2"; fi
}
assert_ge() {
  if [ "$1" -ge "$2" ]; then pass "$3"; else fail "$3 -- $1 < $2"; fi
}

finish() {
  printf '  -- %d checks, %d failures\n' "$CHECKS" "$FAILURES"
  [ "$FAILURES" -eq 0 ] || exit 1
  exit 0
}

# ------------------------------------------------------------------- stub side
RIG_CURL="${RIG_CURL:-curl}"
stub_reset() { "$RIG_CURL" -sS -X POST "$STUB/__reset" >/dev/null; }
# Make the NEXT enrolment redeem to a PROJECT-SCOPED credential -- the DEFAULT
# setup path, which writes kb-tokens.env and deliberately writes NO shared
# KB_TOKEN. Every "a bare $KB_TOKEN is unbound here" bug only reproduces in that
# shape, so the rig has to be able to produce it.
stub_enrol_project() { # <project>
  "$RIG_CURL" -sS -X POST "$STUB/__enrol_project" --data "$1" >/dev/null
}
stub_requests() { "$RIG_CURL" -sS "$STUB/__requests"; }
# Fault injection (C6/C10): make the NEXT <times> ingest-session answers fail
# with HTTP <code>; code 0 closes the connection with no response (curl 000).
# Every injected hit is still recorded with "injected": <code>, so
# `stub_count '.injected'` counts the attempts a retry chain made. The gate
# sits BEFORE the bearer check, so an unauthenticated request spends a slot too.
# The stub answers 400 for a malformed body (a non-int times/code); the HTTP
# code is checked HERE so a typo becomes a failed check on the spot, never a
# silently unarmed knob that surfaces one step later as a wrong retry count.
# (Checked via %{http_code}, NOT `curl -f`: -f hangs up on the 400 body and
# the stub then logs a ConnectionResetError traceback that reads like a crash.)
stub_knob() { # <path> <json-body> <label> -> 0 on 200, else fail + 1
  local code
  code=$("$RIG_CURL" -sS -o /dev/null -w '%{http_code}' -X POST "$STUB/$1" \
    --data "$2" 2>/dev/null) || code=000
  [ "$code" = 200 ] || { fail "$3: the stub refused $2 (http $code)"; return 1; }
}
stub_fail() { # <times> [code=502]
  stub_knob __fail "{\"times\": ${1:-1}, \"code\": ${2:-502}}" stub_fail
}
# Latency injection: delay every ingest answer by <ms>. `stub_delay 0` = no
# delay at all; `stub_delay` with NO argument sends {} and restores the stub's
# own default (its RIG_DELAY_MS env -- the lane think-time wincase 08 relies
# on), which is also what stub_reset restores. Fails loudly like stub_fail.
stub_delay() { # [ms]
  local body='{}'
  [ -n "${1:-}" ] && body="{\"ms\": $1}"
  stub_knob __delay "$body" stub_delay
}
stub_count() { # jq-filter -> count of matching records
  stub_requests | "$RIG_JQ" -c "select($1)" 2>/dev/null | wc -l | tr -d ' '
}
stub_total() { stub_requests | grep -c . || true; }
stub_field() { # jq-filter jq-expr -> values, one per line
  stub_requests | "$RIG_JQ" -r "select($1) | $2"
}

# Concatenate a field across records with NO separator. stub_field uses `jq -r`,
# which terminates every value with a newline — invisible on scalars, but when
# reassembling N transcript chunks it silently inserts N extra bytes and turns a
# byte-exact comparison into a mystery diff at the first chunk boundary.
stub_concat() { # jq-filter jq-expr -> values, joined with nothing
  stub_requests | "$RIG_JQ" -j "select($1) | $2"
}

wait_stub() { # min-records [timeout-s] -- wait for the DETACHED hook child to land
  local want="$1" t="${2:-25}" i=0 n
  while [ "$i" -lt "$((t * 4))" ]; do
    n=$(stub_total)
    [ "${n:-0}" -ge "$want" ] && return 0
    i=$((i + 1))
    sleep 0.25
  done
  return 1
}

log_count() { # pattern -> how many matching lines the flush log has
  # grep -c prints 0 AND exits 1 on no match, so the old `|| echo 0` printed "0" twice
  # (an `assert_eq 0 $(log_count …)` could never pass). One number, always.
  [ -f "$LOGF" ] || { echo 0; return 0; }
  _lc=$(grep -cE "$1" "$LOGF" 2>/dev/null) || true
  echo "${_lc:-0}"
}

# Wait for a FIRING to finish, not merely to start. Every firing ends with exactly
# one summary line (" ok bytes=" when drained, " partial bytes=" when it stopped
# early), so counting those is the only reliable completion signal for a hook that
# detaches. Waiting on "a submission arrived" instead lets a case fire again while
# the previous child is still draining: both children then read the SAME offset
# file and re-send the same chunk, which looks exactly like a product bug.
wait_firing() { # previous-summary-count [timeout-s]
  local want=$(( ${1:-0} + 1 )) t="${2:-120}" i=0
  while [ "$i" -lt "$((t * 4))" ]; do
    [ "$(log_count ' (ok|partial) bytes=')" -ge "$want" ] && return 0
    i=$((i + 1))
    sleep 0.25
  done
  return 1
}

wait_log() { # pattern [timeout-s]
  local pat="$1" t="${2:-25}" i=0
  while [ "$i" -lt "$((t * 4))" ]; do
    [ -f "$LOGF" ] && grep -qE "$pat" "$LOGF" && return 0
    i=$((i + 1))
    sleep 0.25
  done
  return 1
}

settle() { sleep 1.5; } # let a detached child that should do NOTHING prove it

# --------------------------------------------------------------- connector relay
# CONNECTOR mode's data path is a headless `claude -p` invoked as "$CLAUDE_BIN".
# That is just a path to an executable, so the ENTIRE chunk loop is exercisable
# hermetically by standing a recorder in its place — no egress, no Claude usage,
# and deterministic chunk boundaries. run-connector.sh still proves the REAL
# relay reaches claude.ai; these helpers pin what it does with the bytes.
RELAY_DIR_NAME="relay"

fake_claude_relay() { # a `claude` that RECORDS every relayed chunk, in order
  mkdir -p "$HOME/$RELAY_DIR_NAME"
  cat >"$HOME/bin/claude" <<'RELAYEOF'
#!/usr/bin/env bash
# Stand-in for the headless `claude -p` relay. Records each invocation's stdin
# (the chunk as it went on the wire) plus its argv, then answers like the real
# thing: a bare queue id on success.
D="$HOME/relay"
mkdir -p "$D"
N=$(( $(cat "$D/.n" 2>/dev/null || echo 0) + 1 ))
printf '%s' "$N" >"$D/.n"
cat >"$D/chunk.$N"
printf '%s\n' "$*" >"$D/argv.$N"
# KB_RIG_FAIL_AT=<n>  -> the nth invocation reports failure the way the model would.
# KB_RIG_FAIL_MODE    -> what it says (default FAILED); use to exercise each
#                        branch of the hook's output classification.
if [ -n "${KB_RIG_FAIL_AT:-}" ] && [ "$N" = "$KB_RIG_FAIL_AT" ]; then
  printf '%s\n' "${KB_RIG_FAIL_MODE:-FAILED}"
  exit 0
fi
echo $((4000 + N))
RELAYEOF
  chmod +x "$HOME/bin/claude"
}

relay_count() { # how many chunks the relay has been handed so far
  cat "$HOME/$RELAY_DIR_NAME/.n" 2>/dev/null || echo 0
}

relay_chunk() { # <n> -- the exact bytes of the nth relayed chunk
  cat "$HOME/$RELAY_DIR_NAME/chunk.$1" 2>/dev/null
}

relay_bytes() { # <n> -- size of the nth relayed chunk
  wc -c <"$HOME/$RELAY_DIR_NAME/chunk.$1" 2>/dev/null | tr -d " "
}

relay_concat() { # every chunk, in order -- must reproduce the delta EXACTLY
  local n i
  n=$(relay_count)
  for i in $(seq 1 "${n:-0}"); do cat "$HOME/$RELAY_DIR_NAME/chunk.$i"; done
}

relay_reset() { rm -rf "$HOME/$RELAY_DIR_NAME"; mkdir -p "$HOME/$RELAY_DIR_NAME"; }

wait_relay() { # want-chunks [timeout-s] -- the relay runs in the DETACHED child
  local want="$1" t="${2:-30}" i=0
  while [ "$i" -lt "$((t * 4))" ]; do
    [ "$(relay_count)" -ge "$want" ] && return 0
    i=$((i + 1))
    sleep 0.25
  done
  return 1
}

cfg_connector() { # CONNECTOR config pointing at a given claude binary
  printf '# CONNECTOR\nKB_CONNECTOR=1\nCLAUDE_BIN=%s\n' "${1:-$HOME/bin/claude}" | write_cfg
}

read_offset() { # the single .offset file for this HOME (or 0)
  local f
  f=$(ls "$HOOKS/state/"*.offset 2>/dev/null | head -1)
  [ -n "$f" ] && cat "$f" || echo 0
}

offset_file() { ls "$HOOKS/state/"*.offset 2>/dev/null | head -1; }

# ------------------------------------------------------------------- host side
# Where clean-room HOMEs live and what PATH a case starts with. Defaults are the
# container's; the WINDOWS lane (run-windows.sh) overrides both so the SAME
# assertions run against real Git Bash, where HOME lives under the user profile
# and PATH must keep the Windows entries that make jq/python/curl resolvable.
RIG_HOMEBASE="${RIG_HOMEBASE:-/home/tester}"
RIG_BASEPATH="${RIG_BASEPATH:-/usr/local/bin:/usr/bin:/bin}"

new_home() { # name -- pristine HOME so every install is clean-room
  export HOME="$RIG_HOMEBASE/$1"
  mkdir -p "$HOME/bin" "$HOME/work"
  export PATH="$HOME/bin:$RIG_BASEPATH"
  export LOGF="$HOME/.claude/hooks/flush-session.log"
  export HOOKS="$HOME/.claude/hooks"
  export CFG="$HOME/.claude/kb-flush.env"
  export SETTINGS="$HOME/.claude/settings.json"
  cd "$HOME" || exit 1
}

install_hooks() { bash "$SKILL/install.sh" "$@"; }

# Config writers. Written BEFORE install.sh so the installer keeps them
# (install.sh only authors a config when none exists) -- that is the real
# "already configured machine" path.
write_cfg() {
  mkdir -p "$HOME/.claude"
  cat >"$CFG"
  chmod 600 "$CFG"
}
cfg_mcp() { printf '# MCP\nKB_MCP_URL=%s/mcp\nKB_TOKEN=%s\n' "$STUB" "${1:-$RIG_TOKEN}" | write_cfg; }
cfg_rest() { printf '# REMOTE\nKB_REST_URL=%s\nKB_TOKEN=%s\n' "${2:-$STUB}" "${1:-$RIG_TOKEN}" | write_cfg; }
cfg_local() { printf '# LOCAL\nKB_DIR=%s\nUV=%s\n' "$1" "$2" | write_cfg; }

# Fakes for the transport PRECONDITIONS the installer probes.
fake_uv() { # a `uv` on PATH; records every invocation to $HOME/uv-ran
  cat >"$HOME/bin/uv" <<'EOF'
#!/usr/bin/env bash
echo "uv $*" >>"$HOME/uv-ran"
exit 0
EOF
  chmod +x "$HOME/bin/uv"
}
fake_claude() { # a `claude` CLI on PATH (CONNECTOR precondition)
  cat >"$HOME/bin/claude" <<'EOF'
#!/usr/bin/env bash
echo "claude $*" >>"$HOME/claude-ran"
echo 4242
EOF
  chmod +x "$HOME/bin/claude"
}
fake_kb_repo() { # the LOCAL precondition: a dir holding pyproject.toml
  mkdir -p "${1:-$HOME/data-server}"
  printf '[project]\nname = "kb"\n' >"${1:-$HOME/data-server}/pyproject.toml"
}
fake_claude_json() { # the MCP precondition: the REAL connector export shape
  cat >"$HOME/.claude.json" <<EOF
{
  "numStartups": 7,
  "mcpServers": {
    "kb": {
      "type": "http",
      "url": "https://kb.example.invalid/mcp",
      "headers": {"Authorization": "Bearer kbfake_00000000000000000000000000"}
    }
  }
}
EOF
}

# ------------------------------------------- windows (Git Bash) simulation fakes
# Used by cases 23, 25 and 36. These are the parts of Windows a case CAN fake
# from its own $HOME (they land in $HOME/bin, which new_home puts first on PATH).
# The three parts it cannot -- no setsid on PATH, no flock on PATH, and a
# writable /c so that a converted C:\ path resolves to a real file -- come from
# Dockerfile.winsim.
fake_uname_windows() { # `uname -s` -> MINGW64_NT-10.0, exactly like Git Bash
  cat >"$HOME/bin/uname" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
  -s) echo "MINGW64_NT-10.0-22631" ;;
  *) exec /usr/bin/uname "$@" ;;
esac
EOF
  chmod +x "$HOME/bin/uname"
}

fake_cygpath() { # the real Git Bash mapping: C:\ <-> /c/, bash under Program Files
  cat >"$HOME/bin/cygpath" <<'EOF'
#!/usr/bin/env bash
echo "cygpath $*" >>"$HOME/cygpath.calls"
mode=-u; p=""
for a in "$@"; do case "$a" in -u | -w) mode=$a ;; *) p=$a ;; esac; done
if [ "$mode" = -w ]; then
  case "$p" in
    /usr/bin/bash | /bin/bash) printf '%s\n' 'C:\Program Files\Git\usr\bin\bash.exe' ;;
    /c/*) printf 'C:%s\n' "$(printf '%s' "${p#/c}" | tr '/' '\\')" ;;
    *) printf 'C:\\Program Files\\Git%s\n' "$(printf '%s' "$p" | tr '/' '\\')" ;;
  esac
else
  printf '/%s%s\n' "$(printf '%s' "$p" | cut -c1 | tr '[:upper:]' '[:lower:]')" \
    "$(printf '%s' "$p" | cut -c3- | tr '\\' '/')"
fi
EOF
  chmod +x "$HOME/bin/cygpath"
}

fake_win32_timeout() { # C:\Windows\System32\timeout.exe -- a WAIT command that
  # cannot run a program and rejects redirected stdin. Every invocation is
  # recorded so a case can prove the hooks never used it AS a GNU timeout.
  cat >"$HOME/bin/timeout" <<'EOF'
#!/usr/bin/env bash
echo "timeout $*" >>"$HOME/win-timeout.calls"
if [ ! -t 0 ]; then
  echo "ERROR: Input redirection is not supported, exiting the process immediately." >&2
  exit 1
fi
echo "Waiting for ${1:-0} seconds, press a key to continue ..."
exit 0
EOF
  chmod +x "$HOME/bin/timeout"
}

record_nohup() { # log which detach command the hook actually chose
  cat >"$HOME/bin/nohup" <<'EOF'
#!/usr/bin/env bash
echo "nohup $*" >>"$HOME/detach.calls"
exec /usr/bin/nohup "$@"
EOF
  chmod +x "$HOME/bin/nohup"
}
expose_setsid() { # POSITIVE CONTROL: put setsid back (winsim parks it in /rigbin)
  cat >"$HOME/bin/setsid" <<'EOF'
#!/usr/bin/env bash
echo "setsid $*" >>"$HOME/detach.calls"
exec /rigbin/setsid "$@"
EOF
  chmod +x "$HOME/bin/setsid"
}
expose_flock() { # POSITIVE CONTROL: put flock back (winsim parks it in /rigbin)
  # Git Bash has no flock, so on the winsim image the hooks' session lock must
  # take the mkdir path (kb_lock_take, proposal_v2 Appendix B). Re-exposing the
  # real binary through $HOME/bin is how case 36 proves the flock path still
  # wins where flock exists. The wrapper inherits fd 9, which is the whole
  # point: `exec 9>lock; flock -n 9` works through it unchanged.
  cat >"$HOME/bin/flock" <<'EOF'
#!/usr/bin/env bash
echo "flock $*" >>"$HOME/lock.calls"
exec /rigbin/flock "$@"
EOF
  chmod +x "$HOME/bin/flock"
}

# --------------------------------------------------------------- sweep clock
# kb-flush-backfill.sh (stage 2, item C7) reads KB_BACKFILL_NOW -- epoch seconds
# -- instead of `date +%s` when it is set, so a case can pin "now" and assert the
# quiet window, the stall back-off and the 24 h state sweep without sleeping.
# The hooks are fired from THIS shell (fire/install_hooks), so an exported value
# passes straight through; these two keep the export explicit in the case source.
pin_sweep_clock() { # <epoch-seconds>
  export KB_BACKFILL_NOW="$1"
}
unpin_sweep_clock() { unset KB_BACKFILL_NOW; }

# Extract the shared portable block out of a skill script so its helpers can be
# unit-tested directly (and diffed across the four copies).
extract_block() { # script-path outfile
  sed -n '/^# >>> KB PORTABLE BLOCK/,/^# <<< END KB PORTABLE BLOCK/p' "$1" >"$2"
}

# ------------------------------------------------------------------ transcripts
# Every fixture record carries a "timestamp" (ISO-8601 Z, ms precision), exactly
# like a real Claude Code transcript (all 7 live transcripts on the owner box do,
# hybrid_v1 s2.4). The wave-C hook reads the FIRST record's timestamp to decide
# whether a transcript with no offset file predates `.checkpoints-trusted-since`
# (the unseeded refusal, C1) -- a fixture without one would be refused on every
# first firing. Default: 1 s AHEAD of the case's clock, so a fixture built in the
# same wall-clock second as install.sh's stamp still compares strictly newer.
# RIG_FIXTURE_TS overrides it (e.g. "2020-01-01T00:00:00.000Z" for a PRE-stamp
# transcript, which is how case 29 provokes the refusal).
mk_transcript() { # path n-lines [tag]
  "$RIG_PY" - "$1" "$2" "${3:-line}" "${RIG_FIXTURE_TS:-}" <<'EOF'
import json, sys
from datetime import datetime, timedelta, timezone
path, n, tag, ts = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
if not ts:
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
with open(path, "w", encoding="utf-8") as fh:
    for i in range(n):
        rec = {"type": "user", "timestamp": ts, "tag": tag, "i": i, "text": f"{tag} message {i}"}
        fh.write(json.dumps(rec) + "\n")
EOF
}

mk_big_transcript() { # path total-bytes -- ragged line lengths so the 900000
  "$RIG_PY" - "$1" "$2" "${RIG_FIXTURE_TS:-}" <<'EOF'
import json, sys
from datetime import datetime, timedelta, timezone
path, target, ts = sys.argv[1], int(sys.argv[2]), sys.argv[3]
if not ts:
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
# byte window NEVER lands on a line boundary by luck
written, i = 0, 0
with open(path, "w", encoding="utf-8") as fh:
    while written < target:
        pad = "x" * (37 + (i * 13) % 91)
        line = json.dumps({"type": "assistant", "timestamp": ts, "i": i, "pad": pad}) + "\n"
        fh.write(line)
        written += len(line)
        i += 1
EOF
}

fire() { # script-path event-json -- feed a hook event, capture stdout/rc
  local script="$1" payload="$2"
  OUT_FILE="$HOME/.stdout.$$"
  printf '%s' "$payload" | "$script" >"$OUT_FILE" 2>"$HOME/.stderr.$$"
  RC=$?
  OUT=$(cat "$OUT_FILE")
  export RC OUT OUT_FILE
}

fire_guarded() { # like fire(), but with KB_HOOK_GUARD exported for that firing only
  export KB_HOOK_GUARD="${KB_HOOK_GUARD_VALUE:-1}"
  fire "$@"
  unset KB_HOOK_GUARD
}

flush_event() { # transcript cwd session-id [event]
  "$RIG_JQ" -nc --arg t "$1" --arg c "$2" --arg s "$3" --arg e "${4:-PreCompact}" \
    '{transcript_path:$t,cwd:$c,session_id:$s,hook_event_name:$e}'
}
prompt_event() { # prompt cwd
  "$RIG_JQ" -nc --arg p "$1" --arg c "$2" \
    '{hook_event_name:"UserPromptSubmit",prompt:$p,cwd:$c,session_id:"rig"}'
}
start_event() { # source cwd
  "$RIG_JQ" -nc --arg s "$1" --arg c "$2" \
    '{hook_event_name:"SessionStart",source:$s,cwd:$c,session_id:"rig"}'
}

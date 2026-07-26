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
stub_requests() { "$RIG_CURL" -sS "$STUB/__requests"; }
stub_count() { # jq-filter -> count of matching records
  stub_requests | "$RIG_JQ" -c "select($1)" 2>/dev/null | wc -l | tr -d ' '
}
stub_total() { stub_requests | grep -c . || true; }
stub_field() { # jq-filter jq-expr -> values, one per line
  stub_requests | "$RIG_JQ" -r "select($1) | $2"
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
# Used by cases 23 and 25. These are the parts of Windows a case CAN fake from
# its own $HOME (they land in $HOME/bin, which new_home puts first on PATH). The
# two parts it cannot -- no setsid on PATH, and a writable /c so that a converted
# C:\ path resolves to a real file -- come from Dockerfile.winsim.
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

# Extract the shared portable block out of a skill script so its helpers can be
# unit-tested directly (and diffed across the four copies).
extract_block() { # script-path outfile
  sed -n '/^# >>> KB PORTABLE BLOCK/,/^# <<< END KB PORTABLE BLOCK/p' "$1" >"$2"
}

# ------------------------------------------------------------------ transcripts
mk_transcript() { # path n-lines [tag]
  "$RIG_PY" - "$1" "$2" "${3:-line}" <<'EOF'
import json, sys
path, n, tag = sys.argv[1], int(sys.argv[2]), sys.argv[3]
with open(path, "w", encoding="utf-8") as fh:
    for i in range(n):
        fh.write(json.dumps({"type": "user", "tag": tag, "i": i, "text": f"{tag} message {i}"}) + "\n")
EOF
}

mk_big_transcript() { # path total-bytes -- ragged line lengths so the 900000
  "$RIG_PY" - "$1" "$2" <<'EOF'
import json, sys
path, target = sys.argv[1], int(sys.argv[2])
# byte window NEVER lands on a line boundary by luck
written, i = 0, 0
with open(path, "w", encoding="utf-8") as fh:
    while written < target:
        pad = "x" * (37 + (i * 13) % 91)
        line = json.dumps({"type": "assistant", "i": i, "pad": pad}) + "\n"
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

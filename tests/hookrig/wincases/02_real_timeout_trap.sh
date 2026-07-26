#!/usr/bin/env bash
# CASE: the REAL C:\Windows\System32\timeout.exe trap
# PINS: the single most valuable thing the Windows lane adds. On Windows,
#       `timeout` is ALSO a Microsoft utility that WAITS — it cannot run a
#       command and it dies on redirected stdin with "Input redirection is not
#       supported". A hook that trusts it turns every bounded call (pre-brief,
#       recall, the MCP handshake) into a silent no-op: exit 0, no output, no
#       log, forever — on the one platform this code exists for.
#       The container fakes that binary with a shell script. Here it is the
#       genuine Microsoft one, so DETECTION is tested against the real failure.
# NOTE: Git Bash puts its own coreutils /usr/bin/timeout ahead of System32, so
#       the default PATH does NOT reproduce the trap. Claude Code spawns hooks
#       through CreateProcess with the SYSTEM PATH, where System32 wins — so the
#       trap is reproduced explicitly below with a System32-first PATH. Testing
#       only the default PATH would have declared this safe while shipping the
#       exact configuration that breaks.
. "$RIG_SANDBOX/winlib.sh"

new_home timeouttrap
load_portable_block

# ---------------------------------------------------------------- the premise
step "the genuine Microsoft timeout.exe exists on this box"
SYS32_TIMEOUT="/c/Windows/System32/timeout.exe"
if [ -f "$SYS32_TIMEOUT" ]; then
  pass "found $SYS32_TIMEOUT (the real trap binary)"
else
  fail "no System32 timeout.exe — this box cannot exercise the trap"
fi

step "it really does refuse to run a command with stdin redirected"
NAIVE=$("$SYS32_TIMEOUT" 1 echo hello </dev/null 2>&1 || true)
note "\`timeout.exe 1 echo hello </dev/null\` -> ${NAIVE:-<no output>}"
if printf '%s' "$NAIVE" | grep -q "hello"; then
  fail "System32 timeout.exe RAN the command — the premise no longer holds"
else
  pass "it did NOT run the command (exactly the silent no-op we must avoid)"
fi

# ------------------------------------------- the trap, on a System32-first PATH
# This is the configuration Claude Code actually spawns hooks under. The probe
# is invoked in THIS shell (never inside $(...)), because kb_timeout memoises
# its choice in a global — a subshell would discard it and leave the assertion
# below inspecting an empty variable.
step "with System32 FIRST on PATH, kb_timeout must still refuse the trap"
(
  export PATH="/c/Windows/System32:/usr/bin:/bin"
  KB_TIMEOUT_PROBED=0
  KB_TIMEOUT_BIN=""
  RESOLVED=$(command -v timeout 2>/dev/null || true)
  echo "    --   PATH timeout now resolves to: ${RESOLVED:-MISSING}"
  case "$RESOLVED" in
    */System32/*|*/system32/*) echo "    ok   the trap binary is what PATH offers" ;;
    *) echo "    --   PATH still prefers $RESOLVED" ;;
  esac

  # Force the probe in THIS shell, then read the memoised choice.
  kb_timeout 5 true </dev/null >/dev/null 2>&1
  echo "    --   kb_timeout selected: ${KB_TIMEOUT_BIN:-<none: perl watchdog>}"
  case "${KB_TIMEOUT_BIN:-}" in
    */System32/*|*/system32/*)
      echo "    FAIL kb_timeout SELECTED Windows timeout.exe — every bounded call is now a silent no-op"
      exit 1 ;;
    *) echo "    ok   kb_timeout refused the trap binary" ;;
  esac

  # And it must still actually work under that PATH.
  OUT=$(kb_timeout 5 echo bounded-ok </dev/null 2>&1)
  [ "$OUT" = "bounded-ok" ] || { echo "    FAIL kb_timeout did not run the command: [$OUT]"; exit 1; }
  echo "    ok   kb_timeout still runs commands on a System32-first PATH"

  T0=$(date +%s)
  kb_timeout 2 sleep 20 </dev/null >/dev/null 2>&1
  RC=$?
  ELAPSED=$(( $(date +%s) - T0 ))
  echo "    --   sleep 20 under kb_timeout 2 -> rc=$RC after ${ELAPSED}s"
  [ "$ELAPSED" -le 12 ] || { echo "    FAIL the bound did not hold (${ELAPSED}s)"; exit 1; }
  [ "$RC" -ne 0 ] || { echo "    FAIL a timed-out command reported success"; exit 1; }
  echo "    ok   the bound is real under the trap PATH"
) || fail "kb_timeout mishandled the System32-first PATH"
pass "System32-first PATH handled correctly"

# ------------------------------------------------- the default Git Bash PATH
step "on the DEFAULT Git Bash PATH, whatever is chosen must behave"
KB_TIMEOUT_PROBED=0
KB_TIMEOUT_BIN=""
kb_timeout 5 true </dev/null >/dev/null 2>&1   # probe in THIS shell, not a subshell
note "default-PATH backend: ${KB_TIMEOUT_BIN:-<none: perl watchdog>}"
case "${KB_TIMEOUT_BIN:-}" in
  */System32/*|*/system32/*) fail "kb_timeout selected Windows timeout.exe on the default PATH" ;;
  "") pass "no PATH timeout trusted; the perl watchdog bounds calls" ;;
  *) pass "kb_timeout selected a trustworthy binary: $KB_TIMEOUT_BIN" ;;
esac

OUT=$(kb_timeout 5 echo bounded-ok </dev/null 2>&1)
assert_eq "bounded-ok" "$OUT" "kb_timeout actually RUNS the command"

step "stdin-redirected calls work — the exact shape the hooks use"
OUT=$(printf 'piped\n' | kb_timeout 5 cat 2>&1)
assert_eq "piped" "$OUT" "kb_timeout passes stdin through (the hook pipes JSON in)"

step "the bound is REAL and the exit status is faithful"
T0=$(date +%s)
kb_timeout 2 sleep 20 </dev/null >/dev/null 2>&1
RC=$?
ELAPSED=$(( $(date +%s) - T0 ))
note "sleep 20 under kb_timeout 2 -> rc=$RC after ${ELAPSED}s"
assert_le "$ELAPSED" 12 "a 20s command was actually cut short"
assert_ne "0" "$RC" "a timed-out command does not report success"

kb_timeout 5 sh -c 'exit 7' </dev/null >/dev/null 2>&1
assert_eq "7" "$?" "kb_timeout propagates the child's exit code"
kb_timeout 5 true </dev/null >/dev/null 2>&1
assert_eq "0" "$?" "kb_timeout propagates success"

finish

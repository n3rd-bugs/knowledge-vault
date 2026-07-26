#!/usr/bin/env bash
# IMAGE: winsim
# CASE: WINDOWS / Git Bash, simulated end to end
# PINS: kb-system item 1153 (Windows/REMOTE) and item 496. The skill has carried
#       Windows-specific code for a long time and NONE of it was ever executed by
#       a test -- it was field folklore. Everything below is now executable:
#   1. `uname -s` = MINGW*/MSYS*/CYGWIN* is the ONE platform switch, centralised
#      in the shared portable block (kb_is_windows), identical in all four
#      scripts. This case fakes uname; nothing else selects the Windows path.
#   2. Claude Code on Windows spawns a hook as exe+args, NOT through a shell, so
#      a bare .sh command never runs. install.sh must register bash.exe as the
#      command with the script as args[0] -- for ALL FOUR events (item 1152).
#   3. Native event paths (C:\Users\First Last\...) must be converted before
#      basename/-f: a backslash path yields the whole drive path otherwise. Here
#      cygpath does it (case 25 pins the no-cygpath fallback).
#   4. PATH `timeout` on Windows is C:\Windows\System32\timeout.exe -- a WAIT
#      utility that cannot run a command and fails on redirected stdin. It must
#      be DETECTED and refused, or every bounded call becomes a silent no-op and
#      pre-brief/recall die quietly on the one platform this all exists for.
#   5. setsid does not exist on Git Bash: detach must fall back to nohup.
#      Asserted with a positive control (setsid back on PATH -> setsid is used).
# NOTE: a path with a SPACE ("First Last") is used throughout on purpose -- the
#       normal Windows reality, and the reason every path in the hooks is quoted.
# NOTE: this case also PASSES against the pre-portability skill, and that is the
#       point: the Windows code existed, was never once executed by a test, and
#       is now a regression gate. Case 25 is the half that the old code failed.
. /rig/lib.sh

new_home winsim
fake_uname_windows
fake_cygpath
fake_win32_timeout
record_nohup

step "the box now looks like Git Bash"
assert_eq "MINGW64_NT-10.0-22631" "$(uname -s)" "uname -s reports MINGW"
if command -v setsid >/dev/null 2>&1; then fail "setsid is on PATH -- winsim image is wrong"; else pass "no setsid (Git Bash)"; fi
if command -v cygpath >/dev/null 2>&1; then pass "cygpath present"; else fail "cygpath fake missing"; fi

cfg_mcp
install_hooks >/dev/null

# ------------------------------------- 2. the command must be bash.exe + args[0]
step "install registers bash.exe with the script as an ARGUMENT (not a bare .sh)"
BASHEXE='C:\Program Files\Git\usr\bin\bash.exe'
for ev in PreCompact SessionEnd SessionStart UserPromptSubmit; do
  CMD=$("$RIG_JQ" -r --arg e "$ev" '.hooks[$e][0].hooks[0].command' "$SETTINGS")
  ARG0=$("$RIG_JQ" -r --arg e "$ev" '.hooks[$e][0].hooks[0].args[0]' "$SETTINGS")
  assert_eq "$BASHEXE" "$CMD" "$ev command is the native bash.exe path"
  case "$CMD" in *.sh) fail "$ev command is a bare .sh -- Windows cannot spawn it" ;; *) pass "$ev command is not a .sh" ;; esac
  case "$ARG0" in *.sh) pass "$ev args[0] is the hook script" ;; *) fail "$ev args[0] is [$ARG0]" ;; esac
done
assert_eq "$HOOKS/flush-session-to-kb.sh" "$("$RIG_JQ" -r '.hooks.PreCompact[0].hooks[0].args[0]' "$SETTINGS")" "PreCompact -> flush hook"
assert_eq "$HOOKS/kb-session-brief.sh" "$("$RIG_JQ" -r '.hooks.SessionStart[0].hooks[0].args[0]' "$SETTINGS")" "SessionStart -> brief hook"
assert_eq "startup|clear" "$("$RIG_JQ" -r '.hooks.SessionStart[0].matcher' "$SETTINGS")" "matcher still startup|clear"
assert_eq "15" "$("$RIG_JQ" -r '.hooks.PreCompact[0].hooks[0].timeout' "$SETTINGS")" "timeout still declared"

# ------------------------------------------- 3. native paths, with spaces in them
step "a flush with a C:\\ transcript path and a space in it"
WIN_DIR='C:\Users\First Last\proj'
WIN_T='C:\Users\First Last\proj\t.jsonl'
POSIX_DIR='/c/Users/First Last/proj'
mkdir -p "$POSIX_DIR"
mk_transcript "$POSIX_DIR/t.jsonl" 9 winsim
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WIN_T" "$WIN_DIR" sess-win PreCompact)"
assert_eq "0" "$RC" "hook exits 0"
if wait_stub 1 25; then pass "the C:\\ path resolved and the flush submitted"; else fail "nothing submitted -- the native path did not resolve"; fi
assert_eq "proj" "$(stub_field '.tool=="kb_ingest_session"' '.project')" "project = basename of the CONVERTED cwd"
assert_eq "9" "$(stub_field '.tool=="kb_ingest_session"' '.transcript_lines')" "the whole transcript arrived"
if [ -s "$HOME/cygpath.calls" ]; then pass "cygpath was used: $(head -1 "$HOME/cygpath.calls")"; else fail "cygpath was never called"; fi

# ------------------------------------------------- 5. detach falls back to nohup
step "detach: no setsid -> nohup"
if [ -s "$HOME/detach.calls" ]; then pass "a detach command was recorded"; else fail "nothing recorded"; fi
assert_contains "$(cat "$HOME/detach.calls")" "nohup bash -c" "the hook detached with nohup"
assert_not_contains "$(cat "$HOME/detach.calls")" "setsid" "setsid was not used (it does not exist here)"

step "POSITIVE CONTROL: with setsid back on PATH, setsid is preferred"
expose_setsid
rm -f "$HOME/detach.calls" "$HOME/.claude/hooks/state/"*.offset
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WIN_T" "$WIN_DIR" sess-win2 PreCompact)"
if wait_stub 1 25; then pass "flush still submits under setsid"; else fail "nothing submitted"; fi
assert_contains "$(cat "$HOME/detach.calls")" "setsid bash -c" "setsid used when available"
assert_not_contains "$(cat "$HOME/detach.calls")" "nohup" "nohup is the FALLBACK, not the default"

# ----------------------------------- 4. the Win32 timeout must never be trusted
step "the Win32 timeout.exe is refused as a bounded-run wrapper"
rm -f "$HOME/win-timeout.calls"
stub_reset
fire "$HOOKS/kb-session-brief.sh" "$(prompt_event "explain the chunking offset resume behaviour" "$WIN_DIR")"
assert_contains "$OUT" "KB recall" "recall STILL produced output (it did not silently no-op)"
assert_eq "1" "$(stub_count '.tool=="kb_search"')" "the search really ran"
if [ -f "$HOME/win-timeout.calls" ]; then
  note "timeout invocations: $(tr '\n' ' ' <"$HOME/win-timeout.calls")"
  # Probing it is fine and expected (that is HOW it is rejected). Using it to run
  # the search is the bug: any invocation naming the helper or curl means the
  # hook handed a real command to the Windows WAIT utility.
  if grep -qE 'kb-mcp-call|curl|claude|uv' "$HOME/win-timeout.calls"; then
    fail "the Win32 timeout was used to RUN a command -- that call is a silent no-op on Windows"
  else
    pass "only probed (--version / self-test), never used to run a command"
  fi
  if grep -qE '^timeout (--version|1 true)$' "$HOME/win-timeout.calls"; then
    pass "the probe that rejects it did run"
  else
    fail "no recognisable probe was recorded"
  fi
else
  pass "the Win32 timeout was not invoked at all"
fi

finish

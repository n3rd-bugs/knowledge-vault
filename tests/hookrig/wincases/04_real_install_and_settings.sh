#!/usr/bin/env bash
# CASE: install.sh on a REAL Windows box, and the settings.json it writes
# PINS: kb-system items 496/1152/1153. Claude Code on Windows spawns a hook
#       through CreateProcess, NOT a shell, so a bare `.sh` command is never
#       executed — install.sh must register the native bash.exe as `command`
#       with the script as `args[0]`, for ALL FOUR events. The container proves
#       the branch with a faked uname+cygpath; here the native path comes from
#       the box's OWN cygpath/EXEPATH, so a wrong path shape is caught for real.
. "$RIG_SANDBOX/winlib.sh"

new_home install
load_portable_block
# Probe first — that is exactly what all four shipped scripts do immediately
# after the portable block, and the helpers are undefined behaviour without it.
kb_json_probe || fail "no JSON backend on this box"

# Pre-write a REMOTE config pointing at the local stub, so install.sh keeps it
# (it only authors a config when none exists) — the "already configured machine"
# path, and the one that lets the later cases actually submit.
cfg_rest

step "install.sh runs to completion under real Git Bash"
OUT=$(install_hooks 2>&1)
RC=$?
assert_eq "0" "$RC" "install.sh exits 0"
printf '%s\n' "$OUT" | grep -Ei "mode:|config:" | while read -r l; do note "$l"; done
# With a config already present, install.sh keeps it and prints no "mode:" line
# — so assert the TRANSPORT that survived, which is what actually matters.
assert_contains "$OUT" "kept existing" "install kept the pre-written config"
assert_file_contains "$CFG" "KB_REST_URL=" "the REMOTE transport survived install"

step "the hook scripts landed and are executable"
for f in flush-session-to-kb.sh kb-session-brief.sh kb-mcp-call.sh; do
  if [ -f "$HOOKS/$f" ]; then pass "$f installed"; else fail "$f missing from $HOOKS"; fi
done
if grep -qU $'\r' "$HOOKS/flush-session-to-kb.sh" 2>/dev/null; then
  fail "the INSTALLED hook has CRLF endings — Git Bash cannot run it"
else
  pass "installed hooks are LF-clean (install.sh did not mangle endings)"
fi

step "settings.json is valid JSON and registers all four events"
if kb_json_valid "$SETTINGS"; then pass "settings.json is valid JSON"; else fail "settings.json is not parseable"; fi
for ev in PreCompact SessionEnd SessionStart UserPromptSubmit; do
  CMD=$("$RIG_JQ" -r --arg e "$ev" '.hooks[$e][0].hooks[0].command // ""' "$SETTINGS")
  ARG0=$("$RIG_JQ" -r --arg e "$ev" '.hooks[$e][0].hooks[0].args[0] // ""' "$SETTINGS")
  [ -n "$CMD" ] && pass "$ev registered" || fail "$ev NOT registered"
  case "$CMD" in
    *.sh) fail "$ev command is a bare .sh — Windows CreateProcess cannot spawn it" ;;
    *bash*) pass "$ev command is bash ($CMD)" ;;
    *) fail "$ev command is unexpected: [$CMD]" ;;
  esac
  case "$ARG0" in
    *.sh) pass "$ev args[0] is the hook script" ;;
    *) fail "$ev args[0] is [$ARG0], expected the .sh path" ;;
  esac
done

step "the registered bash path is one Windows can actually execute"
BASHW=$("$RIG_JQ" -r '.hooks.PreCompact[0].hooks[0].command' "$SETTINGS")
note "registered command = $BASHW"
# Either a native path that exists, or a bare exe name Windows resolves on PATH.
case "$BASHW" in
  [A-Za-z]:\\*)
    POSIXED=$(kb_winpath "$BASHW")
    if [ -x "$POSIXED" ]; then
      pass "native path resolves to a real executable ($POSIXED)"
    else
      fail "registered native bash path does not exist: $BASHW -> $POSIXED"
    fi
    ;;
  bash.exe) pass "bare bash.exe (resolved through the Windows PATH)" ;;
  *) fail "registered command has an unusable shape: [$BASHW]" ;;
esac

step "re-running install is idempotent (no duplicate hook entries)"
install_hooks >/dev/null 2>&1
for ev in PreCompact SessionEnd SessionStart UserPromptSubmit; do
  N=$("$RIG_JQ" -r --arg e "$ev" '[.hooks[$e][].hooks[]] | length' "$SETTINGS")
  assert_eq "1" "$N" "$ev still has exactly one hook after a second install"
done

finish

#!/usr/bin/env bash
# CASE: FIVE hook events, ONE dispatcher
# PINS: install.sh registers exactly FIVE Claude Code events, every one wired to
#       kb-hook-dispatch.sh with the event name as its argument. ONE hook per
#       event is load-bearing, not style: Claude Code on Windows loses stdin
#       when several hooks share an event (GH #77078, KB 14508) — the dispatcher
#       reads the event once and fans the bytes out itself, so the per-script
#       entries this case used to pin (item 1152) are exactly the bug.
# ALSO PINS: the SessionStart entry carries matcher "startup|clear" while the
#       other four carry the empty matcher — the brief hook self-filters
#       resume/compact sources, so its per-prompt registration must NOT be
#       matcher-limited.
. /rig/lib.sh

new_home four
install_hooks >/dev/null

step "settings.json wires exactly the five events"
KEYS=$(jq -r '.hooks | keys | join(",")' "$SETTINGS")
assert_eq "PreCompact,SessionEnd,SessionStart,Stop,UserPromptSubmit" "$KEYS" "exactly five event keys"
assert_eq "5" "$(jq -r '.hooks | length' "$SETTINGS")" "no sixth event registered"

DISPATCH="$HOOKS/kb-hook-dispatch.sh"

step "every event carries exactly ONE entry: the dispatcher with the event name"
for ev in PreCompact SessionEnd SessionStart Stop UserPromptSubmit; do
  assert_eq "1" "$(jq -r --arg e "$ev" '.hooks[$e] | length' "$SETTINGS")" "$ev entry count is ONE"
  assert_eq "1" "$(jq -r --arg e "$ev" '.hooks[$e][0].hooks | length' "$SETTINGS")" "$ev inner hook count is ONE"
  assert_eq "$DISPATCH $ev" "$(jq -r --arg e "$ev" '.hooks[$e][0].hooks[0].command' "$SETTINGS")" "$ev -> dispatcher with event arg"
  assert_eq "command" "$(jq -r --arg e "$ev" '.hooks[$e][0].hooks[0].type' "$SETTINGS")" "$ev entry is type=command"
done
assert_eq "startup|clear" "$(jq -r '.hooks.SessionStart[0].matcher' "$SETTINGS")" "SessionStart matcher is startup|clear"
assert_eq "" "$(jq -r '.hooks.UserPromptSubmit[0].matcher' "$SETTINGS")" "UserPromptSubmit matcher is empty (self-filters)"

step "timeouts are declared (a hook Claude Code cannot bound would stall a session)"
assert_eq "15" "$(jq -r '.hooks.PreCompact[0].hooks[0].timeout' "$SETTINGS")" "PreCompact timeout 15s"
assert_eq "15" "$(jq -r '.hooks.SessionEnd[0].hooks[0].timeout' "$SETTINGS")" "SessionEnd timeout 15s"
# The SEARCH events must EXCEED the brief's own 30s budget or Claude Code kills
# the hook first and the internal ceiling is dead code. UserPromptSubmit is 40:
# turn-start's 5 rides in front of the brief's 35 behind the one dispatcher.
assert_eq "35" "$(jq -r '.hooks.SessionStart[0].hooks[0].timeout' "$SETTINGS")" "SessionStart timeout 35s"
assert_eq "40" "$(jq -r '.hooks.UserPromptSubmit[0].hooks[0].timeout' "$SETTINGS")" "UserPromptSubmit timeout 40s"
assert_eq "15" "$(jq -r '.hooks.Stop[0].hooks[0].timeout' "$SETTINGS")" "Stop timeout 15s"

step "the dispatcher and every script it fans out to are installed"
for f in kb-hook-dispatch.sh flush-session-to-kb.sh kb-session-brief.sh kb-mcp-call.sh; do
  if [ -x "$HOOKS/$f" ]; then pass "$f installed +x"; else fail "$f missing or not executable"; fi
done

step "FIVE distinct command strings — one per event, all the same dispatcher"
N=$(jq -r '[.hooks[][].hooks[0].command] | unique | length' "$SETTINGS")
assert_eq "5" "$N" "five events, five dispatcher+event strings"
N=$(jq -r '[.hooks[][].hooks[0].command | split(" ")[0]] | unique | length' "$SETTINGS")
assert_eq "1" "$N" "…and exactly ONE underlying script"

finish

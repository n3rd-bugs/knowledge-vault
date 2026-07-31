#!/usr/bin/env bash
# CASE: FOUR hook events, two scripts
# PINS: kb-system item 1152 -- install.sh registers exactly FOUR Claude Code
#       events: PreCompact + SessionEnd -> flush-session-to-kb.sh, and
#       SessionStart + UserPromptSubmit -> kb-session-brief.sh.
#       CORRECTS item 509, which said three events were registered.
#       Source of truth: install.sh:113-116 (the four jq assignments).
# ALSO PINS: the SessionStart entry carries matcher "startup|clear" while the
#       other three carry the empty matcher (install.sh:100-102) -- the brief
#       hook self-filters resume/compact sources, so its per-prompt registration
#       must NOT be matcher-limited.
. /rig/lib.sh

new_home four
install_hooks >/dev/null

step "settings.json wires exactly the four events"
KEYS=$(jq -r '.hooks | keys | join(",")' "$SETTINGS")
assert_eq "PreCompact,SessionEnd,SessionStart,Stop,UserPromptSubmit" "$KEYS" "exactly five event keys"
assert_eq "5" "$(jq -r '.hooks | length' "$SETTINGS")" "no sixth event registered"

FLUSH="$HOOKS/flush-session-to-kb.sh"
BRIEF="$HOOKS/kb-session-brief.sh"

step "the flush hook owns the two session-capture events"
for ev in PreCompact SessionEnd; do
  # UserPromptSubmit legitimately carries TWO: the KB per-prompt recall AND
  # turn-start.sh, which stamps the turn so the Stop notice can report a duration.
  EXPECT=1; [ "$ev" = "UserPromptSubmit" ] && EXPECT=2
  assert_eq "$EXPECT" "$(jq -r --arg e "$ev" '.hooks[$e] | length' "$SETTINGS")" "$ev entry count"
  assert_eq "$FLUSH" "$(jq -r --arg e "$ev" '.hooks[$e][0].hooks[0].command' "$SETTINGS")" "$ev -> flush hook"
  assert_eq "command" "$(jq -r --arg e "$ev" '.hooks[$e][0].hooks[0].type' "$SETTINGS")" "$ev entry is type=command"
  assert_eq "" "$(jq -r --arg e "$ev" '.hooks[$e][0].matcher' "$SETTINGS")" "$ev matcher is empty"
done

step "the brief hook owns the two recall events"
for ev in SessionStart UserPromptSubmit; do
  # UserPromptSubmit legitimately carries TWO: the KB per-prompt recall AND
  # turn-start.sh, which stamps the turn so the Stop notice can report a duration.
  EXPECT=1; [ "$ev" = "UserPromptSubmit" ] && EXPECT=2
  assert_eq "$EXPECT" "$(jq -r --arg e "$ev" '.hooks[$e] | length' "$SETTINGS")" "$ev entry count"
  assert_eq "$BRIEF" "$(jq -r --arg e "$ev" '.hooks[$e][0].hooks[0].command' "$SETTINGS")" "$ev -> brief hook"
done
assert_eq "startup|clear" "$(jq -r '.hooks.SessionStart[0].matcher' "$SETTINGS")" "SessionStart matcher is startup|clear"
assert_eq "" "$(jq -r '.hooks.UserPromptSubmit[0].matcher' "$SETTINGS")" "UserPromptSubmit matcher is empty (self-filters)"

step "timeouts are declared (a hook Claude Code cannot bound would stall a session)"
assert_eq "15" "$(jq -r '.hooks.PreCompact[0].hooks[0].timeout' "$SETTINGS")" "PreCompact timeout 15s"
assert_eq "15" "$(jq -r '.hooks.SessionEnd[0].hooks[0].timeout' "$SETTINGS")" "SessionEnd timeout 15s"
# The two SEARCH hooks get 35s: the registered timeout must EXCEED the script's
# own budget (30s) or Claude Code kills the hook first and the internal ceiling
# is dead code. The flush hooks keep 15s — they detach immediately.
assert_eq "35" "$(jq -r '.hooks.SessionStart[0].hooks[0].timeout' "$SETTINGS")" "SessionStart timeout 35s"
assert_eq "35" "$(jq -r '.hooks.UserPromptSubmit[0].hooks[0].timeout' "$SETTINGS")" "UserPromptSubmit timeout 35s"

step "all three scripts are installed and executable"
for f in flush-session-to-kb.sh kb-session-brief.sh kb-mcp-call.sh; do
  if [ -x "$HOOKS/$f" ]; then pass "$f installed +x"; else fail "$f missing or not executable"; fi
done

step "exactly TWO distinct hook commands are referenced across the four events"
N=$(jq -r '[.hooks[][].hooks[0].command] | unique | length' "$SETTINGS")
assert_eq "4" "$N" "five events, four scripts"

finish

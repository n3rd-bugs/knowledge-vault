#!/usr/bin/env bash
# CASE: IDEMPOTENT INSTALL
# PINS: re-running install.sh leaves exactly ONE KB entry per event and preserves
#       hooks the user already had (install.sh:109-117 removes any prior copy of
#       OUR entry before appending, and never rewrites foreign entries).
#       This is the property that lets the skill be re-run on every machine
#       update; a regression here silently duplicates every flush.
. /rig/lib.sh

new_home idem
mkdir -p "$HOME/.claude"

# A machine that already has unrelated hooks, including one on an event we claim.
cat >"$SETTINGS" <<'EOF'
{
  "model": "opus",
  "hooks": {
    "PreCompact": [
      {"matcher": "", "hooks": [{"type": "command", "command": "/usr/local/bin/user-precompact.sh"}]}
    ],
    "PostToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/local/bin/user-posttool.sh"}]}
    ]
  }
}
EOF

step "first install"
install_hooks >/dev/null
DISPATCH="$HOOKS/kb-hook-dispatch.sh"
assert_eq "2" "$(jq -r '.hooks.PreCompact | length' "$SETTINGS")" "PreCompact: user entry + ours"

step "second install (idempotency)"
install_hooks >/dev/null
step "third install (paranoia -- duplication is usually visible only at N>2)"
install_hooks >/dev/null

for ev in PreCompact SessionEnd SessionStart Stop UserPromptSubmit; do
  N=$(jq -r --arg e "$ev" --arg c "$DISPATCH $ev" \
    '[.hooks[$e][] | select(.hooks[0].command==$c)] | length' "$SETTINGS")
  assert_eq "1" "$N" "$ev holds exactly one KB dispatcher entry after 3 installs"
done

step "pre-existing NON-KB hooks survive"
assert_eq "2" "$(jq -r '.hooks.PreCompact | length' "$SETTINGS")" "PreCompact still has the user's entry too"
assert_eq "/usr/local/bin/user-precompact.sh" \
  "$(jq -r '.hooks.PreCompact[0].hooks[0].command' "$SETTINGS")" "user's PreCompact hook preserved (and still first)"
assert_eq "/usr/local/bin/user-posttool.sh" \
  "$(jq -r '.hooks.PostToolUse[0].hooks[0].command' "$SETTINGS")" "unrelated PostToolUse hook untouched"
assert_eq "Bash" "$(jq -r '.hooks.PostToolUse[0].matcher' "$SETTINGS")" "unrelated matcher untouched"

step "non-hook settings survive"
assert_eq "opus" "$(jq -r '.model' "$SETTINGS")" "unrelated top-level settings preserved"
assert_eq "true" "$(jq -e . "$SETTINGS" >/dev/null && echo true)" "settings.json is still valid JSON"

step "re-install does not clobber an existing kb-flush.env"
cfg_mcp
install_hooks >/dev/null
assert_file_contains "$CFG" "KB_MCP_URL=$STUB/mcp" "existing transport config preserved"

finish

#!/usr/bin/env bash
# CASE: transport MODE PRIORITY
# PINS: kb-system item 1151 -- install.sh picks LOCAL > MCP > CONNECTOR > REMOTE,
#       evaluated top-down, first precondition that holds wins.
#       CORRECTS item 519, which claimed CONNECTOR was the default mode.
#       Source of truth: install.sh:48-77 (the if/elif chain).
# ALSO PINS (corrections to the field description of the chain):
#   * LOCAL needs BOTH a pyproject.toml dir AND `uv` on PATH (install.sh:52) --
#     either one alone degrades to the next mode.
#   * MCP auto-discovery only accepts a ~/.claude.json server whose KEY matches
#     /kb|knowledge/i (install.sh:28) -- an unrelated http+/mcp+Bearer server is
#     NOT picked up, contrary to "else the first http server ending in /mcp".
#   * REMOTE is an UNCONDITIONAL fallback that writes a PLACEHOLDER
#     (KB_REST_URL=https://api.example.com, empty KB_TOKEN); a KB_REST_URL in the
#     environment does NOT influence installation (install.sh:73-76).
. /rig/lib.sh

mode_of() { # -> LOCAL|MCP|CONNECTOR|REMOTE, read from the config the installer wrote
  sed -n '1p' "$CFG" | sed -n 's/.*(\([A-Z]*\)).*/\1/p'
}

expect_mode() { # expected label
  local want="$1" lbl="$2" got
  got=$(mode_of)
  assert_eq "$want" "$got" "$lbl"
}

# Every step runs with KB_REST_URL exported to prove it never jumps the queue.
export KB_REST_URL="$STUB"

step "1: ALL preconditions present -> LOCAL wins"
new_home p1
export KB_REST_URL="$STUB"
fake_kb_repo "$HOME/data-server"
fake_uv
fake_claude
fake_claude_json
OUT=$(install_hooks)
expect_mode LOCAL "all four preconditions -> LOCAL"
assert_contains "$OUT" "LOCAL enqueue via $HOME/data-server" "installer reports LOCAL"
assert_file_contains "$CFG" "KB_DIR=$HOME/data-server" "config carries KB_DIR"
assert_eq "0" "$(grep -c 'KB_MCP_URL\|KB_CONNECTOR\|KB_REST_URL' "$CFG")" "LOCAL config is single-mode"

step "2a: pyproject.toml present but NO uv -> degrades to MCP (the AND matters)"
new_home p2a
export KB_REST_URL="$STUB"
fake_kb_repo "$HOME/data-server"
fake_claude
fake_claude_json
install_hooks >/dev/null
expect_mode MCP "pyproject without uv -> MCP"

step "2b: uv present but NO pyproject.toml -> degrades to MCP"
new_home p2b
export KB_REST_URL="$STUB"
fake_uv
fake_claude
fake_claude_json
OUT=$(install_hooks)
expect_mode MCP "uv without pyproject -> MCP"
assert_file_contains "$CFG" "KB_MCP_URL=https://kb.example.invalid/mcp" "MCP url discovered from ~/.claude.json"
assert_file_contains "$CFG" "KB_TOKEN=kbfake_00000000000000000000000000" "bearer discovered from ~/.claude.json"
assert_contains "$OUT" "auto-discovered" "installer reports auto-discovery"
assert_eq "0" "$(grep -c 'KB_CONNECTOR\|KB_DIR=' "$CFG")" "MCP config is single-mode"

step "3: LOCAL+MCP preconditions removed, a claude CLI present -> CONNECTOR"
new_home p3
export KB_REST_URL="$STUB"
fake_claude
OUT=$(install_hooks)
expect_mode CONNECTOR "only a claude CLI -> CONNECTOR"
assert_file_contains "$CFG" "KB_CONNECTOR=1" "connector flag set"
assert_file_contains "$CFG" "CLAUDE_BIN=$HOME/bin/claude" "connector binds the discovered CLI"
assert_eq "0" "$(grep -c 'KB_TOKEN\|KB_MCP_URL\|KB_DIR=' "$CFG")" "CONNECTOR config carries no bearer"

step "3b: a ~/.claude.json MCP server NOT named kb/knowledge is ignored -> CONNECTOR"
new_home p3b
export KB_REST_URL="$STUB"
fake_claude
cat >"$HOME/.claude.json" <<'EOF'
{"mcpServers":{"memory-thing":{"type":"http","url":"https://other.example.invalid/mcp",
 "headers":{"Authorization":"Bearer other_0000000000000000000000000000"}}}}
EOF
install_hooks >/dev/null
expect_mode CONNECTOR "non-kb-named mcp server is NOT auto-discovered"
assert_eq "0" "$(grep -c 'other.example.invalid' "$CFG")" "foreign MCP url never written to the config"

step "3c: a kb-named server WITHOUT a Bearer header is ignored -> CONNECTOR"
new_home p3c
export KB_REST_URL="$STUB"
fake_claude
cat >"$HOME/.claude.json" <<'EOF'
{"mcpServers":{"kb":{"type":"http","url":"https://kb.example.invalid/mcp"}}}
EOF
install_hooks >/dev/null
expect_mode CONNECTOR "kb server without a bearer is not usable -> next mode"

step "4: no preconditions at all -> REMOTE placeholder (needs manual KB_TOKEN)"
new_home p4
export KB_REST_URL="$STUB"
OUT=$(install_hooks)
expect_mode REMOTE "nothing present -> REMOTE fallback"
assert_file_contains "$CFG" "KB_REST_URL=https://api.example.com" "REMOTE writes the placeholder url"
assert_eq "0" "$(grep -c "$STUB" "$CFG")" "KB_REST_URL in the ENV does not configure REMOTE"
assert_eq "KB_TOKEN=" "$(grep '^KB_TOKEN=' "$CFG")" "REMOTE leaves KB_TOKEN empty for the operator"
assert_contains "$OUT" "REMOTE — edit" "installer tells the operator to finish setup"

step "5: an EXISTING config is never re-authored (mode is sticky)"
new_home p5
cfg_mcp
fake_kb_repo "$HOME/data-server"
fake_uv
OUT=$(install_hooks)
assert_contains "$OUT" "kept existing" "existing config kept even though LOCAL is now possible"
assert_file_contains "$CFG" "KB_MCP_URL=$STUB/mcp" "config untouched"

step "6: config permissions"
new_home p6
install_hooks >/dev/null
assert_eq "600" "$(stat -c '%a' "$CFG")" "kb-flush.env is 0600 (it can hold a bearer)"

finish

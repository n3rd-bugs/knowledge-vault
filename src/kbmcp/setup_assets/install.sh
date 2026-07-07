#!/usr/bin/env bash
# Install the KB session-flush hook on THIS machine: writes the hook script,
# creates ~/.claude/kb-flush.env (LOCAL if a KB repo is here, else a REMOTE stub),
# and registers PreCompact + SessionEnd in ~/.claude/settings.json. Idempotent.
set -euo pipefail

command -v jq >/dev/null || { echo "need jq (apt install jq)"; exit 1; }
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS="$HOME/.claude/hooks"; mkdir -p "$HOOKS"
HOOK="$HOOKS/flush-session-to-kb.sh"
CFG="$HOME/.claude/kb-flush.env"
SETTINGS="$HOME/.claude/settings.json"

install -m 0755 "$SKILL_DIR/flush-session-to-kb.sh" "$HOOK"
BRIEF="$HOOKS/kb-session-brief.sh"
install -m 0755 "$SKILL_DIR/kb-session-brief.sh" "$BRIEF"
install -m 0755 "$SKILL_DIR/kb-mcp-call.sh" "$HOOKS/kb-mcp-call.sh"

# Discover an existing KB MCP registration (url + bearer) from ~/.claude.json so
# a machine already connected to the KB via `claude mcp` needs zero extra config.
# Prefers a server named 'kb'; else the first http server whose url ends in /mcp
# and carries a bearer. Prints "url<TAB>token" or nothing.
_discover_mcp() {
  local cj="$HOME/.claude.json"
  [ -f "$cj" ] || return 0
  jq -r '
    (.mcpServers // {}) as $m
    | ([$m.kb] + [$m[]])
    | map(select(type == "object"
        and ((.url // "") | endswith("/mcp"))
        and ((.headers.Authorization // "") | test("Bearer"))))
    | (.[0] // empty)
    | [.url, (.headers.Authorization | sub("^Bearer +"; ""))] | @tsv
  ' "$cj" 2>/dev/null
}

if [ ! -f "$CFG" ]; then
  KB_DIR_GUESS="${KB_DIR:-$HOME/data-server}"
  MCP_LINE="$(_discover_mcp || true)"
  if [ -f "$KB_DIR_GUESS/pyproject.toml" ] && command -v uv >/dev/null 2>&1; then
    { echo "# KB session-flush (LOCAL)"; echo "KB_DIR=$KB_DIR_GUESS"; echo "UV=$(command -v uv)"; } >"$CFG"
    MODE="LOCAL enqueue via $KB_DIR_GUESS"
  elif [ -n "$MCP_LINE" ]; then
    MURL=$(printf '%s' "$MCP_LINE" | cut -f1)
    MTOK=$(printf '%s' "$MCP_LINE" | cut -f2)
    { echo "# KB session hooks (MCP) — reuses your 'claude mcp' KB registration"
      echo "KB_MCP_URL=$MURL"; echo "KB_TOKEN=$MTOK"; } >"$CFG"
    MODE="MCP via $MURL (auto-discovered from your claude mcp registration)"
  else
    { echo "# KB session-flush (REMOTE) — set KB_TOKEN and confirm the URL:"
      echo "KB_REST_URL=https://api.example.com"
      echo "KB_TOKEN="; } >"$CFG"
    MODE="REMOTE — edit $CFG (set KB_TOKEN) then re-run"
  fi
  chmod 600 "$CFG"
else
  MODE="kept existing $CFG"
fi

[ -f "$SETTINGS" ] || echo '{}' >"$SETTINGS"
entry=$(jq -n --arg c "$HOOK" '{matcher:"",hooks:[{type:"command",command:$c,timeout:15}]}')
brief_entry=$(jq -n --arg c "$BRIEF" '{matcher:"startup|clear",hooks:[{type:"command",command:$c,timeout:30}]}')
tmp=$(mktemp)
# Append our entries (idempotent: any prior copy removed first) while
# preserving other hooks the user has. The brief hook self-filters
# resume/compact sources, so its matcher stays empty.
jq --argjson e "$entry" --argjson b "$brief_entry" --arg c "$HOOK" --arg bc "$BRIEF" '
  .hooks = (.hooks // {})
  | .hooks.PreCompact = (((.hooks.PreCompact // []) | map(select(.hooks[0].command != $c))) + [$e])
  | .hooks.SessionEnd = (((.hooks.SessionEnd // []) | map(select(.hooks[0].command != $c))) + [$e])
  | .hooks.SessionStart = (((.hooks.SessionStart // []) | map(select(.hooks[0].command != $bc))) + [$b])
' "$SETTINGS" >"$tmp" && mv "$tmp" "$SETTINGS"

echo "hook:     $HOOK"
echo "brief:    $BRIEF (SessionStart -> injects a KB pre-brief into new sessions)"
echo "config:   $CFG  ($MODE)"
echo "settings: $SETTINGS  (PreCompact + SessionEnd + SessionStart registered)"
echo
echo "smoke test (feeds a fake PreCompact event to the hook):"
echo "  T=\$(ls -t \"\$HOME\"/.claude/projects/*/*.jsonl | head -1)"
echo "  printf '{\"transcript_path\":\"%s\",\"cwd\":\"%s\",\"session_id\":\"smoke\"}' \"\$T\" \"\$PWD\" | $HOOK"
echo "  sleep 2 && tail -3 $HOOKS/flush-session.log   # expect: local … | mcp … ok | remote … http=202"

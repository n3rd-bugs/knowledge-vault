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

if [ ! -f "$CFG" ]; then
  KB_DIR_GUESS="${KB_DIR:-$HOME/data-server}"
  if [ -f "$KB_DIR_GUESS/pyproject.toml" ] && command -v uv >/dev/null 2>&1; then
    { echo "# KB session-flush (LOCAL)"; echo "KB_DIR=$KB_DIR_GUESS"; echo "UV=$(command -v uv)"; } >"$CFG"
    MODE="LOCAL enqueue via $KB_DIR_GUESS"
  else
    { echo "# KB session-flush (REMOTE) — set KB_TOKEN and confirm the URL:"
      echo "KB_REST_URL=https://api.n3rd-bugs.dev"
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
echo "  sleep 2 && tail -3 $HOOKS/flush-session.log"

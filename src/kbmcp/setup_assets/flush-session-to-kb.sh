#!/usr/bin/env bash
# Claude Code PreCompact/SessionEnd hook: flush this session's NEW transcript
# delta into the Knowledge Vault before compaction. Detaches immediately so it
# never blocks/stalls compaction. Config lives in ~/.claude/kb-flush.env:
#   LOCAL  (KB host): KB_DIR=/path/to/kb-repo   [UV=/path/to/uv]
#   REMOTE (client) : KB_REST_URL=https://host[:port]   KB_TOKEN=...
# Installed by the `kb-session-flush` skill. See that skill's README for setup.
set -uo pipefail

CFG="$HOME/.claude/kb-flush.env"
[ -f "$CFG" ] || exit 0
# shellcheck disable=SC1090
. "$CFG"
command -v jq >/dev/null 2>&1 || exit 0

INPUT=$(cat)
T=$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty')
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty')
SID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty')
[ -n "$T" ] && [ -f "$T" ] || exit 0

# Project = the working repo's dir name, slugified (falls back to "sessions").
PROJECT=$(basename "${CWD:-sessions}" | tr '[:upper:]' '[:lower:]' \
  | tr -c 'a-z0-9-' '-' | sed 's/-\{2,\}/-/g;s/^-//;s/-$//')
[ -z "$PROJECT" ] && PROJECT=sessions
LOG="$HOME/.claude/hooks/flush-session.log"
export T CWD SID PROJECT LOG

if [ -n "${KB_DIR:-}" ]; then
  # LOCAL: trusted CLI enqueue — checkpointed, GPU-free (nightly drain extracts).
  UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
  export KB_DIR UV
  setsid bash -c '
    cd "$KB_DIR" 2>/dev/null || exit 0
    echo "$(date -Is) local $PROJECT $T"
    "$UV" run kb ingest-session "$T" --project "$PROJECT" --queue
  ' </dev/null >>"$LOG" 2>&1 &
elif [ -n "${KB_REST_URL:-}" ] && [ -n "${KB_TOKEN:-}" ]; then
  # REMOTE: POST the delta over HTTPS. Per-session byte offset in a state file so
  # each compaction submits only the new window (idempotent).
  STATE="$HOME/.claude/hooks/state"; mkdir -p "$STATE"
  OFFF="$STATE/${SID:-default}.offset"
  export KB_REST_URL KB_TOKEN OFFF
  setsid bash -c '
    OFFSET=$(cat "$OFFF" 2>/dev/null || echo 0)
    SIZE=$(stat -c%s "$T" 2>/dev/null || echo 0)
    [ "$SIZE" -le "$OFFSET" ] && exit 0
    DELTA=$(tail -c +$((OFFSET+1)) "$T"); [ -n "$DELTA" ] || exit 0
    BODY=$(jq -n --arg p "$PROJECT" --arg t "$DELTA" "{project:\$p,kind:\"session\",transcript:\$t}")
    CODE=$(printf "%s" "$BODY" | curl -sS -o /dev/null -w "%{http_code}" --max-time 20 \
       -X POST "$KB_REST_URL/api/ingest-session" \
       -H "Authorization: Bearer $KB_TOKEN" -H "Content-Type: application/json" --data @-)
    echo "$(date -Is) remote $PROJECT http=$CODE bytes=$((SIZE-OFFSET))"
    case "$CODE" in 2*) echo "$SIZE" >"$OFFF";; esac
  ' </dev/null >>"$LOG" 2>&1 &
fi
exit 0

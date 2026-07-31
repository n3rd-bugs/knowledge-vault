#!/usr/bin/env bash
# UserPromptSubmit hook: stamp when this turn started, so the Stop hook can tell
# a 5-second answer from a 20-minute agentic run and only interrupt you for the
# latter. Keyed by session_id so parallel sessions never overwrite each other.
set -u
INPUT=$(cat 2>/dev/null || true)
SID=$(printf '%s' "$INPUT" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
[ -n "${SID:-}" ] || SID=$$
D="${TMPDIR:-/tmp}/claude-turns"
mkdir -p "$D" 2>/dev/null || exit 0
date +%s > "$D/$SID" 2>/dev/null || true
# Reap markers from sessions that ended without a Stop (crash, kill) so this
# never becomes an unbounded pile of stale files.
find "$D" -type f -mmin +720 -delete 2>/dev/null || true
exit 0

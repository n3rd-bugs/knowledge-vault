#!/usr/bin/env bash
# CASE: NEVER-SILENT FAILURE (and never a broken session)
# PINS: a hook that cannot reach its transport must (a) exit 0 so Claude Code's
#       compaction/prompt is never blocked, (b) print nothing to stdout, and
#       (c) leave an EXPLANATORY line in ~/.claude/hooks/flush-session.log --
#       the log is the only channel a detached flush has, and the pre-brief hook
#       surfaces recent failures from it (kb-session-brief.sh:106-116).
#       Sources: flush-session-to-kb.sh:12 (`set -uo pipefail`, no -e), :146
#       (chunk FAILED <code> ... will resume), :115 (helper stderr appended to
#       the log), kb-mcp-call.sh:19 ("no mcp session (auth/url?)").
. /rig/lib.sh

new_home fails
WORK="$HOME/work/failrepo"
mkdir -p "$WORK"
T="$WORK/transcript.jsonl"
mk_transcript "$T" 10 fail

step "MCP endpoint unreachable"
cfg_mcp
sed -i "s|KB_MCP_URL=.*|KB_MCP_URL=http://127.0.0.1:9/mcp|" "$CFG"
install_hooks >/dev/null
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-unreach PreCompact)"
assert_eq "0" "$RC" "hook exits 0 (the session is never broken)"
assert_empty_file "$OUT_FILE" "nothing is printed into the session"
if wait_log "FAILED|BUG:|no mcp session" 25; then
  pass "the failure is explained in flush-session.log"
  note "log: $(tail -2 "$LOGF" | tr '\n' '|')"
else
  fail "the failure was SILENT: log=[$(cat "$LOGF" 2>/dev/null)]"
fi
assert_file_contains "$LOGF" "no mcp session" "the helper's diagnosis reaches the log"
assert_file_contains "$LOGF" "will resume" "the log says the delta will be retried"
assert_offset_not_advanced "$HOME/.claude/hooks/state/sess-unreach.offset" "offset not advanced, so nothing is lost"

step "the pre-brief hook SURFACES that failure to the next session"
stub_reset
cfg_mcp
fire "$HOOKS/kb-session-brief.sh" "$(start_event startup "$WORK")"
assert_contains "$OUT" "session-flush reported a recent failure" "next SessionStart warns about the failed flush"

step "REST endpoint unreachable"
new_home fails2
WORK="$HOME/work/failrepo"
mkdir -p "$WORK"
T="$WORK/transcript.jsonl"
mk_transcript "$T" 10 fail
cfg_rest "$RIG_TOKEN" "http://127.0.0.1:9"
install_hooks >/dev/null
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-unreach2 PreCompact)"
assert_eq "0" "$RC" "hook exits 0"
assert_empty_file "$OUT_FILE" "stdout empty"
if wait_log "chunk FAILED" 25; then
  pass "connection failure logged with its curl code"
  note "log: $(tail -1 "$LOGF")"
else
  fail "the REST failure was SILENT: log=[$(cat "$LOGF" 2>/dev/null)]"
fi
assert_offset_not_advanced "$HOME/.claude/hooks/state/sess-unreach2.offset" "offset not advanced"

step "a garbage transcript_path is a no-op, not an error"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "/no/such/transcript.jsonl" "$WORK" sess-x PreCompact)"
assert_eq "0" "$RC" "missing transcript -> exit 0"
assert_empty_file "$OUT_FILE" "missing transcript -> silent"

step "malformed hook input is a no-op, not an error"
fire "$HOOKS/flush-session-to-kb.sh" "not json at all"
assert_eq "0" "$RC" "unparseable stdin -> exit 0"
assert_empty_file "$OUT_FILE" "unparseable stdin -> silent"
fire "$HOOKS/kb-session-brief.sh" "not json at all"
assert_eq "0" "$RC" "brief: unparseable stdin -> exit 0"

step "no config at all: both hooks are inert (the skill is simply not installed)"
new_home fails3
mkdir -p "$HOME/.claude/hooks"
install -m 0755 "$SKILL/flush-session-to-kb.sh" "$HOME/.claude/hooks/"
install -m 0755 "$SKILL/kb-session-brief.sh" "$HOME/.claude/hooks/"
WORK="$HOME/work/failrepo"
mkdir -p "$WORK"
mk_transcript "$WORK/t.jsonl" 5 fail
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WORK/t.jsonl" "$WORK" sess-nocfg PreCompact)"
assert_eq "0" "$RC" "no kb-flush.env -> exit 0"
assert_empty_file "$OUT_FILE" "no kb-flush.env -> silent"
fire "$HOOKS/kb-session-brief.sh" "$(prompt_event "a perfectly substantive question about chunk offsets" "$WORK")"
assert_eq "0" "$RC" "brief with no config -> exit 0"
assert_empty_file "$OUT_FILE" "brief with no config -> silent"

finish

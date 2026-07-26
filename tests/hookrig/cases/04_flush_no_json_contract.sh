#!/usr/bin/env bash
# CASE: the flush hook emits NO JSON response contract
# PINS: the correction to kb-system item 1073, which documented a FABRICATED
#       stdout contract for the flush hook (a JSON object with status/queued
#       fields). The hook prints NOTHING to stdout -- ever. It detaches a child
#       whose only output goes to ~/.claude/hooks/flush-session.log
#       (flush-session-to-kb.sh:53-57, :70, :150, :193 all redirect >>"$LOG").
#       This matters because Claude Code INTERPRETS hook stdout: a PreCompact
#       hook that printed JSON would inject it into the session.
#       Success is a LOG LINE, never a stdout payload.
. /rig/lib.sh

new_home nojson
stub_reset
cfg_mcp
install_hooks >/dev/null

WORK="$HOME/work/hookrig-demo"
mkdir -p "$WORK"
T="$WORK/transcript.jsonl"
mk_transcript "$T" 25 hello

LINES_BEFORE=0
[ -f "$LOGF" ] && LINES_BEFORE=$(wc -l <"$LOGF")

step "feed a PreCompact event"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-nojson PreCompact)"
assert_eq "0" "$RC" "hook exits 0"
assert_empty_file "$OUT_FILE" "stdout is byte-empty"
assert_not_contains "$OUT" '{"status"' 'stdout has no {"status" payload'
assert_not_contains "$OUT" '"queued"' 'stdout has no "queued" payload'
assert_not_contains "$OUT" "{" "stdout contains no JSON object at all"

step "success is reported in the LOG, asynchronously"
if wait_log "flush hookrig-demo ok bytes=" 25; then
  pass "flush-session.log gained the success line"
else
  fail "no success line in flush-session.log: [$(tail -3 "$LOGF" 2>/dev/null)]"
fi
LINES_AFTER=$(wc -l <"$LOGF" 2>/dev/null || echo 0)
assert_ge "$LINES_AFTER" "$((LINES_BEFORE + 1))" "log gained at least one line"
assert_contains "$(tail -1 "$LOGF")" "hookrig-demo" "log line names the project"

step "the submission really happened (the log line is not cosmetic)"
assert_eq "1" "$(stub_count '.tool=="kb_ingest_session"')" "exactly one ingest call reached the stub"

step "SessionEnd behaves the same (no stdout contract on the terminal event)"
stub_reset
printf '%s\n' '{"type":"user","text":"more"}' >>"$T"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-nojson SessionEnd)"
assert_eq "0" "$RC" "SessionEnd firing exits 0"
assert_empty_file "$OUT_FILE" "SessionEnd stdout is byte-empty"
wait_stub 3 20 || true
assert_eq "1" "$(stub_count '.tool=="kb_ingest_session"')" "SessionEnd flushed the appended delta"

finish

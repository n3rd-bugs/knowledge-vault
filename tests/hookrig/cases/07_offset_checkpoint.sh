#!/usr/bin/env bash
# CASE: per-session OFFSET CHECKPOINT (delta submission)
# PINS: a flush submits only the NEW bytes since the last accepted chunk. The
#       byte offset lives in ~/.claude/hooks/state/<session_id>.offset and
#       advances only after an ACCEPTED chunk (flush-session-to-kb.sh:65-66,
#       :74-77, :145). Consequences pinned here:
#         * a second flush of an appended transcript sends the APPENDED bytes only
#         * a flush with nothing new sends NOTHING (not even an MCP handshake)
#         * the offset key is the session_id, so two sessions checkpoint apart
#       Without this the KB would re-ingest the whole transcript on every
#       compaction of a long session.
. /rig/lib.sh

new_home offset
stub_reset
cfg_mcp
install_hooks >/dev/null

WORK="$HOME/work/deltas"
mkdir -p "$WORK"
T="$WORK/transcript.jsonl"
OFFF="$HOME/.claude/hooks/state/sess-delta.offset"

step "first flush submits everything"
mk_transcript "$T" 20 alpha
SIZE1=$(wc -c <"$T")
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-delta PreCompact)"
wait_stub 3 25 || fail "first flush never reached the stub"
assert_eq "1" "$(stub_count '.tool=="kb_ingest_session"')" "one submission"
assert_eq "$SIZE1" "$(stub_field '.tool=="kb_ingest_session"' '.transcript_bytes')" "sent the whole transcript"
assert_eq "$SIZE1" "$(cat "$OFFF" 2>/dev/null)" "checkpoint == bytes accepted"

step "append, flush again: ONLY the new bytes go"
stub_reset
mk_transcript "$HOME/append.jsonl" 7 beta
cat "$HOME/append.jsonl" >>"$T"
SIZE2=$(wc -c <"$T")
DELTA=$((SIZE2 - SIZE1))
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-delta PreCompact)"
wait_stub 3 25 || fail "second flush never reached the stub"
assert_eq "1" "$(stub_count '.tool=="kb_ingest_session"')" "one submission"
assert_eq "$DELTA" "$(stub_field '.tool=="kb_ingest_session"' '.transcript_bytes')" "sent only the appended $DELTA bytes"
stub_requests | jq -j 'select(.tool=="kb_ingest_session") | .transcript' >"$HOME/second"
if cmp -s "$HOME/append.jsonl" "$HOME/second"; then
  pass "the second submission is exactly the appended lines"
else
  fail "the second submission is not the appended lines"
fi
assert_eq "0" "$(grep -c alpha "$HOME/second" || true)" "no already-ingested content was resent"
assert_ge "$(grep -c beta "$HOME/second" || true)" "7" "the appended lines are all present"
assert_eq "$SIZE2" "$(cat "$OFFF")" "checkpoint advanced to the new EOF"

step "nothing new: the hook makes NO transport call at all"
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-delta PreCompact)"
assert_eq "0" "$RC" "hook exits 0"
settle
settle
assert_eq "0" "$(stub_total)" "zero requests -- not even an MCP initialize"
assert_eq "$SIZE2" "$(cat "$OFFF")" "checkpoint unchanged"

step "a DIFFERENT session checkpoints independently"
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-other PreCompact)"
wait_stub 3 25 || fail "the second session never flushed"
assert_eq "$SIZE2" "$(stub_field '.tool=="kb_ingest_session"' '.transcript_bytes')" "a new session_id starts from offset 0"
assert_eq "$SIZE2" "$(cat "$HOME/.claude/hooks/state/sess-other.offset")" "its own checkpoint file"

step "a corrupt offset file restarts from 0 rather than wedging the session"
stub_reset
printf 'garbage' >"$OFFF"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-delta PreCompact)"
wait_stub 3 25 || fail "a corrupt offset wedged the flush"
assert_eq "$SIZE2" "$(stub_field '.tool=="kb_ingest_session"' '.transcript_bytes')" "corrupt offset -> full resend, no crash"

finish

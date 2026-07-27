#!/usr/bin/env bash
# CASE: CONNECTOR chunking — sizing, line alignment, and EXACT reassembly
# PINS: the gap this rig recorded as OPEN. run-connector.sh proved the real relay
#       reaches claude.ai, but on a 57 KB transcript only the first ~24 KB chunk
#       landed inside the wait window and the log was empty, so whether the
#       remaining bytes relay AT ALL was never established. The live lane cannot
#       settle it cheaply: every chunk is a headless `claude -p` that spends real
#       usage and takes tens of seconds.
#
#       CLAUDE_BIN is just a path to an executable (flush-session-to-kb.sh:498),
#       so the whole loop is exercisable with a recorder standing in for the
#       model. That makes the BYTE contract — which this case owns — hermetic,
#       deterministic and free, and leaves the live lane to prove only that the
#       real relay reaches the connector.
#
#       The contract asserted here: the delta is relayed as sequential
#       KB_CONNECTOR_CHUNK-bounded windows cut at LINE boundaries, the offset
#       advances per ACCEPTED chunk, and the concatenation of the chunks is the
#       delta byte-for-byte. Anything less loses transcript content silently —
#       the connector user would never know, because the hook exits 0 either way.
. /rig/lib.sh

new_home connchunk
fake_claude_relay
cfg_connector
install_hooks >/dev/null

WORK="$HOME/work/connrepo"
mkdir -p "$WORK"
T="$WORK/transcript.jsonl"

# Ragged line lengths on purpose: a chunk boundary must never land on a line
# boundary by luck, or line-alignment would be untested.
mk_big_transcript "$T" 60000
SIZE=$(wc -c <"$T")
LINES=$(wc -l <"$T")
note "transcript: $SIZE bytes, $LINES lines (ragged)"
assert_ge "$SIZE" "48001" "the input needs at least 3 chunks at the default 24000"

# Default chunk size, stated explicitly so the case reads as a contract rather
# than as a dependency on whatever the default happens to be.
export KB_CONNECTOR_CHUNK=24000
export KB_CONNECTOR_MAX=240000

step "a PreCompact flush relays the whole transcript"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" conn-1)"
assert_eq "0" "$RC" "the hook exits 0 (never blocks Claude Code)"
wait_relay 3 45 || fail "the relay never received 3 chunks (got $(relay_count))"
wait_log "connector .* ok bytes=" 30 || note "no ok line yet: $(tail -2 "$LOGF" 2>/dev/null | tr '\n' '|')"

N=$(relay_count)
note "relayed in $N chunk(s)"
assert_ge "$N" "3" "a 60 KB delta needs multiple chunks at 24000"

# --------------------------------------------------------------- byte contract
step "every chunk respects the size bound"
i=1
while [ "$i" -le "$N" ]; do
  B=$(relay_bytes "$i")
  note "chunk $i = $B bytes"
  assert_le "$B" "$KB_CONNECTOR_CHUNK" "chunk $i is within KB_CONNECTOR_CHUNK"
  assert_ge "$B" "1" "chunk $i is non-empty"
  i=$((i + 1))
done

step "every chunk ends on a LINE boundary (never a split JSONL record)"
# A chunk cut mid-line would submit half a record and lose the straddled
# message. The LAST chunk may legitimately end wherever the file ends.
i=1
while [ "$i" -lt "$N" ]; do
  if [ "$(relay_chunk "$i" | tail -c1)" = "" ]; then
    pass "chunk $i ends with a newline"
  else
    fail "chunk $i ends mid-line -- a JSONL record was split"
  fi
  i=$((i + 1))
done

step "each chunk is independently parseable as JSONL"
i=1
while [ "$i" -le "$N" ]; do
  BAD=$(relay_chunk "$i" | while IFS= read -r l; do
    [ -n "$l" ] || continue
    printf '%s' "$l" | "$RIG_JQ" -e . >/dev/null 2>&1 || echo x
  done | wc -l | tr -d ' ')
  assert_eq "0" "$BAD" "chunk $i has no unparseable line"
  i=$((i + 1))
done

step "the chunks reassemble into the delta EXACTLY"
relay_concat >"$HOME/reassembled"
assert_eq "$SIZE" "$(wc -c <"$HOME/reassembled")" "reassembled byte count matches the transcript"
if cmp -s "$T" "$HOME/reassembled"; then
  pass "byte-for-byte identical to the original transcript"
else
  fail "reassembly differs from the original -- content was lost or duplicated"
  note "first difference: $(cmp "$T" "$HOME/reassembled" 2>&1 | head -1)"
fi

step "no chunk was relayed twice"
DUPES=$(for i in $(seq 1 "$N"); do relay_chunk "$i" | head -c 120 | md5sum | cut -d' ' -f1; done | sort | uniq -d | wc -l | tr -d ' ')
assert_eq "0" "$DUPES" "every chunk carries distinct content"

# ------------------------------------------------------------------- bookkeeping
step "the offset advanced to EOF and the run is reported as complete"
assert_eq "$SIZE" "$(read_offset)" "offset == transcript size (fully drained)"
assert_file_contains "$LOGF" "connector" "the log names the connector transport"
if grep -qE "connector .* ok bytes=$SIZE " "$LOGF"; then
  pass "logged 'ok bytes=$SIZE' -- a truthful fully-drained summary"
else
  fail "no truthful ok line -- log says: $(grep connector "$LOGF" | tail -2 | tr '\n' '|')"
fi
assert_not_contains "$(cat "$LOGF")" "partial" "a fully-drained firing must not claim 'partial'"

step "the relay was invoked as a headless, tool-restricted call"
ARGV=$(cat "$HOME/relay/argv.1")
assert_contains "$ARGV" "-p" "invoked in headless print mode"
assert_contains "$ARGV" "kb_ingest_session" "the ingest tool is named in the prompt"
assert_contains "$ARGV" "--allowedTools" "the child is restricted to an allowlist"
# The relay must not be able to wander into other tools while holding a
# transcript that may itself carry injected instructions.
assert_not_contains "$ARGV" "--dangerously" "no permission bypass on the relay child"

step "a re-firing with no new bytes relays nothing"
BEFORE=$(relay_count)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" conn-1)"
settle
assert_eq "$BEFORE" "$(relay_count)" "nothing re-sent once the transcript is drained"

finish

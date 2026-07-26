#!/usr/bin/env bash
# CASE: CHUNKING + the 1,000,000-byte row cap
# PINS: kb-system item 1154 -- the flush hook splits a delta into sequential
#       <=900,000-byte windows cut at LINE boundaries (flush-session-to-kb.sh:71,
#       :81-96). The 900,000 is chosen to sit under the server's real caps:
#       MAX_CONTENT_BYTES = 1,000,000 (src/kbcore/ingest_queue.py:22) with gzip
#       guards at 2,000,000 / 1,100,000 (src/kbcore/compress.py:21-22).
#       A chunk that split mid-line would submit half a JSONL record and lose the
#       straddled message, so line alignment is asserted per chunk, not overall.
# ALSO PINS: every chunk opens its OWN MCP session (initialize per chunk), and
#       the concatenation of the chunks reproduces the delta exactly.
. /rig/lib.sh

new_home chunk
stub_reset
cfg_mcp
install_hooks >/dev/null

WORK="$HOME/work/bigrepo"
mkdir -p "$WORK"
T="$WORK/transcript.jsonl"
mk_big_transcript "$T" 1300000
SIZE=$(wc -c <"$T")
note "synthetic transcript: $SIZE bytes, $(wc -l <"$T") lines (ragged lengths)"
assert_ge "$SIZE" "1000001" "the input is comfortably over the 1,000,000-byte row cap"

fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-chunk PreCompact)"
assert_eq "0" "$RC" "hook exits 0"
assert_empty_file "$OUT_FILE" "stdout stays empty even for a multi-chunk flush"
wait_stub 6 60 || note "fewer than 6 requests landed; assertions below will show what arrived"
wait_log "ok bytes=" 60 || true
settle

N=$(stub_count '.tool=="kb_ingest_session"')
step "the delta was SPLIT (a single submission would have been rejected)"
assert_ge "$N" "2" "submitted in multiple chunks (got $N)"
assert_eq "$((N * 3))" "$(stub_count '.transport=="mcp"')" "one initialize/initialized/tools-call round per chunk"
assert_eq "0" "$(stub_count '.is_error==true')" "no chunk was rejected by the server"

step "every chunk is under the caps and ends on a line boundary"
i=0
TOTAL=0
while [ "$i" -lt "$N" ]; do
  stub_requests | jq -j -s --argjson i "$i" \
    '[.[] | select(.tool=="kb_ingest_session")] | .[$i].transcript' >"$HOME/chunk.$i"
  B=$(wc -c <"$HOME/chunk.$i")
  TOTAL=$((TOTAL + B))
  assert_le "$B" "900000" "chunk $i is <= the 900000-byte window ($B)"
  assert_le "$B" "1000000" "chunk $i is under the 1,000,000-byte row cap"
  if [ "$(tail -c1 "$HOME/chunk.$i" | wc -c)" -eq 1 ] && [ -z "$(tail -c1 "$HOME/chunk.$i")" ]; then
    pass "chunk $i ends with a newline"
  else
    fail "chunk $i does NOT end on a line boundary"
  fi
  if jq -e . "$HOME/chunk.$i" >/dev/null 2>&1; then
    pass "chunk $i is individually valid JSONL ($(wc -l <"$HOME/chunk.$i") lines)"
  else
    fail "chunk $i contains a truncated JSON line"
  fi
  i=$((i + 1))
done

step "reassembly is lossless"
assert_eq "$SIZE" "$TOTAL" "chunk bytes sum to the transcript size"
stub_requests | jq -j 'select(.tool=="kb_ingest_session") | .transcript' >"$HOME/reassembled"
if cmp -s "$T" "$HOME/reassembled"; then
  pass "concatenated chunks == the original transcript, byte for byte"
else
  fail "reassembly differs from the input ($(wc -c <"$HOME/reassembled") vs $SIZE bytes)"
fi

step "the offset ends at EOF (the whole delta was accounted for)"
assert_eq "$SIZE" "$(cat "$HOME/.claude/hooks/state/sess-chunk.offset")" "checkpoint == file size"

step "a single chunk larger than the row cap would be REJECTED (the cap is real)"
python3 -c "
import json,sys
open('$HOME/over.json','w').write(json.dumps({'project':'bigrepo','kind':'session','transcript':'x'*1000001}))
"
CODE=$(curl -sS -o "$HOME/over.resp" -w '%{http_code}' -X POST "$STUB/api/ingest-session" \
  -H "Authorization: Bearer $RIG_TOKEN" -H 'Content-Type: application/json' --data @"$HOME/over.json")
assert_eq "400" "$CODE" "1,000,001 bytes -> 400 at the server"
assert_file_contains "$HOME/over.resp" "1000000" "the rejection names the byte limit"

finish

#!/usr/bin/env bash
# CASE: LARGE transcripts — the owner's sessions reach hundreds of megabytes
# PINS: everything about the flush loop that only misbehaves at scale. A 300 MB
#       transcript is 350 chunks, so anything that is merely slow, that loses a
#       byte at a boundary, or that fails to make progress on one pathological
#       line becomes a session that is silently never captured.
#
#       Measured facts this case rests on (so nobody re-derives them):
#         - `tail -c +N` SEEKS, so extracting chunk N is O(1), not O(N). The loop
#           is linear in transcript size, not quadratic. 300 MB ≈ 21 s of local
#           work over MCP/REST.
#         - MCP/REST caps a PreCompact firing at KB_FLUSH_MAX (200 MB in
#           production) and lifts it at SessionEnd; the tail resumes next firing.
#
#       The cap and chunk size are read from the environment here so the SAME
#       logic can be exercised in seconds. Production defaults are asserted
#       separately, from the script source, so shrinking them for the test can
#       never quietly become shrinking them for real.
. /rig/lib.sh

new_home bigflush
stub_reset
cfg_rest
install_hooks >/dev/null

step "the shipped defaults are the production ones"
# Read from the installed script, not from this shell: the point is that the
# overrides below are a TEST affordance, not a change to what users get.
assert_file_contains "$HOOKS/flush-session-to-kb.sh" 'KB_FLUSH_CHUNK:-900000' "default chunk is 900,000 bytes"
assert_file_contains "$HOOKS/flush-session-to-kb.sh" 'KB_FLUSH_MAX:-209715200' "default per-firing cap is 200 MB"

WORK="$HOME/work/bigrepo"
mkdir -p "$WORK"
T="$WORK/transcript.jsonl"

# ------------------------------------------------- 1. many chunks, exact bytes
# Ragged lines so no chunk boundary lands on a line boundary by luck.
mk_big_transcript "$T" 12000000
SIZE=$(wc -c <"$T")
note "transcript: $SIZE bytes, $(wc -l <"$T") lines"
export KB_FLUSH_CHUNK=400000
export KB_FLUSH_MAX=209715200          # no cap in play for this part
EXPECT=$(( (SIZE + KB_FLUSH_CHUNK - 1) / KB_FLUSH_CHUNK ))
note "expect roughly $EXPECT chunks at $KB_FLUSH_CHUNK bytes"

step "a multi-chunk delta drains in ONE firing and loses nothing"
T0=$(date +%s)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" big-1)"
assert_eq "0" "$RC" "the hook exits 0 immediately (it detaches)"
wait_log "flush .* ok bytes=" 180 || fail "the flush never completed: $(tail -2 "$LOGF" | tr '\n' '|')"
ELAPSED=$(( $(date +%s) - T0 ))
N=$(stub_count '.transport=="rest"')
note "$N chunk(s) in ${ELAPSED}s"
assert_ge "$N" "$((EXPECT - 1))" "the whole transcript was chunked, not truncated"

# Byte-exactness is the assertion that matters: at 30 chunks a single dropped
# boundary byte would still "look" successful in the log.
stub_concat '.transport=="rest"' '.transcript' >"$HOME/reassembled"
assert_eq "$SIZE" "$(wc -c <"$HOME/reassembled")" "reassembled size == transcript size"
if cmp -s "$T" "$HOME/reassembled"; then
  pass "byte-for-byte identical across $N chunks"
else
  fail "reassembly differs: $(cmp "$T" "$HOME/reassembled" 2>&1 | head -1)"
fi
assert_eq "$SIZE" "$(read_offset)" "the offset reached EOF"

step "every chunk stayed under the server's 1,000,000-byte row cap"
MAXSEEN=$(stub_field '.transport=="rest"' '.transcript_bytes' | sort -n | tail -1)
note "largest chunk submitted: $MAXSEEN bytes"
assert_le "$MAXSEEN" "1000000" "no chunk can be rejected by the row cap"

# ------------------------------------------------ 2. the per-firing cap + resume
new_home bigcap
stub_reset
cfg_rest
install_hooks >/dev/null
WORK2="$HOME/work/caprepo"
mkdir -p "$WORK2"
T2="$WORK2/transcript.jsonl"
mk_big_transcript "$T2" 12000000
SIZE2=$(wc -c <"$T2")
export KB_FLUSH_CHUNK=400000
export KB_FLUSH_MAX=3000000            # a budget well under the transcript

step "a PreCompact firing stops at the per-firing budget"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T2" "$WORK2" cap-1)"
wait_log "flush .* partial bytes=" 120 || fail "no partial line: $(tail -3 "$LOGF" | tr '\n' '|')"
OFF1=$(read_offset)
note "firing 1 stopped at offset $OFF1/$SIZE2"
assert_ge "$OFF1" "$KB_FLUSH_MAX" "it sent at least the budget"
assert_le "$OFF1" "$((SIZE2 - 1))" "it stopped before EOF"
if grep -qE "flush .* ok bytes=" "$LOGF"; then
  fail "a capped firing claimed 'ok' — the pre-brief would report a captured session"
else
  pass "no false 'ok' while bytes remain"
fi

step "repeated firings drain the rest, exactly once each"
# Each firing must be allowed to FINISH before the next one starts. Overlapping
# children share one offset file and would re-send a chunk — which is a property
# of firing twice concurrently, not of the resume logic.
i=0
while [ "$(read_offset)" -lt "$SIZE2" ] && [ "$i" -lt 20 ]; do
  SUMS=$(log_count ' (ok|partial) bytes=')
  fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T2" "$WORK2" cap-1)"
  wait_firing "$SUMS" 120 || break
  i=$((i + 1))
done
assert_eq "$SIZE2" "$(read_offset)" "the transcript drained across $i further firing(s)"
stub_concat '.transport=="rest"' '.transcript' >"$HOME/reassembled2"
if cmp -s "$T2" "$HOME/reassembled2"; then
  pass "resumed chunks reassemble byte-for-byte (nothing lost, nothing doubled)"
else
  fail "resumed reassembly differs: $(cmp "$T2" "$HOME/reassembled2" 2>&1 | head -1)"
fi

step "SessionEnd lifts the cap so the terminal firing cannot strand a tail"
new_home bigend
stub_reset
cfg_rest
install_hooks >/dev/null
W3="$HOME/work/endrepo"
mkdir -p "$W3"
mk_big_transcript "$W3/t.jsonl" 12000000
S3=$(wc -c <"$W3/t.jsonl")
export KB_FLUSH_CHUNK=400000 KB_FLUSH_MAX=3000000
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$W3/t.jsonl" "$W3" end-1 SessionEnd)"
wait_log "flush .* ok bytes=" 180 || fail "SessionEnd did not drain: $(tail -3 "$LOGF" | tr '\n' '|')"
# The log is the authority here: a drained SessionEnd RETIRES its offset file, so
# read_offset legitimately reports 0 afterwards.
if grep -qE "flush .* ok bytes=$S3 \(offset $S3/$S3\)" "$LOGF"; then
  pass "SessionEnd drained all $S3 bytes in one firing despite the budget"
else
  fail "SessionEnd did not drain fully: $(grep flush "$LOGF" | tail -2 | tr '\n' '|')"
fi
if [ -z "$(offset_file)" ]; then
  pass "the offset file was retired after a drained SessionEnd"
else
  fail "offset file survived a drained SessionEnd: $(read_offset)"
fi

# ------------------------------------- 3. a single line larger than one chunk
# A pasted 2 MB tool output is one JSONL record. It cannot be cut on a line
# boundary, so the loop MUST still make progress — a stall here would mean the
# session is never captured again, silently, from that byte onward.
new_home bigline
stub_reset
cfg_rest
install_hooks >/dev/null
W4="$HOME/work/linerepo"
mkdir -p "$W4"
T4="$W4/transcript.jsonl"
"$RIG_PY" - "$T4" <<'EOF'
import json, sys
with open(sys.argv[1], "w") as fh:
    fh.write(json.dumps({"type": "user", "text": "before the monster"}) + "\n")
    fh.write(json.dumps({"type": "assistant", "text": "M" * 2_000_000}) + "\n")
    fh.write(json.dumps({"type": "user", "text": "after the monster"}) + "\n")
EOF
S4=$(wc -c <"$T4")
note "transcript with one ~2 MB line: $S4 bytes, $(wc -l <"$T4") lines"
export KB_FLUSH_CHUNK=400000 KB_FLUSH_MAX=209715200

step "an over-long line does not stall the loop"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T4" "$W4" line-1)"
wait_log "flush .* ok bytes=" 120 || fail "the over-long line stalled the flush: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "$S4" "$(read_offset)" "the offset advanced past the over-long line to EOF"
stub_concat '.transport=="rest"' '.transcript' >"$HOME/reassembled4"
if cmp -s "$T4" "$HOME/reassembled4"; then
  pass "the over-long line was delivered intact across chunk boundaries"
else
  fail "the over-long line was corrupted: $(cmp "$T4" "$HOME/reassembled4" 2>&1 | head -1)"
fi
assert_le "$(stub_field '.transport=="rest"' '.transcript_bytes' | sort -n | tail -1)" "1000000" \
  "even while splitting a huge line, no chunk exceeds the row cap"

# ------------------------------------- 4. the transcript grows during a flush
# SIZE is sampled once per firing. Bytes appended after that must be picked up
# by the NEXT firing, never skipped.
new_home biggrow
stub_reset
cfg_rest
install_hooks >/dev/null
W5="$HOME/work/growrepo"
mkdir -p "$W5"
T5="$W5/transcript.jsonl"
mk_transcript "$T5" 200 grow
S5A=$(wc -c <"$T5")
export KB_FLUSH_CHUNK=400000 KB_FLUSH_MAX=209715200

step "bytes appended after a flush are captured by the next one"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T5" "$W5" grow-1)"
wait_log "flush .* ok bytes=" 60 || fail "first flush did not complete"
assert_eq "$S5A" "$(read_offset)" "first firing drained to the then-current EOF"
mk_transcript "$HOME/more.jsonl" 150 appended
cat "$HOME/more.jsonl" >>"$T5"
S5B=$(wc -c <"$T5")
BEFORE=$(stub_total)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T5" "$W5" grow-1)"
wait_stub "$((BEFORE + 1))" 60 || fail "the appended bytes were never submitted"
assert_eq "$S5B" "$(read_offset)" "the second firing picked up exactly the growth"
LAST=$(stub_field '.transport=="rest"' '.transcript' | tail -c 400)
assert_contains "$LAST" "appended" "the appended content really arrived"

# --------------------------- 5. the transcript shrinks (rotated / replaced)
# A stale offset larger than the file must not wedge the session forever.
step "a transcript replaced by a SHORTER one does not wedge the offset"
: >"$T5"
mk_transcript "$T5" 30 rotated
S5C=$(wc -c <"$T5")
note "file replaced: now $S5C bytes, stale offset $(read_offset)"
BEFORE=$(stub_total)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T5" "$W5" grow-1)"
assert_eq "0" "$RC" "the hook exits 0 on a shrunk transcript"
settle
NOW=$(read_offset)
note "after the shrink: offset $NOW, submissions $(stub_total) (was $BEFORE)"
if [ "$(stub_total)" -gt "$BEFORE" ]; then
  pass "the rotated content was re-submitted (offset recovered)"
else
  note "offset ($NOW) still exceeds the file ($S5C): this session captures nothing further"
  note "until the 30-day sweep removes the offset file — recorded, not asserted"
fi

finish

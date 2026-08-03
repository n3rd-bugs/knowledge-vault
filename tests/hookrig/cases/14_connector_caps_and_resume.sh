#!/usr/bin/env bash
# CASE: CONNECTOR per-firing cap, failure handling, resume, and SessionEnd drain
# PINS: the half of connector chunking that decides whether transcript content
#       is EVENTUALLY captured or quietly abandoned. Case 13 proves a drainable
#       delta is relayed exactly; this one covers every way a firing can STOP
#       EARLY, and asserts that stopping early always leaves the session
#       resumable rather than truncated.
#
#       Four stop conditions exist (flush-session-to-kb.sh:478-527):
#         1. KB_CONNECTOR_MAX  — the per-firing budget, because each chunk costs
#            real Claude usage. PreCompact honours it; SessionEnd must NOT (it is
#            the terminal firing, so anything held is lost forever).
#         2. a chunk the model did not accept — must break WITHOUT advancing the
#            offset, or the unsent bytes are skipped permanently.
#         3. a still-being-written partial line — held rather than shipped as
#            half a JSONL record, EXCEPT at SessionEnd/EOF where there is no
#            next firing to hold it for.
#         4. nothing left to send.
#       In every early-stop case the log must NOT claim success: the pre-brief
#       lifts these lines into the next session, so a false "ok" is how a user
#       comes to believe a session was captured when it was not.
. /rig/lib.sh

# ============================================================ 1. per-firing cap
new_home conncap
fake_claude_relay
cfg_connector
install_hooks >/dev/null

WORK="$HOME/work/caprepo"
mkdir -p "$WORK"
T="$WORK/transcript.jsonl"
mk_big_transcript "$T" 60000
SIZE=$(wc -c <"$T")
note "transcript: $SIZE bytes"

# A budget deliberately smaller than the transcript, so the cap must bite.
export KB_CONNECTOR_CHUNK=10000
export KB_CONNECTOR_MAX=25000

step "PreCompact stops at the per-firing budget"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" cap-1)"
assert_eq "0" "$RC" "the hook exits 0"
wait_relay 3 45 || fail "expected at least 3 chunks, got $(relay_count)"
settle
N1=$(relay_count)
OFF1=$(read_offset)
note "firing 1: $N1 chunk(s), offset $OFF1/$SIZE"
assert_ge "$OFF1" "1" "some bytes were accepted"
assert_le "$OFF1" "$((SIZE - 1))" "the firing STOPPED before EOF (the cap bit)"

step "an early stop is reported honestly, never as success"
if grep -qE "connector .* partial bytes=" "$LOGF"; then
  pass "logged 'partial' for a firing that did not drain"
else
  fail "no partial line -- log: $(grep connector "$LOGF" | tail -3 | tr '\n' '|')"
fi
if grep -q "per-firing cap reached" "$LOGF"; then
  pass "the cap itself is named in the log"
else
  fail "the cap was hit but never explained in the log"
fi
if grep -qE "connector .* ok bytes=" "$LOGF"; then
  fail "a capped firing claimed 'ok' -- the pre-brief would report a captured session"
else
  pass "no false 'ok' for a capped firing"
fi

step "the NEXT firing resumes from the offset instead of re-sending"
FIRST_CHUNK_HEAD=$(relay_chunk 1 | head -c 200)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" cap-1)"
wait_relay "$((N1 + 1))" 45 || fail "the second firing relayed nothing"
settle
OFF2=$(read_offset)
note "firing 2: $(relay_count) chunk(s) total, offset $OFF2/$SIZE"
assert_ge "$OFF2" "$((OFF1 + 1))" "the offset advanced further"
NEXT_HEAD=$(relay_chunk "$((N1 + 1))" | head -c 200)
assert_ne "$FIRST_CHUNK_HEAD" "$NEXT_HEAD" "the resumed chunk is NOT a re-send of chunk 1"

step "keep firing until drained: everything arrives exactly once"
i=0
while [ "$(read_offset)" -lt "$SIZE" ] && [ "$i" -lt 12 ]; do
  BEFORE=$(relay_count)
  fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" cap-1)"
  wait_relay "$((BEFORE + 1))" 45 || break
  settle
  i=$((i + 1))
done
assert_eq "$SIZE" "$(read_offset)" "repeated firings eventually drain the transcript"
relay_concat >"$HOME/reassembled"
if cmp -s "$T" "$HOME/reassembled"; then
  pass "the resumed chunks reassemble byte-for-byte (nothing lost, nothing doubled)"
else
  fail "resumed reassembly differs: $(cmp "$T" "$HOME/reassembled" 2>&1 | head -1)"
fi

step "every firing of the SAME session relays the SAME identity"
# A resumed session spans several firings, each a fresh detached child that
# recomputes the identity from the event. If any of them dropped it (or derived a
# different one) the chunks would land in the queue as unrelated rows and be
# extracted separately — the fragmentation the identity exists to prevent, just
# spread across firings instead of chunks.
NALL=$(relay_count)
i=1
MISSING=0
while [ "$i" -le "$NALL" ]; do
  case "$(cat "$HOME/relay/argv.$i" 2>/dev/null)" in
    *"session:cap-1"*) : ;;
    *) MISSING=$((MISSING + 1)); note "chunk $i carries no session identity" ;;
  esac
  i=$((i + 1))
done
assert_eq "0" "$MISSING" "all $NALL chunks across every firing carry session:cap-1"

# ================================================= 2. SessionEnd ignores the cap
new_home connend
fake_claude_relay
cfg_connector
install_hooks >/dev/null
WORK2="$HOME/work/endrepo"
mkdir -p "$WORK2"
T2="$WORK2/transcript.jsonl"
mk_big_transcript "$T2" 60000
SIZE2=$(wc -c <"$T2")
export KB_CONNECTOR_CHUNK=10000
export KB_CONNECTOR_MAX=25000

step "SessionEnd drains EVERYTHING despite the same small budget"
# The terminal firing has no successor, so honouring the budget here would
# discard the tail of the session permanently.
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T2" "$WORK2" end-1 SessionEnd)"
assert_eq "0" "$RC" "the hook exits 0"
wait_log "connector .* ok bytes=" 60 || note "log: $(tail -3 "$LOGF" 2>/dev/null | tr '\n' '|')"
settle
note "SessionEnd relayed $(relay_count) chunk(s) for $SIZE2 bytes"
relay_concat >"$HOME/reassembled2"
assert_eq "$SIZE2" "$(wc -c <"$HOME/reassembled2")" "the whole transcript was relayed in ONE firing"
if cmp -s "$T2" "$HOME/reassembled2"; then
  pass "SessionEnd reassembly is byte-for-byte"
else
  fail "SessionEnd lost or duplicated content"
fi
if grep -q "per-firing cap reached" "$LOGF"; then
  fail "SessionEnd honoured the per-firing cap -- the session tail would be lost"
else
  pass "SessionEnd ignored the cap, as the terminal firing must"
fi

step "the terminal firing carries the identity, and no empty git keys"
# endrepo is a plain directory, so repo/branch/commit have nothing to say: they
# must be ABSENT from the tool call, not present and blank.
AEND=$(cat "$HOME/relay/argv.1")
assert_contains "$AEND" "session:end-1" "SessionEnd stamps the session identity too"
assert_not_contains "$AEND" '"session:"' "never a degenerate identity, even on the last firing"
assert_not_contains "$AEND" '""' "no key is sent with an empty value"

step "a fully-drained SessionEnd retires the offset file"
# Leaving it behind would make a future session with a recycled id resume from
# a stale offset and skip its opening bytes.
if [ -z "$(offset_file)" ]; then
  pass "the offset file was removed after a drained SessionEnd"
else
  fail "offset file survived a drained SessionEnd: $(offset_file) -> $(read_offset)"
fi

# ============================================== 3. a chunk the model refuses
new_home connfail
fake_claude_relay
cfg_connector
install_hooks >/dev/null
WORK3="$HOME/work/failrepo"
mkdir -p "$WORK3"
T3="$WORK3/transcript.jsonl"
mk_big_transcript "$T3" 60000
SIZE3=$(wc -c <"$T3")
export KB_CONNECTOR_CHUNK=10000
export KB_CONNECTOR_MAX=240000

step "the 2nd chunk is refused: accepted bytes stick, the rest is retried"
export KB_RIG_FAIL_AT=2
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T3" "$WORK3" fail-1)"
assert_eq "0" "$RC" "the hook still exits 0"
wait_log "chunk FAILED" 45 || fail "a refused chunk was not logged"
settle
OFFF1=$(read_offset)
B1=$(relay_bytes 1)
note "after the refusal: offset $OFFF1/$SIZE3 (chunk 1 was $B1 bytes)"
assert_eq "$B1" "$OFFF1" "the offset advanced by EXACTLY the accepted chunk, not the refused one"
assert_eq "2" "$(relay_count)" "the loop broke at the refusal instead of ploughing on"
if grep -qE "connector .* ok bytes=" "$LOGF"; then
  fail "a failed firing claimed 'ok'"
else
  pass "no false 'ok' after a refusal"
fi

step "with the transport healthy again, the refused bytes are re-sent"
unset KB_RIG_FAIL_AT
BEFORE=$(relay_count)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T3" "$WORK3" fail-1)"
wait_relay "$((BEFORE + 1))" 45 || fail "no retry after the transport recovered"
settle
# The chunk that was refused must be the FIRST thing retried, byte-identical.
REFUSED=$(relay_chunk 2 | md5sum | cut -d' ' -f1)
RETRIED=$(relay_chunk 3 | md5sum | cut -d' ' -f1)
assert_eq "$REFUSED" "$RETRIED" "the refused chunk was retried verbatim (no bytes skipped)"

step "each classification branch is treated as a failure"
# The hook matches on FAIL/Error/NOTOOL/unavailable, and on 'no id'. A reply the
# hook cannot read as a queue id must never advance the offset.
for mode in "Error: tool unavailable" "NOTOOL" "sorry, I cannot do that"; do
  new_home "connclass$(printf '%s' "$mode" | tr -c 'a-z' x | head -c 6)"
  fake_claude_relay
  cfg_connector
  install_hooks >/dev/null
  W="$HOME/work/r"
  mkdir -p "$W"
  mk_transcript "$W/t.jsonl" 20 cls
  export KB_RIG_FAIL_AT=1 KB_RIG_FAIL_MODE="$mode"
  fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$W/t.jsonl" "$W" cls-1)"
  wait_log "chunk FAILED" 30 >/dev/null 2>&1
  settle
  OFFC=$(read_offset)
  if [ "${OFFC:-0}" = "0" ]; then
    pass "reply '$mode' did not advance the offset"
  else
    fail "reply '$mode' was accepted as success (offset $OFFC)"
  fi
  unset KB_RIG_FAIL_AT KB_RIG_FAIL_MODE
done

# ====================================== 4. a still-being-written partial line
new_home connpart
fake_claude_relay
cfg_connector
install_hooks >/dev/null
WORK4="$HOME/work/partrepo"
mkdir -p "$WORK4"
T4="$WORK4/transcript.jsonl"
export KB_CONNECTOR_CHUNK=24000
export KB_CONNECTOR_MAX=240000

step "a lone unterminated line is HELD on PreCompact, not shipped as half a record"
printf '{"type":"user","text":"a line still being written' >"$T4"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T4" "$WORK4" part-1)"
assert_eq "0" "$RC" "the hook exits 0"
settle
assert_eq "0" "$(relay_count)" "nothing was relayed -- the partial line was held"
assert_eq "0" "$(read_offset)" "the offset did not advance past an unterminated line"

step "once the line is completed it relays normally"
printf '"}\n' >>"$T4"
SIZE4=$(wc -c <"$T4")
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T4" "$WORK4" part-1)"
wait_relay 1 30 || fail "the completed line was never relayed"
settle
assert_eq "$SIZE4" "$(read_offset)" "the completed line drained fully"
if printf '%s' "$(relay_chunk 1)" | "$RIG_JQ" -e . >/dev/null 2>&1; then
  pass "the relayed line is valid JSON (never a split record)"
else
  fail "the relayed content is not parseable JSON"
fi

step "at SessionEnd there is no next firing, so a final partial line IS sent"
new_home connpart2
fake_claude_relay
cfg_connector
install_hooks >/dev/null
W5="$HOME/work/p2"
mkdir -p "$W5"
printf '{"type":"user","text":"truncated at the very end' >"$W5/t.jsonl"
S5=$(wc -c <"$W5/t.jsonl")
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$W5/t.jsonl" "$W5" p2-1 SessionEnd)"
wait_relay 1 30 || fail "SessionEnd dropped the final partial line -- content lost forever"
settle
assert_eq "$S5" "$(relay_bytes 1)" "the final partial line was sent whole at SessionEnd"

finish

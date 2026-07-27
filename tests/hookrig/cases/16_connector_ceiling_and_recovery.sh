#!/usr/bin/env bash
# CASE: CONNECTOR at 100s-of-MB scale, and the state bugs that surface there
# PINS: the fixes for an audit of the flush loop against the owner's real
#       transcript sizes. CONNECTOR relays ONE headless `claude -p` per 24 KB, so
#       it is ~2600x more expensive per byte than a bearer transport, and four
#       things followed from that which only bite on a large session:
#
#   1. SessionEnd used to lift the cap to 1 TB so a tail could never be stranded.
#      Correct for a normal session; catastrophic for a 300 MB one — ~13,000
#      model calls, days of detached background work silently spending the
#      user's Claude usage, with NO log line until the loop ends (every summary
#      is written after it). There is now an absolute ceiling, and the bytes it
#      cannot carry are dropped LOUDLY, naming the transcript so they can be
#      re-ingested by hand.
#   2. The drop keeps the most RECENT bytes. A head-first ceiling would relay the
#      session's opening forever on a transcript growing faster than the budget,
#      which is exactly backwards: recent context is the valuable part.
#   3. "duplicate — already queued" carries no digits, so the classifier read it
#      as failure and broke WITHOUT advancing. Any replay of an accepted chunk —
#      after a crash, a zero-length offset file, or an overlapping firing —
#      re-sent the same bytes forever: a permanent, self-perpetuating stall.
#      A duplicate means the bytes ARE queued, which is what advancing records.
#   4. Nothing stopped two firings draining one session at once. They shared an
#      offset file, relayed the same chunk, and a slow child's late write could
#      REWIND the offset over a faster one.
. /rig/lib.sh

# ==================================== 1. the ceiling, and what it drops
new_home connceiling
fake_claude_relay
cfg_connector
install_hooks >/dev/null

step "the shipped default ceiling is the production one"
assert_file_contains "$HOOKS/flush-session-to-kb.sh" 'KB_CONNECTOR_SESSION_MAX:-2000000' \
  "default absolute ceiling is 2,000,000 bytes"

WORK="$HOME/work/hugerepo"
mkdir -p "$WORK"
T="$WORK/transcript.jsonl"
mk_big_transcript "$T" 900000
SIZE=$(wc -c <"$T")
note "transcript: $SIZE bytes"

# A ceiling far below the transcript, so the gate must engage. Chunk stays small
# so a drained remainder is still several relays.
export KB_CONNECTOR_CHUNK=20000
export KB_CONNECTOR_MAX=240000
export KB_CONNECTOR_SESSION_MAX=200000

step "a SessionEnd firing no longer tries to relay the whole backlog"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" huge-1 SessionEnd)"
assert_eq "0" "$RC" "the hook exits 0"
wait_log "NOT CAPTURED" 60 || fail "the ceiling never announced itself: $(tail -3 "$LOGF" | tr '\n' '|')"
settle
RELAYED=$(relay_count)
note "relayed $RELAYED chunk(s); log: $(grep -c . "$LOGF") line(s)"
# Without the ceiling this would be ceil(900000/20000) = 45 relays.
assert_le "$RELAYED" "12" "the firing relayed only what the ceiling allows, not the whole backlog"
assert_ge "$RELAYED" "1" "it still captured something"

step "the drop is LOUD, quantified, and tells the user how to recover"
LINE=$(grep -m1 "NOT CAPTURED" "$LOGF")
note "$(printf '%s' "$LINE" | head -c 240)"
assert_contains "$LINE" "NOT CAPTURED" "the line names the outcome"
assert_contains "$LINE" "$T" "it names the transcript path, so the bytes can be re-ingested"
assert_contains "$LINE" "GONE" "it is explicit that the bytes are not merely deferred"
assert_contains "$LINE" "KB_MCP_URL" "it names the transport that WOULD capture the session"

step "the bytes kept are the most RECENT ones, not the oldest"
# The final line of the transcript is the newest content; it must be inside what
# was relayed. The first line must NOT be.
LASTLINE=$(tail -1 "$T" | head -c 60)
FIRSTLINE=$(head -1 "$T" | head -c 60)
ALL=$(relay_concat)
assert_contains "$ALL" "$LASTLINE" "the newest content was relayed"
assert_not_contains "$ALL" "$FIRSTLINE" "the oldest content was the part dropped"

step "the skip realigned forward to a line boundary"
# Parse the first complete LINE, not a fixed byte prefix: a prefix of a long
# line is invalid JSON no matter where the skip landed, which would make this
# assertion fail for a correct implementation.
FIRSTKEPT=$(printf '%s' "$ALL" | head -1)
if printf '%s' "$FIRSTKEPT" | "$RIG_JQ" -e . >/dev/null 2>&1; then
  pass "the first kept line is complete, valid JSON (skip realigned)"
else
  fail "the skip landed mid-line: $(printf '%s' "$FIRSTKEPT" | head -c 120)"
fi

step "a normal-sized session is untouched by the ceiling"
new_home connnormal
fake_claude_relay
cfg_connector
install_hooks >/dev/null
W2="$HOME/work/small"
mkdir -p "$W2"
mk_transcript "$W2/t.jsonl" 40 small
S2=$(wc -c <"$W2/t.jsonl")
export KB_CONNECTOR_CHUNK=20000 KB_CONNECTOR_MAX=240000 KB_CONNECTOR_SESSION_MAX=2000000
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$W2/t.jsonl" "$W2" small-1 SessionEnd)"
wait_relay 1 45 || fail "a small session was not relayed"
settle
assert_not_contains "$(cat "$LOGF")" "NOT CAPTURED" "no ceiling line for a session under the limit"
relay_concat >"$HOME/small.out"
if cmp -s "$W2/t.jsonl" "$HOME/small.out"; then
  pass "a small session still relays byte-for-byte"
else
  fail "the ceiling changed behaviour for a normal session"
fi

# ==================================== 2. duplicate is acceptance, not failure
new_home conndupe
fake_claude_relay
cfg_connector
install_hooks >/dev/null
W3="$HOME/work/duperepo"
mkdir -p "$W3"
mk_transcript "$W3/t.jsonl" 60 dupe
S3=$(wc -c <"$W3/t.jsonl")
export KB_CONNECTOR_CHUNK=20000 KB_CONNECTOR_MAX=240000 KB_CONNECTOR_SESSION_MAX=2000000

step "a chunk the server has already queued ADVANCES the offset"
# The server dedupes on content hash and answers "duplicate — already queued",
# which contains no digits. Treating that as failure meant a replayed chunk
# stalled the session forever.
export KB_RIG_FAIL_AT=1 KB_RIG_FAIL_MODE="duplicate — already queued"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$W3/t.jsonl" "$W3" dupe-1)"
assert_eq "0" "$RC" "the hook exits 0"
wait_relay 1 45 || fail "nothing was relayed"
settle
unset KB_RIG_FAIL_AT KB_RIG_FAIL_MODE
OFFD=$(read_offset)
note "offset after a duplicate reply: $OFFD/$S3"
assert_ge "$OFFD" "1" "the offset advanced past an already-queued chunk"
if grep -q "chunk FAILED" "$LOGF"; then
  fail "a duplicate was logged as a failure — the session would stall forever"
else
  pass "a duplicate is not recorded as a failure"
fi

step "and the session goes on to complete rather than stalling"
assert_eq "$S3" "$OFFD" "the whole transcript drained despite the duplicate reply"

# ==================================== 3. a shrunk transcript recovers
new_home connshrink
fake_claude_relay
cfg_connector
install_hooks >/dev/null
W4="$HOME/work/shrinkrepo"
mkdir -p "$W4"
mk_transcript "$W4/t.jsonl" 200 original
export KB_CONNECTOR_CHUNK=20000 KB_CONNECTOR_MAX=240000 KB_CONNECTOR_SESSION_MAX=2000000
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$W4/t.jsonl" "$W4" shrink-1)"
wait_relay 1 45 || fail "the first flush relayed nothing"
settle
BIGOFF=$(read_offset)

step "a transcript replaced by a SHORTER one restarts instead of going silent"
# Without recovery, SIZE <= OFFSET exits 0 forever: the session is never captured
# again, with no log line, until the 30-day sweep removes the offset file.
mk_transcript "$W4/t.jsonl" 20 replaced
S4=$(wc -c <"$W4/t.jsonl")
note "replaced: $S4 bytes, stale offset $BIGOFF"
BEFORE=$(relay_count)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$W4/t.jsonl" "$W4" shrink-1)"
assert_eq "0" "$RC" "the hook exits 0"
wait_relay "$((BEFORE + 1))" 45 || fail "the shrunk transcript was never re-sent — capture is silently dead"
settle
assert_file_contains "$LOGF" "transcript shrank" "the restart is announced, not silent"
assert_eq "$S4" "$(read_offset)" "the offset was reset and the new content drained"

# ==================================== 4. overlapping firings cannot race
new_home connlock
fake_claude_relay
cfg_connector
install_hooks >/dev/null
W5="$HOME/work/lockrepo"
mkdir -p "$W5"
mk_big_transcript "$W5/t.jsonl" 400000
S5=$(wc -c <"$W5/t.jsonl")
export KB_CONNECTOR_CHUNK=20000 KB_CONNECTOR_MAX=2000000 KB_CONNECTOR_SESSION_MAX=2000000

step "two firings on one session do not both relay the same chunks"
# Fire twice back to back so the second lands while the first is still draining.
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$W5/t.jsonl" "$W5" lock-1)"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$W5/t.jsonl" "$W5" lock-1)"
wait_log "connector .* ok bytes=" 90 || note "log: $(tail -3 "$LOGF" | tr '\n' '|')"
settle; settle
N=$(relay_count)
EXPECT=$(( (S5 + 20000 - 1) / 20000 ))
note "relayed $N chunk(s); a single clean drain needs about $EXPECT"
relay_concat >"$HOME/lock.out"
assert_eq "$S5" "$(wc -c <"$HOME/lock.out")" "exactly one copy of the transcript was relayed"
if cmp -s "$W5/t.jsonl" "$HOME/lock.out"; then
  pass "overlapping firings produced no duplicate or reordered bytes"
else
  fail "concurrent firings corrupted the stream: $(cmp "$W5/t.jsonl" "$HOME/lock.out" 2>&1 | head -1)"
fi
assert_eq "$S5" "$(read_offset)" "the offset ended at EOF, never rewound"

finish

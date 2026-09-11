#!/usr/bin/env bash
# CASE: TRANSPORT LINE + RETRY MARKER (C6, proposal_v2 Appendix D + G, KB 33784;
#       KB 1195 for the missing connect-timeout) — one REST curl line with
#       curl-level retries, ONE script-level retry on the transient set, a
#       <okey>.retry marker the Stop gate honours, and a first-window cap that
#       re-cuts a lost 202 byte-identically.
# PINS (flush-session-to-kb.sh kb_rest_post + the drain child + kb_stop_gate):
#   * `stub_fail 1 502` lands on curl's own --retry: exactly 1 extra hit, no
#     FAILED line, no marker, offset at EOF.
#   * `stub_fail 9 502` (KB_FLUSH_CURL_RETRY=1 for a tight bound): ONE FAILED
#     line `chunk FAILED (502) at offset 0 — will resume retry#1 ev= sid=`, at
#     most 4 hits (2 attempts x 2 chains), `.retry` = `<epoch> 1 502 <end>`
#     where <end> is the FAILED CHUNK'S END (offset + bytes; == the size here,
#     one newline-terminated window); a second failed drain counts n=2; a
#     clean drain that reaches that end removes the marker.
#   * a dropped connection (`stub_fail 9 0`, curl 000) is on the transient set:
#     FAILED (000) + marker.
#   * 401 is configuration: ONE hit (no curl retry, no script retry), no marker.
#   * 429: marker with code 429 and the LONG back-off (KB_FLUSH_RETRY_MAX 3600,
#     not the exponential 300 s for n=1) — the next Stop (4 MB pending, .last
#     old) makes zero requests, and so does one with the marker 1000 s old
#     (open under the exponential schedule, closed under the long bound); once
#     the marker is 4000 s old the Stop drains and the marker goes.
#   * lost 202 then growth: with the offset rewound to the failed chunk's start
#     and a marker recording the OLD size, the first window is capped at that
#     size so the re-cut is byte-identical (duplicate:true, dup=1), the rest is
#     new, reassembly of the new rows equals exactly the appended bytes (zero
#     overlap), the marker is removed.
#   * a retry drain whose CAPPED first window fails AGAIN rewrites the marker
#     with the SAME end (the failed chunk's offset + bytes), never the grown
#     file size — so the drain after that still re-cuts [P, E1) byte-identically
#     (duplicate:true) and the new rows still equal exactly the appended bytes;
#     had the marker carried the grown size, the third cut would have ended at
#     the new cell edge and handed the server a row overlapping [P, E1).
#   * source pins: the served ingest curl line carries --max-time/--retry/
#     --retry-delay 3/--retry-max-time/$KB_CURL_RETRY_ALL, and NO hook script
#     (flush, kb-mcp-call, brief, update) carries --connect-timeout; the ingest
#     line carries no --fail; the idempotent-endpoint warning comment exists.
# DEPENDS ON C10 (RIG): POST /__fail (times, code; 0 = dropped connection),
#   injected hits recorded, dedupe keyed on (session_uri, hash).
. /rig/lib.sh

new_home transport
stub_reset
cfg_rest
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
ST="$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$ST/.checkpoints-trusted-since"
WORK="$HOME/work/retryproj"
mkdir -p "$WORK"
NOW=$(date +%s)
# 2 attempts per curl chain (the shipped default is 2 retries = 3 attempts; the
# default itself is pinned below from the source). Two chains -> <= 4 hits.
export KB_FLUSH_CURL_RETRY=1
hits() { stub_count '.injected != null'; }
marker() { cat "$ST/$1.retry" 2>/dev/null; }
sums() { log_count ' (ok|partial) bytes='; }
fire_pc() { # <transcript> <sid> -- fire PreCompact; SUMS = the summary-line count BEFORE it (for wait_firing)
  SUMS=$(sums)
  fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$1" "$WORK" "$2" PreCompact)"
}
offset_of() { cat "$ST/$1.offset" 2>/dev/null || echo 0; }

# ----------------------------------------------------- 1. one 502: curl retry
step "stub_fail 1 502: curl's own retry absorbs it (1 extra hit, no FAILED, no marker)"
T1="$WORK/one.jsonl"
mk_transcript "$T1" 30 one
stub_fail 1 502
fire_pc "$T1" s-one
wait_firing "$SUMS" 60 || fail "the drain never completed: $(tail -2 "$LOGF" | tr '\n' '|')"
assert_eq "1" "$(hits)" "exactly one injected hit"
assert_eq "2" "$(stub_count '.path=="/api/ingest-session"')" "two requests in total (the failed one + the retry)"
assert_eq "0" "$(log_count 'chunk FAILED')" "no FAILED line"
assert_file_missing "$ST/s-one.retry" "no retry marker"
assert_eq "$(wc -c <"$T1")" "$(cat "$ST/s-one.offset")" "offset at EOF"

# ---------------------------------------------- 2. nine 502s: marker with size
step "stub_fail 9 502: one FAILED line, <= 4 hits, .retry = <epoch> 1 502 <size>"
stub_reset
T2="$WORK/nine.jsonl"
mk_transcript "$T2" 30 nine
S2=$(wc -c <"$T2")
stub_fail 9 502
fire_pc "$T2" s-nine
wait_log "chunk FAILED \(502\)" 60 || fail "no FAILED line: $(tail -2 "$LOGF" | tr '\n' '|')"
settle
assert_eq "1" "$(log_count 'chunk FAILED \(502\)')" "exactly one FAILED line"
assert_le "$(hits)" "4" "at most 4 hits (2 attempts x 2 chains)"
assert_ge "$(hits)" "3" "more than one chain was tried (script-level retry happened)"
assert_file_contains "$LOGF" "chunk FAILED (502) at offset 0 — will resume retry#1 ev=PreCompact sid=s-nine" "the FAILED line names retry#1 and ends with ev= sid="
M=$(marker s-nine)
note "marker: [$M]"
set -- $M
assert_eq "4" "$#" "the marker has 4 fields"
case "${1:-}" in *[!0-9]* | '') fail "field 1 is not an epoch: [$1]" ;; *) assert_ge "$1" "$NOW" "field 1 is a fresh epoch" ;; esac
assert_eq "1" "${2:-}" "field 2: n=1 (first failed drain)"
assert_eq "502" "${3:-}" "field 3: the code"
assert_eq "$S2" "${4:-}" "field 4: the failed chunk's end (== the size: one newline-terminated window)"
assert_eq "0" "$(offset_of s-nine)" "the offset did not advance past the first-sight 0 (nothing was accepted)"
step "a second failed drain counts n=2; a clean drain that reaches the size removes the marker"
stub_fail 9 502
fire_pc "$T2" s-nine
wait_log "retry#2" 60 || fail "no retry#2 line"
assert_eq "2" "$(marker s-nine | awk '{print $2}')" "n=2 after the second failed drain"
stub_reset # clears the fail queue
settle     # the failed child releases the session lock a few ms after its FAILED line; a clean firing behind a held lock would defer
fire_pc "$T2" s-nine
wait_firing "$SUMS" 60 || fail "the clean drain never completed"
assert_eq "$S2" "$(cat "$ST/s-nine.offset")" "the clean drain reached EOF"
assert_file_missing "$ST/s-nine.retry" "the marker is removed once the offset >= the end it recorded"

# ------------------------------------------------ 3. dropped connection (000)
step "stub_fail 9 0 (dropped connection): FAILED (000) is on the transient set and leaves a marker"
stub_reset
T3="$WORK/drop.jsonl"
mk_transcript "$T3" 30 drop
stub_fail 9 0
fire_pc "$T3" s-drop
wait_log "chunk FAILED \(000\)" 60 || fail "no FAILED (000) line: $(tail -2 "$LOGF" | tr '\n' '|')"
assert_eq "000" "$(marker s-drop | awk '{print $3}')" "marker code 000"

# ------------------------------------------------------------ 4. 401: no marker
step "401 is configuration: one hit, no curl retry, no script retry, no marker"
stub_reset
T4="$WORK/auth.jsonl"
mk_transcript "$T4" 30 auth
stub_fail 9 401
fire_pc "$T4" s-auth
wait_log "chunk FAILED \(401\)" 30 || fail "no FAILED (401) line"
settle
settle
assert_eq "1" "$(hits)" "exactly ONE hit for a 401 (never retried)"
assert_file_missing "$ST/s-auth.retry" "no marker for a 401"
assert_eq "0" "$(offset_of s-auth)" "offset not advanced past the first-sight 0"
assert_file_contains "$LOGF" "chunk FAILED (401) at offset 0 — will resume ev=PreCompact sid=s-auth" "the 401 line carries no retry# but keeps ev= sid="

# --------------------------------------------------- 5. 429: long back-off
step "429: marker + the long back-off, honoured by the next Stop"
stub_reset
T5="$WORK/rate.jsonl"
mk_big_transcript "$T5" 4200000
stub_fail 9 429
fire_pc "$T5" s-rate
wait_log "chunk FAILED \(429\)" 60 || fail "no FAILED (429) line"
assert_eq "429" "$(marker s-rate | awk '{print $3}')" "marker code 429"
stub_reset
printf '%s' "$((NOW - 1900))" >"$ST/s-rate.last" # every size/age condition holds; only the marker can close the gate
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T5" "$WORK" s-rate Stop)"
settle
settle
assert_eq "0" "$(stub_total)" "a Stop inside the 429 back-off makes zero requests (4 MB pending, .last old)"
# THE line that separates the 429/503 rule from the exponential one: n=1 would
# be due after 300 s; a 429 marker is held for KB_FLUSH_RETRY_MAX (3600 s).
awk -v e="$((NOW - 1000))" '{ $1 = e; print }' "$ST/s-rate.retry" >"$ST/s-rate.retry.t" && mv "$ST/s-rate.retry.t" "$ST/s-rate.retry"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T5" "$WORK" s-rate Stop)"
settle
settle
assert_eq "0" "$(stub_total)" "a 429 marker 1000 s old still closes the Stop (long back-off 3600 s, not the exponential 300 s)"
assert_eq "429" "$(marker s-rate | awk '{print $3}')" "the marker is untouched by the closed Stop"
awk -v e="$((NOW - 4000))" '{ $1 = e; print }' "$ST/s-rate.retry" >"$ST/s-rate.retry.t" && mv "$ST/s-rate.retry.t" "$ST/s-rate.retry"
SUMS=$(log_count ' (ok|partial) bytes=')
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T5" "$WORK" s-rate Stop)"
wait_firing "$SUMS" 120 || fail "the Stop after the back-off never drained: $(tail -2 "$LOGF" | tr '\n' '|')"
assert_eq "$(wc -c <"$T5")" "$(cat "$ST/s-rate.offset")" "the retry drain reached EOF"
assert_file_missing "$ST/s-rate.retry" "the 429 marker is gone after the drain"
assert_ge "$(stub_count '.path=="/api/ingest-session"')" "5" "the 4.2 MB went over in several chunks"

# ------------------------------------------ 6. lost 202 then growth: re-cut
step "lost 202 then growth: the first window is capped at the marker size -> byte-identical re-cut, duplicate:true, zero overlap"
stub_reset
export KB_FLUSH_CHUNK=200000 KB_FLUSH_SEND_MAX=260000
T6="$WORK/lost.jsonl"
mk_big_transcript "$T6" 430000 # EOF inside cell 3 (400000..600000)
S6=$(wc -c <"$T6")
fire_pc "$T6" s-lost
wait_firing "$SUMS" 60 || fail "the first drain never completed"
N6=$(stub_count '.session_uri=="session:s-lost"')
assert_ge "$N6" "3" "the first pass cut >= 3 chunks ($N6)"
P=$(stub_field '.session_uri=="session:s-lost"' '.flush_offset' | tail -1)   # start of the last (EOF-cell) chunk
E1=$(cat "$ST/s-lost.offset")                                              # its end = last newline at or before S6
note "last chunk of pass 1: [$P, $E1) of $S6"
assert_ge "$P" "390000" "fixture: the last chunk starts at the lattice point B_2 (the byte after the last newline at or before 400000)"
# Emulate the lost 202: the server stored [P, E1) but the client never saw it —
# the offset stays at P and the marker records the size the failed drain saw.
printf '%s' "$P" >"$ST/s-lost.offset"
printf '%s 1 000 %s' "$((NOW - 4000))" "$S6" >"$ST/s-lost.retry"
mk_big_transcript "$HOME/grow6.jsonl" 300000
cat "$HOME/grow6.jsonl" >>"$T6"
S6B=$(wc -c <"$T6")
assert_ge "$S6B" "600001" "fixture: the file now runs past the cell edge the failed chunk was cut in"
SUMS=$(log_count ' (ok|partial) bytes=')
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T6" "$WORK" s-lost Stop)" # the marker (4000 s old) opens the gate
wait_firing "$SUMS" 120 || fail "the retry drain never completed: $(tail -2 "$LOGF" | tr '\n' '|')"
assert_eq "$S6B" "$(cat "$ST/s-lost.offset")" "the retry drain reached the new EOF"
assert_file_missing "$ST/s-lost.retry" "the marker is gone (offset >= the recorded size)"
# Pass-2 requests are the ones after the first N6 records for this uri.
stub_requests | "$RIG_JQ" -c 'select(.session_uri=="session:s-lost")' | tail -n +"$((N6 + 1))" >"$HOME/pass2.jsonl"
NP2=$(wc -l <"$HOME/pass2.jsonl")
assert_ge "$NP2" "2" "pass 2 sent >= 2 chunks ($NP2)"
FIRST_OFF=$(head -1 "$HOME/pass2.jsonl" | "$RIG_JQ" -r '.flush_offset')
FIRST_BYTES=$(head -1 "$HOME/pass2.jsonl" | "$RIG_JQ" -r '.transcript_bytes')
FIRST_DUP=$(head -1 "$HOME/pass2.jsonl" | "$RIG_JQ" -r '.result.duplicate')
assert_eq "$P" "$FIRST_OFF" "the first re-cut chunk starts at the failed chunk's offset"
assert_eq "$((E1 - P))" "$FIRST_BYTES" "the first re-cut chunk ends where the failed one did (capped at the marker size, not at the new cell edge)"
assert_eq "true" "$FIRST_DUP" "the byte-identical re-cut is answered duplicate:true"
assert_eq "1" "$(grep ' sid=s-lost' "$LOGF" | grep -oE 'dup=[0-9]+' | tail -1 | cut -d= -f2)" "the log line counts dup=1"
assert_eq "$E1" "$(sed -n 2p "$HOME/pass2.jsonl" | "$RIG_JQ" -r '.flush_offset')" "the second chunk starts exactly at the re-cut chunk's end"
"$RIG_JQ" -j 'select(.result.duplicate==false) | .transcript' "$HOME/pass2.jsonl" >"$HOME/new.bytes"
tail -c +"$((E1 + 1))" "$T6" >"$HOME/want.bytes"
if cmp -s "$HOME/new.bytes" "$HOME/want.bytes"; then
  pass "the NEW rows reassemble to exactly the bytes after the lost chunk (zero overlap, nothing lost)"
else
  fail "new rows differ from the appended bytes: $(cmp "$HOME/new.bytes" "$HOME/want.bytes" 2>&1 | head -1)"
fi

# ------------------------ 6b. a retry drain that fails again keeps the chunk END
step "a capped retry drain that FAILS again keeps the failed chunk's END in the marker, so the next retry still re-cuts byte-identically"
stub_reset
T7="$WORK/lost2.jsonl"
mk_big_transcript "$T7" 430000 # EOF inside cell 3 (400000..600000), same shape as step 6
fire_pc "$T7" s-lost2
wait_firing "$SUMS" 60 || fail "the first drain never completed"
N7=$(stub_count '.session_uri=="session:s-lost2"')
P7=$(stub_field '.session_uri=="session:s-lost2"' '.flush_offset' | tail -1)
E7=$(cat "$ST/s-lost2.offset")
note "last chunk of pass 1: [$P7, $E7)"
# Lost 202 on the last chunk: offset rewound to its start, marker = its end
# (what the hook itself writes: offset + bytes -- here == the size, the fixture
# ends in a newline), then the file grows past the cell edge.
printf '%s' "$P7" >"$ST/s-lost2.offset"
printf '%s 1 000 %s' "$((NOW - 4000))" "$E7" >"$ST/s-lost2.retry"
mk_big_transcript "$HOME/grow7.jsonl" 300000
cat "$HOME/grow7.jsonl" >>"$T7"
S7B=$(wc -c <"$T7")
assert_ge "$S7B" "600001" "fixture: the file now runs past the cell edge the failed chunk was cut in"
# The retry drain itself fails (both chains): 502 is on curl's own retry set,
# so the 4 hits are deterministic (2 attempts x 2 chains). NO stub_reset from
# here to the clean pass: a reset would forget the dedupe key pass 1 planted.
stub_fail 9 502
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T7" "$WORK" s-lost2 Stop)" # the 4000 s-old marker opens the gate
wait_log "chunk FAILED \(502\) at offset $P7 — will resume retry#2 ev=Stop sid=s-lost2" 60 || fail "no retry#2 FAILED line: $(tail -2 "$LOGF" | tr '\n' '|')"
settle
stub_fail 0 502 # disarm what the 4 hits left (no reset: the dedupe state must survive)
M7=$(marker s-lost2)
note "marker after the failed retry: [$M7] (file size now $S7B)"
set -- $M7
assert_eq "2" "${2:-}" "n=2 after the failed retry drain"
assert_eq "502" "${3:-}" "the code"
assert_eq "$E7" "${4:-}" "field 4 is STILL the failed chunk's end $E7, not the grown size $S7B"
assert_eq "$P7" "$(offset_of s-lost2)" "the offset did not move"
assert_eq "$P7" "$(stub_field '.session_uri=="session:s-lost2" and .injected!=null' '.flush_offset' | head -1)" "the failed re-cut started at the lost chunk's offset"
assert_eq "$((E7 - P7))" "$(stub_field '.session_uri=="session:s-lost2" and .injected!=null' '.flush_bytes' | head -1)" "and was capped at its end (byte-identical, even though the file had grown)"
# Grow AGAIN, then the clean retry (n=2 -> 600 s back-off; the marker is aged).
mk_big_transcript "$HOME/grow7b.jsonl" 100000
cat "$HOME/grow7b.jsonl" >>"$T7"
S7C=$(wc -c <"$T7")
awk -v e="$((NOW - 4000))" '{ $1 = e; print }' "$ST/s-lost2.retry" >"$ST/s-lost2.retry.t" && mv "$ST/s-lost2.retry.t" "$ST/s-lost2.retry"
SUMS=$(log_count ' (ok|partial) bytes=')
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T7" "$WORK" s-lost2 Stop)"
wait_firing "$SUMS" 120 || fail "the clean retry drain never completed: $(tail -2 "$LOGF" | tr '\n' '|')"
assert_eq "$S7C" "$(cat "$ST/s-lost2.offset")" "the clean retry drain reached the new EOF"
assert_file_missing "$ST/s-lost2.retry" "the marker is gone (offset >= the recorded end)"
# Pass 3 = the ANSWERED (non-injected) requests for this uri after pass 1's N7.
stub_requests | "$RIG_JQ" -c 'select(.session_uri=="session:s-lost2" and .injected==null)' | tail -n +"$((N7 + 1))" >"$HOME/pass3.jsonl"
NP3=$(wc -l <"$HOME/pass3.jsonl")
assert_ge "$NP3" "3" "pass 3 sent >= 3 chunks ($NP3)"
assert_eq "$P7" "$(head -1 "$HOME/pass3.jsonl" | "$RIG_JQ" -r '.flush_offset')" "the first re-cut chunk starts at the lost chunk's offset"
assert_eq "$((E7 - P7))" "$(head -1 "$HOME/pass3.jsonl" | "$RIG_JQ" -r '.transcript_bytes')" "and still ends at $E7 (the marker kept the end across the failed retry)"
assert_eq "true" "$(head -1 "$HOME/pass3.jsonl" | "$RIG_JQ" -r '.result.duplicate')" "the byte-identical re-cut is answered duplicate:true"
assert_eq "$E7" "$(sed -n 2p "$HOME/pass3.jsonl" | "$RIG_JQ" -r '.flush_offset')" "the second chunk starts exactly at the re-cut chunk's end"
"$RIG_JQ" -j 'select(.result.duplicate==false) | .transcript' "$HOME/pass3.jsonl" >"$HOME/new3.bytes"
tail -c +"$((E7 + 1))" "$T7" >"$HOME/want3.bytes"
if cmp -s "$HOME/new3.bytes" "$HOME/want3.bytes"; then
  pass "the NEW rows reassemble to exactly the bytes after the lost chunk (zero overlap across two growths and a failed retry)"
else
  fail "new rows differ from the appended bytes: $(cmp "$HOME/new3.bytes" "$HOME/want3.bytes" 2>&1 | head -1)"
fi
unset KB_FLUSH_CHUNK KB_FLUSH_SEND_MAX

# ------------------------------------------------------------ 7. source pins
step "source pins: the served curl lines"
H="$HOOKS/flush-session-to-kb.sh"
LINE=$(sed -n '/^kb_rest_post() {/,/^}/p' "$H" | tr '\n' ' ')
assert_contains "$LINE" '--max-time "${KB_FLUSH_CURL_MAX:-60}"' "ingest curl: --max-time 60 default"
assert_contains "$LINE" '--retry "${KB_FLUSH_CURL_RETRY:-2}"' "ingest curl: --retry 2 default"
assert_contains "$LINE" '--retry-delay 3' "ingest curl: --retry-delay 3"
assert_contains "$LINE" '--retry-max-time "${KB_FLUSH_CURL_RETRY_MAX:-30}"' "ingest curl: --retry-max-time 30 default"
assert_contains "$LINE" '$KB_CURL_RETRY_ALL' "ingest curl: the probed --retry-all-errors slot"
assert_contains "$LINE" '/api/ingest-session' "it is the ingest-session line"
assert_not_contains "$LINE" "--connect-timeout" "ingest curl: no --connect-timeout (KB 1195)"
assert_not_contains "$LINE" "--fail" "ingest curl: no --fail (401/403 must not be retried)"
assert_not_contains "$LINE" " -f " "ingest curl: no -f"
assert_file_contains "$H" "RE-POSTING IS SAFE ONLY AGAINST THE DEDUPING /api/ingest-session" "the idempotent-endpoint warning comment exists"
assert_file_contains "$H" 'grep -q -- --retry-all-errors' "--retry-all-errors is probed, not assumed"
for f in flush-session-to-kb.sh kb-mcp-call.sh kb-session-brief.sh update.sh; do
  N=$(grep -c -- '--connect-timeout' "$HOOKS/$f" 2>/dev/null) || N=0
  assert_eq "0" "$N" "$f carries no --connect-timeout"
  N=$(grep -c -- '--max-time' "$HOOKS/$f" 2>/dev/null) || N=0
  assert_ge "$N" "1" "$f still bounds its curls with --max-time"
done

finish

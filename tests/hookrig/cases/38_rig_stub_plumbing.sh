#!/usr/bin/env bash
# CASE: the rig's OWN plumbing for wave C (item C10) -- stub-only, no hook fires
# PINS: what the stub must be able to do before cases 29-37 can prove anything
#       about the hooks. Asserted directly against the stub with curl, so a
#       failure here is a rig defect, never a hook defect.
#         * DEDUPE KEY (KB 33783 root cause, KB 34161 wave S; proposal_v2 App. F;
#           stub_contract.py Queue.take): a transcript chunk is identified by
#           (session_uri, content_hash) across EVERY project -- the same bytes
#           under the same session uri from ANOTHER project answer 202 with
#           duplicate:true and a null queue_id, byte-identical to a same-project
#           repeat (no oracle, rules 10/12). The legacy (kind, project, hash)
#           arbiter applies only when no session_uri arrives or it is degenerate
#           ('session:'), mirroring src/kbcore/queue_enqueue.py:180,202-206,268-270
#           and src/kbcore/queue_groups.py:55-83. Before this re-key the stub
#           deduped on sha256(text) alone, LOOSER than the server, so case 27's
#           "second sweep is a no-op" passed for the wrong reason.
#         * 202 BODY is exactly today's four keys (case 08 pins it through the
#           hook; this pins it on a refused cross-project resend too).
#         * FLUSH STAMPS (src/kbrest/schemas.py:80-87): the eight flush_* fields
#           are recorded VERBATIM (a number stays a number, a string a string),
#           consistency (src/kbcore/models.py:268-274) is recorded and NEVER a
#           refusal, and a malformed stamp is a 422 exactly like pydantic
#           (fullmatch: a trailing newline is refused; a non-string session_uri
#           is a 422 too, never a crashed handler that looks like curl 000).
#         * POST /__fail {"times": N, "code": C}: the next N ingest answers are
#           HTTP C; C = 0 closes the connection with no response (curl 000).
#           Each hit is recorded so a retry chain can be counted, and an injected
#           failure does NOT consume the dedupe key (the retried chunk lands).
#         * POST /__delay {"ms": N} (RIG_DELAY_MS sets the default): every
#           ingest answer is held N ms; {} restores the env default.
#         * lib.sh: stub_fail / stub_delay FAIL LOUDLY when the stub answers 400
#           (a typo can never silently arm nothing); pin_sweep_clock /
#           expose_flock; fixtures that carry a "timestamp" (C1 reads the first).
. /rig/lib.sh

new_home rigplumb
stub_reset

post() { # <body-file> -> http code; body in $HOME/r.json
  "$RIG_CURL" -sS -o "$HOME/r.json" -w '%{http_code}' -X POST "$STUB/api/ingest-session" \
    -H "Authorization: Bearer $RIG_TOKEN" -H "Content-Type: application/json" \
    --data @"$1" 2>/dev/null
}
body() { # <project> <text> [session_uri] [extra-jq-object] -> file
  local f="$HOME/body.$$.json" extra="${4:-}"
  [ -n "$extra" ] || extra='{}'
  "$RIG_JQ" -nc --arg p "$1" --arg t "$2" --arg u "${3:-}" --argjson x "$extra" \
    '{project:$p, transcript:$t} + (if $u == "" then {} else {session_uri:$u} end) + $x' >"$f"
  printf '%s' "$f"
}
last() { # jq-expr over the LAST recorded rest ingest
  stub_requests | "$RIG_JQ" -r "select(.path==\"/api/ingest-session\") | $1" | tail -1
}
r() { "$RIG_JQ" -r "$1" "$HOME/r.json"; }

X='{"type":"user","timestamp":"2026-09-11T00:00:00.000Z","text":"chunk X"}'$'\n'
Y='{"type":"user","timestamp":"2026-09-11T00:00:00.000Z","text":"chunk Y"}'$'\n'

# ==================================================== 1. the 202 body shape
step "a fresh chunk answers 202 with exactly the four documented keys"
assert_eq "202" "$(post "$(body alpha "$X" session:s1)")" "first submission -> 202"
assert_eq "queue_id,duplicate,requeued,permanently_failed" "$(r 'keys_unsorted|join(",")')" "202 body keys"
assert_eq "false" "$(r .duplicate)" "a fresh (uri, hash) is not a duplicate"
assert_eq "1" "$(r .queue_id)" "queue ids start at 1 after a reset"
assert_eq "session" "$(last .dedupe_key)" "record: keyed on the session uri"
assert_eq "session:s1" "$(last .session_uri_normalised)" "record: the normalised uri"

# ====================================== 2. (session_uri, hash) across projects
step "the same (uri, hash) from ANOTHER project is refused as a plain duplicate"
assert_eq "202" "$(post "$(body beta "$X" session:s1)")" "cross-project resend still answers 202"
assert_eq "true" "$(r .duplicate)" "duplicate:true"
assert_eq "null" "$(r .queue_id)" "queue_id null"
assert_eq "queue_id,duplicate,requeued,permanently_failed" "$(r 'keys_unsorted|join(",")')" \
  "no extra key betrays the canonical project or id (no oracle)"
assert_not_contains "$(cat "$HOME/r.json")" "alpha" "the answer never names the canonical project"
assert_eq "cross-project" "$(last .duplicate_kind)" "record (harness only): cross-project refusal"

step "the same (uri, hash) from the SAME project is the ordinary live duplicate"
assert_eq "202" "$(post "$(body alpha "$X" session:s1)")" "same-project repeat -> 202"
assert_eq "true" "$(r .duplicate)" "duplicate:true"
assert_eq "same-project" "$(last .duplicate_kind)" "record: same-project repeat"

step "the same bytes under a DIFFERENT session uri are a new chunk (uri is part of the key)"
assert_eq "202" "$(post "$(body alpha "$X" session:s2)")" "new uri -> 202"
assert_eq "false" "$(r .duplicate)" "not a duplicate: the key is (uri, hash), not hash alone"

# ================================================ 3. the legacy fallback key
step "no session_uri -> the legacy (kind, project, hash) arbiter"
assert_eq "202" "$(post "$(body alpha "$Y")")" "no uri, alpha -> 202"
assert_eq "false" "$(r .duplicate)" "first time under alpha"
assert_eq "legacy" "$(last .dedupe_key)" "record: legacy key"
assert_eq "null" "$(last .session_uri_normalised)" "record: no uri"
assert_eq "202" "$(post "$(body alpha "$Y")")" "no uri, alpha again -> 202"
assert_eq "true" "$(r .duplicate)" "second time under alpha is a duplicate"
assert_eq "202" "$(post "$(body beta "$Y")")" "no uri, beta -> 202"
assert_eq "false" "$(r .duplicate)" "the legacy key is PER PROJECT: beta's copy is a new row"

step "a degenerate 'session:' uri is dropped at the door and falls back to the legacy key"
assert_eq "202" "$(post "$(body gamma "$Y" 'session:')")" "'session:' -> 202"
assert_eq "false" "$(r .duplicate)" "gamma had no legacy row yet"
assert_eq "legacy" "$(last .dedupe_key)" "record: legacy key (the uri was degenerate)"
assert_eq "null" "$(last .session_uri_normalised)" "record: normalised to null"
assert_eq "session:" "$(last .session_uri)" "record: the degenerate value AS PRESENTED is still visible"

# ====================================================== 4. flush_* stamps
step "the eight flush_* fields are recorded verbatim; consistency is recorded, never refused"
Z='{"type":"user","timestamp":"2026-09-11T00:00:00.000Z","text":"chunk Z with a stamp"}'$'\n'
ZLEN=$(printf '%s' "$Z" | wc -c | tr -d ' ')
STAMP=$("$RIG_JQ" -nc --argjson b "$ZLEN" '{flush_offset:1400000, flush_bytes:$b, flush_size:2000000,
  flush_event:"PreCompact", flush_mtime:1757548800, flush_box:"rig-box.local",
  flush_project_by:"gitfirst", flush_branch_of:"0123abcd"}')
assert_eq "202" "$(post "$(body alpha "$Z" session:s3 "$STAMP")")" "a fully stamped chunk -> 202"
assert_eq "1400000" "$(last .flush_offset)" "flush_offset recorded"
assert_eq "number" "$(last '.flush_offset|type')" "flush_offset recorded as a NUMBER (sent as n)"
assert_eq "$ZLEN" "$(last .flush_bytes)" "flush_bytes recorded"
assert_eq "2000000" "$(last .flush_size)" "flush_size recorded"
assert_eq "PreCompact" "$(last .flush_event)" "flush_event recorded"
assert_eq "1757548800" "$(last .flush_mtime)" "flush_mtime recorded"
assert_eq "rig-box.local" "$(last .flush_box)" "flush_box recorded"
assert_eq "gitfirst" "$(last .flush_project_by)" "flush_project_by recorded"
assert_eq "0123abcd" "$(last .flush_branch_of)" "flush_branch_of recorded"
assert_eq "true" "$(last .flush_consistent)" "offset+bytes <= size and bytes == raw -> consistent"

STAMP2=$("$RIG_JQ" -nc --argjson b "$ZLEN" '{flush_offset:1999990, flush_bytes:$b, flush_size:2000000}')
assert_eq "202" "$(post "$(body alpha "$Z" session:s4 "$STAMP2")")" "an INCONSISTENT stamp is still 202 (telemetry only)"
assert_eq "false" "$(last .flush_consistent)" "offset+bytes > size -> recorded inconsistent"
assert_eq "false" "$(r .duplicate)" "and the row was still taken"

assert_eq "202" "$(post "$(body alpha "$Z" session:s5 '{"flush_offset":"12"}')")" "a digit STRING is lax-accepted like pydantic"
assert_eq "string" "$(last '.flush_offset|type')" "but recorded VERBATIM as a string -- the drift a case must see"
assert_eq "null" "$(last .flush_consistent)" "a partial stamp (no bytes/size) places nothing"

step "no stamp -> no flush_* key in the record at all"
assert_eq "202" "$(post "$(body alpha "$Z" session:s6)")" "unstamped -> 202"
assert_eq "false" "$(last 'has("flush_offset")')" "flush_offset absent from the record"
assert_eq "false" "$(last 'has("flush_consistent")')" "flush_consistent absent from the record"

step "a malformed stamp is a 422, exactly like the pydantic model"
assert_eq "422" "$(post "$(body alpha "$Z" session:s7 '{"flush_event":"Pre-Compact1"}')")" "flush_event outside ^[A-Za-z]+$ -> 422"
assert_eq "422" "$(post "$(body alpha "$Z" session:s7 '{"flush_bytes":2000001}')")" "flush_bytes > 2,000,000 -> 422"
assert_eq "422" "$(post "$(body alpha "$Z" session:s7 '{"flush_offset":-1}')")" "flush_offset < 0 -> 422"
assert_eq "422" "$(post "$(body alpha "$Z" session:s7 '{"flush_project_by":"GitFirst"}')")" "flush_project_by outside ^[a-z-]+$ -> 422"
assert_eq "422" "$(post "$(body alpha "$Z" session:s7 '{"flush_box":"rig box"}')")" "flush_box with a space -> 422"
assert_eq "422" "$(post "$(body alpha "$Z" session:s7 '{"flush_event":"PreCompact\n"}')")" "a trailing newline is refused (fullmatch, like the server's anchored pattern)"
assert_eq "422" "$(post "$(body alpha "$Z" "" '{"session_uri":123}')")" "a non-string session_uri -> 422 (never a crashed handler = curl 000)"
assert_eq "422" "$(last .status)" "and the refusal is RECORDED with status 422 (a dropped connection would not be)"
assert_eq "202" "$(post "$(body alpha "$Z" session:s7 '{"flush_offset":null}')")" "an explicit null is 'absent' -> 202"

# ========================================================= 5. POST /__fail
stub_reset
step "__fail: the next N ingest answers carry the injected code, then service resumes"
stub_fail 2 502
W='{"type":"user","timestamp":"2026-09-11T00:00:00.000Z","text":"chunk W"}'$'\n'
assert_eq "502" "$(post "$(body alpha "$W" session:f1)")" "1st answer: 502"
assert_eq "502" "$(post "$(body alpha "$W" session:f1)")" "2nd answer: 502"
assert_eq "202" "$(post "$(body alpha "$W" session:f1)")" "3rd answer: 202 -- the queue is spent"
assert_eq "false" "$(r .duplicate)" "an injected failure did NOT consume the dedupe key: the retry lands as a new row"
assert_eq "2" "$(stub_count '.injected==502')" "both injected hits are recorded (a retry chain is countable)"
assert_eq "3" "$(stub_count '.path=="/api/ingest-session"')" "three requests in total"

step "__fail code 0: the connection is closed with no response (curl reports 000)"
stub_fail 1 0
assert_eq "000" "$(post "$(body alpha "$W" session:f2)")" "dropped connection -> 000"
assert_eq "0" "$(last .status)" "the dropped hit is recorded with status 0"
assert_eq "0" "$(last .injected)" "and marked injected 0"
assert_eq "202" "$(post "$(body alpha "$W" session:f2)")" "the very next request is served normally"
assert_eq "false" "$(r .duplicate)" "and lands as a new row"

step "__fail with a 4xx: 401 and 429 are injectable too (C6's no-marker / long-backoff paths)"
stub_fail 1 401
assert_eq "401" "$(post "$(body alpha "$W" session:f3)")" "injected 401"
stub_fail 1 429
assert_eq "429" "$(post "$(body alpha "$W" session:f3)")" "injected 429"
assert_eq "202" "$(post "$(body alpha "$W" session:f3)")" "then 202"

step "__fail also fails an MCP kb_ingest_session tool call"
stub_fail 1 503
MCPB=$("$RIG_JQ" -nc --arg t "$W" '{jsonrpc:"2.0",id:1,method:"tools/call",params:{name:"kb_ingest_session",arguments:{project:"alpha",transcript:$t,session_uri:"session:m1"}}}')
MC=$("$RIG_CURL" -sS -o "$HOME/m.json" -w '%{http_code}' -X POST "$STUB/mcp" \
  -H "Authorization: Bearer $RIG_TOKEN" -H "Content-Type: application/json" -H "Mcp-Session-Id: x" \
  --data "$MCPB" 2>/dev/null)
assert_eq "503" "$MC" "MCP tools/call ingest -> injected 503"

step "a malformed knob body is refused loudly by lib.sh (stub answers 400, stub_fail returns 1)"
if ( stub_fail 2 five ) >/dev/null 2>&1; then
  fail "stub_fail 2 five returned 0 -- the stub's 400 was swallowed"
else
  pass "stub_fail 2 five returns non-zero (the stub's 400 is checked, not swallowed)"
fi
assert_eq "202" "$(post "$(body alpha "$W" session:f5)")" "and nothing was armed by the typo"
if ( stub_delay five ) >/dev/null 2>&1; then
  fail "stub_delay five returned 0 -- the stub's 400 was swallowed"
else
  pass "stub_delay five returns non-zero"
fi

step "__reset clears an armed failure queue"
stub_fail 5 503
stub_reset
assert_eq "202" "$(post "$(body alpha "$W" session:f4)")" "after a reset the next answer is 202 again"

# ======================================================== 6. POST /__delay
step "__delay holds every ingest answer for N ms"
stub_delay 1500
T=$("$RIG_CURL" -sS -o /dev/null -w '%{time_total}' -X POST "$STUB/api/ingest-session" \
  -H "Authorization: Bearer $RIG_TOKEN" -H "Content-Type: application/json" \
  --data @"$(body alpha "$W" session:d1)" 2>/dev/null)
if awk -v t="$T" 'BEGIN{exit !(t >= 1.4)}'; then
  pass "an ingest answer took ${T}s with a 1500 ms delay armed"
else
  fail "the delay did not apply: ${T}s"
fi
stub_delay 0
T=$("$RIG_CURL" -sS -o /dev/null -w '%{time_total}' -X POST "$STUB/api/ingest-session" \
  -H "Authorization: Bearer $RIG_TOKEN" -H "Content-Type: application/json" \
  --data @"$(body alpha "$W" session:d2)" 2>/dev/null)
if awk -v t="$T" 'BEGIN{exit !(t < 1.0)}'; then
  pass "with the delay cleared an answer takes ${T}s"
else
  fail "the delay stuck: ${T}s"
fi

step "__delay {} (stub_delay with no argument) restores the stub's RIG_DELAY_MS default, not a literal 0"
# On this lane the stub runs without RIG_DELAY_MS, so the default is 0; the
# pin is that {} and {"ms":0} are DIFFERENT requests -- {} asks for the env
# default (whatever the lane set), so a wincase started with RIG_DELAY_MS keeps
# its think-time after `stub_delay`, and only `stub_delay 0` removes it.
stub_delay 1500
DEF=$("$RIG_CURL" -sS -X POST "$STUB/__delay" --data '{}' 2>/dev/null | "$RIG_JQ" -r .ms)
assert_eq "0" "$DEF" "{} answers the env default (0 on this lane: no RIG_DELAY_MS on the stub)"
stub_delay 1500
stub_delay
T=$("$RIG_CURL" -sS -o /dev/null -w '%{time_total}' -X POST "$STUB/api/ingest-session" \
  -H "Authorization: Bearer $RIG_TOKEN" -H "Content-Type: application/json" \
  --data @"$(body alpha "$W" session:d3)" 2>/dev/null)
if awk -v t="$T" 'BEGIN{exit !(t < 1.0)}'; then
  pass "after a bare stub_delay the 1500 ms arm is gone (${T}s)"
else
  fail "a bare stub_delay did not restore the default: ${T}s"
fi

# ============================================================ 7. lib.sh side
step "fixtures carry a first-record timestamp (C1 reads it), overridable for pre-stamp transcripts"
mk_transcript "$HOME/fx.jsonl" 3 fx
FTS=$(head -n1 "$HOME/fx.jsonl" | "$RIG_JQ" -r .timestamp)
case "$FTS" in
  20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z) pass "mk_transcript stamps ISO-8601 Z with ms: $FTS" ;;
  *) fail "mk_transcript timestamp is not ISO-8601 Z: [$FTS]" ;;
esac
NOWTS=$("$RIG_PY" -c 'from datetime import datetime,timezone; print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"))')
if [ "$FTS" \> "$NOWTS" ]; then
  pass "the fixture timestamp compares newer than the case's own clock (never same-second as an install stamp)"
else
  fail "fixture timestamp [$FTS] is not newer than now [$NOWTS]"
fi
mk_big_transcript "$HOME/fxbig.jsonl" 5000
BTS=$(head -n1 "$HOME/fxbig.jsonl" | "$RIG_JQ" -r .timestamp)
case "$BTS" in
  20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z) pass "mk_big_transcript stamps the same shape: $BTS" ;;
  *) fail "mk_big_transcript timestamp is not ISO-8601 Z: [$BTS]" ;;
esac
RIG_FIXTURE_TS="2020-01-01T00:00:00.000Z" mk_transcript "$HOME/fxold.jsonl" 2 old
assert_eq "2020-01-01T00:00:00.000Z" "$(head -n1 "$HOME/fxold.jsonl" | "$RIG_JQ" -r .timestamp)" \
  "RIG_FIXTURE_TS pins a PRE-stamp first record"
if "$RIG_JQ" -e . "$HOME/fxold.jsonl" >/dev/null 2>&1; then pass "fixture records are valid JSONL"; else fail "fixture is not JSONL"; fi

step "pin_sweep_clock exports KB_BACKFILL_NOW to fired scripts; unpin clears it"
pin_sweep_clock 1700000000
assert_eq "1700000000" "$(bash -c 'printf %s "${KB_BACKFILL_NOW:-unset}"')" "a child process sees the pinned clock"
unpin_sweep_clock
assert_eq "unset" "$(bash -c 'printf %s "${KB_BACKFILL_NOW:-unset}"')" "unpinned"

step "expose_flock writes a fd-preserving wrapper (the positive control for case 36)"
expose_flock
assert_file_contains "$HOME/bin/flock" 'exec /rigbin/flock "$@"' "the wrapper execs the parked binary"
if [ -x "$HOME/bin/flock" ]; then pass "wrapper is executable"; else fail "wrapper not executable"; fi
# On the base image /rigbin does not exist; the wrapper's behaviour is only
# meaningful on winsim (case 36). Here we pin its shape, not its execution.

finish

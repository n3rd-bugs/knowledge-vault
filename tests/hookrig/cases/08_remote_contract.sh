#!/usr/bin/env bash
# CASE: REMOTE (REST) transport contract, end to end
# PINS: in REMOTE mode the hook POSTs to <KB_REST_URL>/api/ingest-session with a
#       bearer (flush-session-to-kb.sh:134-137) and the server contract is
#       202 {queue_id,duplicate,requeued,permanently_failed} / 401 / 422 / 400
#       (src/kbrest/server.py:156-172, src/kbcore/ingest_queue.py:84-96).
#       The hook treats ONLY 2xx as accepted and never advances the offset
#       otherwise (flush-session-to-kb.sh:144-147).
# ALSO PINS: an EMPTY transcript is stopped by the HOOK (SIZE<=OFFSET at
#       flush-session-to-kb.sh:77) and never reaches the server -- the server's
#       400-on-empty is the second line of defence, asserted directly.
. /rig/lib.sh

post() { # body-file [token] -> http code, response in $HOME/r.json
  local f="$1" tok="${2-$RIG_TOKEN}"
  if [ "$tok" = "__none__" ]; then
    curl -sS -o "$HOME/r.json" -w '%{http_code}' -X POST "$STUB/api/ingest-session" \
      -H 'Content-Type: application/json' --data @"$f"
  else
    curl -sS -o "$HOME/r.json" -w '%{http_code}' -X POST "$STUB/api/ingest-session" \
      -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' --data @"$f"
  fi
}

new_home rest
stub_reset
cfg_rest
install_hooks >/dev/null
WORK="$HOME/work/restrepo"
mkdir -p "$WORK"
T="$WORK/transcript.jsonl"

step "happy path through the HOOK -> 202"
mk_transcript "$T" 12 rest
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-rest PreCompact)"
assert_eq "0" "$RC" "hook exits 0"
assert_empty_file "$OUT_FILE" "stdout empty"
wait_stub 1 25 || fail "the hook never POSTed"
assert_eq "1" "$(stub_count '.path=="/api/ingest-session"')" "one REST submission"
assert_eq "202" "$(stub_field '.transport=="rest"' '.status')" "server returned 202"
assert_eq "restrepo" "$(stub_field '.transport=="rest"' '.project')" "project from cwd"
assert_eq "gz" "$(stub_field '.transport=="rest"' '.encoding')" "sent as transcript_gz"
assert_eq "$(wc -c <"$T")" "$(stub_field '.transport=="rest"' '.transcript_bytes')" "full transcript delivered"
assert_file_contains "$LOGF" "ok bytes=" "success logged"
assert_eq "$(wc -c <"$T")" "$(cat "$HOME/.claude/hooks/state/sess-rest.offset")" "offset checkpointed"

step "WRONG bearer through the HOOK -> 401, offset not advanced, session survives"
stub_reset
cfg_rest "kbrig_wrong000000000000000000000"
printf '%s\n' '{"type":"user","text":"after-401"}' >>"$T"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-401 PreCompact)"
assert_eq "0" "$RC" "hook exits 0 despite the 401"
assert_empty_file "$OUT_FILE" "stdout empty despite the 401"
wait_stub 1 25 || fail "no request reached the stub"
assert_eq "401" "$(stub_field '.transport=="rest"' '.status')" "server rejected the bearer"
wait_log "chunk FAILED \\(401\\)" 20 && pass "the 401 is logged with its code" || fail "the 401 is not in the log"
assert_file_missing "$HOME/.claude/hooks/state/sess-401.offset" "offset NOT advanced on 401"

step "EMPTY transcript: the hook refuses to submit at all"
stub_reset
cfg_rest
: >"$WORK/empty.jsonl"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WORK/empty.jsonl" "$WORK" sess-empty PreCompact)"
assert_eq "0" "$RC" "hook exits 0 on an empty transcript"
settle
settle
assert_eq "0" "$(stub_total)" "no request made for a zero-byte transcript"

step "server contract, asserted directly (the hook's second line of defence)"
printf '{"project":"restrepo","kind":"session","transcript":"hello\\n"}' >"$HOME/ok.json"
assert_eq "401" "$(post "$HOME/ok.json" __none__)" "no Authorization header -> 401"
assert_eq "401" "$(post "$HOME/ok.json" nope_0000000000000000)" "invalid bearer -> 401"

printf 'this is not json' >"$HOME/bad.json"
assert_eq "422" "$(post "$HOME/bad.json")" "unparseable body -> 422"
printf '{"transcript":"hi"}' >"$HOME/noproj.json"
assert_eq "422" "$(post "$HOME/noproj.json")" "missing project -> 422"
printf '{"project":5,"transcript":"hi"}' >"$HOME/badtype.json"
assert_eq "422" "$(post "$HOME/badtype.json")" "wrong-typed project -> 422"

printf '{"project":"restrepo","transcript":""}' >"$HOME/empty.json"
assert_eq "400" "$(post "$HOME/empty.json")" "empty transcript -> 400"
printf '{"project":"restrepo","transcript":"   \\n  "}' >"$HOME/blank.json"
assert_eq "400" "$(post "$HOME/blank.json")" "whitespace-only transcript -> 400"
printf '{"project":"restrepo","transcript":"hi","transcript_gz":"H4sIAA=="}' >"$HOME/both.json"
assert_eq "400" "$(post "$HOME/both.json")" "transcript AND transcript_gz -> 400"
printf '{"project":"restrepo","transcript":"hi","kind":"bogus"}' >"$HOME/kind.json"
assert_eq "400" "$(post "$HOME/kind.json")" "unknown kind -> 400"
printf '{"project":"restrepo","transcript_gz":"not-base64!!"}' >"$HOME/gz.json"
assert_eq "400" "$(post "$HOME/gz.json")" "undecodable transcript_gz -> 400"
python3 -c "
import json
open('$HOME/big.json','w').write(json.dumps({'project':'restrepo','transcript':'y'*1000001}))"
assert_eq "400" "$(post "$HOME/big.json")" "content over 1,000,000 bytes -> 400"

step "202 response body carries the documented keys"
printf '{"project":"restrepo","kind":"session","transcript":"fresh content\\n"}' >"$HOME/fresh.json"
assert_eq "202" "$(post "$HOME/fresh.json")" "valid submission -> 202"
assert_eq "queue_id,duplicate,requeued,permanently_failed" \
  "$(jq -r 'keys_unsorted | join(",")' "$HOME/r.json")" "202 body keys"
assert_eq "false" "$(jq -r '.duplicate' "$HOME/r.json")" "first submission is not a duplicate"
assert_ne "null" "$(jq -r '.queue_id' "$HOME/r.json")" "queue_id assigned"
assert_eq "202" "$(post "$HOME/fresh.json")" "identical resubmission is still 202 (idempotent)"
assert_eq "true" "$(jq -r '.duplicate' "$HOME/r.json")" "identical content flagged duplicate"
assert_eq "null" "$(jq -r '.queue_id' "$HOME/r.json")" "duplicate carries a null queue_id"

finish

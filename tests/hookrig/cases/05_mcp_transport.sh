#!/usr/bin/env bash
# CASE: MCP transport wire behaviour
# PINS: kb-mcp-call.sh speaks MCP streamable-HTTP as a THREE-request sequence --
#       POST initialize (the session id arrives as the Mcp-Session-Id RESPONSE
#       HEADER, not in the body), POST notifications/initialized, POST tools/call
#       (kb-mcp-call.sh:16-32). A flush in MCP mode calls the tool
#       kb_ingest_session and the payload travels as transcript_gz
#       (base64(gzip(chunk))), not plaintext (flush-session-to-kb.sh:101-110).
# ALSO PINS: the project is derived from the event's cwd basename, slugified
#       (flush-session-to-kb.sh:43-45) -- "My_Repo" -> "my-repo".
. /rig/lib.sh

new_home mcp
stub_reset
cfg_mcp
install_hooks >/dev/null

WORK="$HOME/work/My_Repo"
mkdir -p "$WORK"
T="$WORK/transcript.jsonl"
mk_transcript "$T" 40 payload

fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-mcp PreCompact)"
assert_eq "0" "$RC" "hook exits 0"
wait_stub 3 25 || fail "the stub never saw the three-request MCP sequence"

step "request sequence"
SEQ=$(stub_field '.transport=="mcp"' '.rpc' | tr '\n' ' ' | sed 's/ $//')
assert_eq "initialize notifications/initialized tools/call" "$SEQ" "initialize -> notifications/initialized -> tools/call"

step "session id: issued as a response header, echoed on every later request"
SID=$(stub_field '.rpc=="initialize"' '.issued_session_id')
assert_ne "" "$SID" "stub issued an Mcp-Session-Id"
assert_eq "$SID" "$(stub_field '.rpc=="notifications/initialized"' '.mcp_session_id')" "initialized carries the session id"
assert_eq "$SID" "$(stub_field '.rpc=="tools/call"' '.mcp_session_id')" "tools/call carries the session id"
assert_eq "true" "$(stub_field '.rpc=="tools/call"' '.session_known')" "server recognises the session"

step "tool call shape"
assert_eq "kb_ingest_session" "$(stub_field '.rpc=="tools/call"' '.tool')" "tool name is kb_ingest_session"
assert_eq "my-repo" "$(stub_field '.tool=="kb_ingest_session"' '.project')" "project slugified from cwd basename My_Repo"
assert_eq "session" "$(stub_field '.tool=="kb_ingest_session"' '.kind')" "kind=session"
assert_eq "gz" "$(stub_field '.tool=="kb_ingest_session"' '.encoding')" "payload sent as transcript_gz"
# session_uri is always present now: it is the identity that lets the drain
# coalesce a session's chunks into ONE extraction instead of one per 900 KB
# window. The point of this assertion is unchanged — no EMPTY git keys may
# leak into the body — so the expected set grows by exactly that field.
assert_eq "kind,project,session_uri,transcript_gz" "$(stub_field '.tool=="kb_ingest_session"' '.arg_keys | join(",")')" "no extra arguments"
assert_eq "false" "$(stub_field '.tool=="kb_ingest_session"' '.is_error')" "stub accepted the submission"

step "the SESSION IDENTITY that arrives is real, not a bare scheme"
# THE TRIPWIRE THIS CASE WAS MISSING. Every assertion above passed for weeks
# while session_uri was literally "session:" on every machine: the hook computes
# the key in the parent shell and the value is used inside a DETACHED child, and
# it was never exported, so it expanded to the empty string. A bare "session:" is
# not an identity, it is a wildcard -- the drain groups by it, so 137 rows from
# SIX unrelated projects were concatenated into cross-project extractions stamped
# with whichever project sorted first. Asserting "a submission arrived" cannot
# see that; asserting the VALUE can.
SURI=$(stub_field '.tool=="kb_ingest_session"' '.session_uri // empty')
assert_ne "" "$SURI" "a session identity was sent at all"
assert_ne "session:" "$SURI" "the identity is not a DEGENERATE bare scheme"
assert_eq "session:sess-mcp" "$SURI" "the identity is this session's id"

step "the decoded payload is byte-identical to what was fed in"
stub_requests | jq -j 'select(.tool=="kb_ingest_session") | .transcript' >"$HOME/received"
if cmp -s "$T" "$HOME/received"; then
  pass "decoded transcript_gz == the transcript file ($(wc -c <"$T") bytes)"
else
  fail "payload mismatch: fed $(wc -c <"$T") bytes, stub decoded $(wc -c <"$HOME/received")"
fi

step "auth: the bearer from kb-flush.env is what reaches the server"
assert_eq "3" "$(stub_count '.auth=="ok"')" "all three requests authenticated"
assert_eq "0" "$(stub_count '.auth=="bad" or .auth=="missing"')" "no unauthenticated requests"

step "a WRONG bearer is a hard failure, not a silent success"
stub_reset
cfg_mcp "kbrig_wrongtoken00000000000000"
printf '%s\n' '{"type":"user","text":"delta"}' >>"$T"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-mcp-bad PreCompact)"
assert_eq "0" "$RC" "hook still exits 0 with a bad bearer"
wait_stub 1 15 || fail "no request reached the stub"
settle
assert_eq "1" "$(stub_count '.auth=="bad"')" "the stub rejected the bearer (401)"
assert_eq "0" "$(stub_count '.tool=="kb_ingest_session"')" "no tool call after a 401"
assert_file_contains "$LOGF" "FAILED" "the failure is logged"
assert_offset_not_advanced "$HOME/.claude/hooks/state/sess-mcp-bad.offset" "offset NOT advanced on failure"

finish

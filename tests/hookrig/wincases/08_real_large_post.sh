#!/usr/bin/env bash
# CASE: a LARGE, SLOW REST POST from real Git Bash lands (C6, proposal_v2
#       Appendix G; KB 1195 — Schannel aborts a healthy slow request under
#       --connect-timeout, and a >= 1 MB body with a 12 s server think-time is
#       exactly the shape that used to die).
# PINS: on the Windows box, the served ingest curl line (NO --connect-timeout,
#       --max-time 60, --retry 2 / --retry-delay 3 / --retry-max-time 30)
#         * carries a >= 1,000,000-byte body (the hook's plaintext fallback,
#           reached when gzip is unusable: a 999 KB incompressible transcript
#           escapes to ~1.01 MB of JSON) through a `stub_delay 12000` and is
#           answered 202 — not aborted, not retried, offset at EOF;
#         * carries the same bytes over the gzip path (~940 KB on the wire —
#           base64 of gzip cannot exceed the 1,000,000-byte row cap) through the
#           same 12 s delay and is answered 202;
#         * the hook parent still returns in well under a second (the wait is
#           the detached child's), and nothing is logged as a failure.
#       The 12 s delay is armed per step with `stub_delay 12000` and restored
#       with a bare `stub_delay` (the stub's env default) afterwards.
. "$RIG_SANDBOX/winlib.sh"

new_home largepost
load_portable_block
stub_reset

cfg_rest
install_hooks >/dev/null 2>&1

PROJ_DIR="$HOME/work/Large Post"
mkdir -p "$PROJ_DIR"
TRANS="$PROJ_DIR/transcript.jsonl"
# ~999 KB of INCOMPRESSIBLE lines (random base64 payloads, each record with a
# timestamp like a real transcript), every line newline-terminated so the whole
# file is one window: the row cap is 1,000,000 transcript bytes, so the window
# knobs sit just under it and above the file.
"$RIG_PY" - "$TRANS" <<'EOF'
import base64, json, os, sys
from datetime import datetime, timedelta, timezone
path = sys.argv[1]
# +2 s: the first record must be STRICTLY newer than .checkpoints-trusted-since (lib.sh mk_transcript does the same)
ts = (datetime.now(timezone.utc) + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
w = i = 0
with open(path, "w", encoding="utf-8", newline="\n") as fh:
    while True:
        blob = base64.b64encode(os.urandom(750)).decode()
        line = json.dumps({"type": "assistant", "timestamp": ts, "i": i, "blob": blob}) + "\n"
        if w + len(line) > 999500:
            break
        fh.write(line); w += len(line); i += 1
EOF
SIZE=$(wc -c <"$TRANS" | tr -d ' ')
assert_ge "$SIZE" "990000" "fixture: ~1 MB transcript ($SIZE bytes)"
export KB_FLUSH_CHUNK=999900 KB_FLUSH_SEND_MAX=999900
TRANS_WIN=$(win_path "$TRANS")
PROJ_WIN=$(win_path "$PROJ_DIR")
note "native transcript = $TRANS_WIN"

# ------------------------------------------- 1. plaintext body >= 1 MB, 12 s delay
step "a >= 1 MB plaintext POST through a 12 s server delay is answered 202"
# A gzip that fails makes the hook take its documented plaintext fallback
# (`gzip -c | base64` yields nothing -> `transcript` is sent verbatim).
printf '#!/usr/bin/env bash\nexit 1\n' >"$HOME/bin/gzip"
chmod +x "$HOME/bin/gzip"
stub_delay 12000
T0=$(date +%s)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TRANS_WIN" "$PROJ_WIN" win-large-1)"
T1=$(date +%s)
assert_eq "0" "$RC" "the hook exits 0"
assert_le "$((T1 - T0))" "2" "the parent returned at once (the 12 s wait belongs to the detached child)"
wait_stub 1 90 || fail "no submission landed within 90 s: $(tail -2 "$LOGF" 2>/dev/null | tr '\n' '|')"
T2=$(date +%s)
assert_ge "$((T2 - T0))" "12" "the answer took the full think-time (the request was not cut short)"
assert_eq "1" "$(stub_count '.path=="/api/ingest-session"')" "exactly ONE request (no retry of a request that was still in flight)"
assert_eq "202" "$(stub_field '.path=="/api/ingest-session"' '.status' | tail -1)" "answered 202"
assert_eq "plain" "$(stub_field '.path=="/api/ingest-session"' '.encoding' | tail -1)" "the plaintext fallback was taken"
BODY=$(stub_field '.path=="/api/ingest-session"' '.bytes' | tail -1)
note "body on the wire: $BODY bytes"
assert_ge "$BODY" "1000000" "the POST body is >= 1,000,000 bytes"
assert_eq "$SIZE" "$(stub_field '.path=="/api/ingest-session"' '.transcript_bytes' | tail -1)" "the whole transcript arrived in one chunk"
wait_log "ok bytes=$SIZE" 30 || fail "no ok line: $(tail -2 "$LOGF" | tr '\n' '|')"
assert_eq "$SIZE" "$(cat "$HOOKS/state/win-large-1.offset")" "offset at EOF"
rm -f "$HOME/bin/gzip"

# ------------------------------------------- 2. the gzip path, same delay
step "the same bytes over the gzip path through the 12 s delay are answered 202"
stub_reset
stub_delay 12000
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TRANS_WIN" "$PROJ_WIN" win-large-2)"
assert_eq "0" "$RC" "the hook exits 0"
wait_stub 1 90 || fail "no gz submission landed within 90 s: $(tail -2 "$LOGF" 2>/dev/null | tr '\n' '|')"
assert_eq "202" "$(stub_field '.path=="/api/ingest-session"' '.status' | tail -1)" "answered 202"
assert_eq "gz" "$(stub_field '.path=="/api/ingest-session"' '.encoding' | tail -1)" "sent gzipped"
GBODY=$(stub_field '.path=="/api/ingest-session"' '.bytes' | tail -1)
note "gz body on the wire: $GBODY bytes"
assert_ge "$GBODY" "900000" "the gz body is still ~0.9 MB (incompressible payload)"
assert_eq "$SIZE" "$(stub_field '.path=="/api/ingest-session"' '.transcript_bytes' | tail -1)" "decodes to the whole transcript"
wait_log "ok bytes=$SIZE.*sid=win-larg" 30 || fail "no ok line for win-large-2"
stub_delay # restore the stub's default think-time

step "nothing was logged as a failure"
if grep -q "FAILED\|NOT CONFIGURED" "$LOGF" 2>/dev/null; then
  fail "the flush log records a failure: $(grep -m1 'FAILED\|NOT CONFIGURED' "$LOGF")"
else
  pass "no failures recorded in the flush log"
fi

finish

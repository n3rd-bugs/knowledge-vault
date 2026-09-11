#!/usr/bin/env bash
# CASE: a real end-to-end flush from Git Bash to the stub, with NATIVE paths
# PINS: the whole point of the exercise — on Windows the event JSON carries
#       `C:\Users\...\transcript.jsonl` (with spaces), the hook must convert it
#       before -f/basename, submit the delta, and checkpoint the offset so the
#       next firing resumes instead of re-sending. Every one of those steps has
#       a Windows-specific failure mode the Linux container cannot reach.
#       Wave C (C5, KB 33784) adds, on the same real box: a drained SessionEnd
#       KEEPS the offset at the transcript size (never deleted); a closed-gate
#       Stop makes 0 requests and no log line; an open-gate Stop's PARENT
#       returns in < 3 s (the dispatcher's Stop budget) while its child drains;
#       and a drain detached with nohup (Git Bash has no setsid) survives a 1 s
#       kb_timeout killing its parent. Delta records carry a "timestamp" like
#       real transcript records.
. "$RIG_SANDBOX/winlib.sh"

new_home flushe2e
load_portable_block
stub_reset

cfg_rest
install_hooks >/dev/null 2>&1

# A project directory whose name contains a SPACE — the normal Windows reality
# and the reason every path in the hooks is quoted.
PROJ_DIR="$HOME/work/My Project"
mkdir -p "$PROJ_DIR"
TRANS="$PROJ_DIR/transcript.jsonl"
mk_transcript "$TRANS" 40 winflush

# Hand the hook the NATIVE form, exactly as Claude Code on Windows would.
TRANS_WIN=$(win_path "$TRANS")
PROJ_WIN=$(win_path "$PROJ_DIR")
note "native transcript = $TRANS_WIN"
note "native cwd        = $PROJ_WIN"

step "kb_winpath resolves the native path the event will carry"
BACK=$(kb_winpath "$TRANS_WIN")
if [ -f "$BACK" ]; then pass "kb_winpath('$TRANS_WIN') resolves to an existing file"; else fail "kb_winpath produced [$BACK], which is not a file"; fi

step "PreCompact flush submits the transcript to the stub"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TRANS_WIN" "$PROJ_WIN" win-e2e-1)"
assert_eq "0" "$RC" "the hook exits 0 (never blocks Claude Code)"
wait_stub 1 60 || fail "no submission reached the stub within 60s"
assert_ge "$(stub_total)" 1 "at least one submission recorded"

step "the submission carries the right project and real content"
PROJ_SENT=$(stub_field '.transport=="rest"' '.project')
note "project submitted: [$PROJ_SENT]"
# The hook SLUGIFIES the directory name: "My Project" -> "my-project". That is
# the contract (project ids are lowercase-hyphenated), and it is exactly the
# path a Windows user hits, since spaces in directory names are the norm there.
assert_eq "my-project" "$PROJ_SENT" "project slugified from a cwd containing a space"
CONTENT=$(stub_field '.transport=="rest"' '.transcript' | head -c 400)
assert_contains "$CONTENT" "winflush" "the transcript content really made it across"
assert_eq "gz" "$(stub_field '.transport=="rest"' '.encoding')" "sent gzipped, as the REST contract requires"

step "the offset was checkpointed (so the next firing resumes)"
OFF_FILE=$(ls "$HOOKS/state/"*.offset 2>/dev/null | head -1)
if [ -n "$OFF_FILE" ]; then
  OFF=$(cat "$OFF_FILE")
  note "offset file = $OFF_FILE -> $OFF"
  assert_ge "$OFF" 1 "offset advanced past zero"
else
  fail "no offset file was written under $HOOKS/state"
fi

step "a second flush of the SAME session with no new bytes submits nothing"
# The offset is checkpointed PER SESSION, so this must reuse win-e2e-1: a new
# session id legitimately restarts at offset 0 (container case 07 pins that).
BEFORE=$(stub_total)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TRANS_WIN" "$PROJ_WIN" win-e2e-1)"
assert_eq "0" "$RC" "the no-op flush still exits 0"
settle
assert_eq "$BEFORE" "$(stub_total)" "no duplicate submission for an unchanged transcript"

step "appending new lines submits ONLY the delta"
SIZE_BEFORE=$(wc -c <"$TRANS")
"$RIG_PY" - "$TRANS" <<'EOF'
import json, sys
from datetime import datetime, timezone
ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    for i in range(15):
        fh.write(json.dumps({"type": "user", "timestamp": ts, "tag": "delta", "i": i, "text": f"delta message {i}"}) + "\n")
EOF
DELTA=$(( $(wc -c <"$TRANS") - SIZE_BEFORE ))
note "appended $DELTA bytes"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TRANS_WIN" "$PROJ_WIN" win-e2e-1)"
wait_stub $((BEFORE + 1)) 60 || fail "the delta flush never reached the stub"
# Byte count is the unambiguous assertion: .transcript is multi-line, so a
# text grep cannot tell one record from the next.
assert_eq "$DELTA" "$(stub_field '.transport=="rest"' '.transcript_bytes' | tail -1)" \
  "the last submission carried EXACTLY the appended bytes, not the whole file"
LASTTXT=$(stub_field '.transport=="rest"' '.transcript' | tail -c "$((DELTA + 64))")
assert_contains "$LASTTXT" "delta message" "the delta content was submitted"
assert_not_contains "$LASTTXT" "winflush message 0" "already-submitted content was NOT re-sent"

step "nothing was logged as a failure"
if [ -f "$LOGF" ]; then
  note "log: $(tail -3 "$LOGF" | tr '\n' '|')"
  if grep -q "FAILED\|NOT CONFIGURED" "$LOGF"; then
    fail "the flush log records a failure"
  else
    pass "no failures recorded in the flush log"
  fi
else
  pass "no failure log written"
fi

# ------------------------------------------------------------- wave C (C5)
grow() { # <bytes> <tag> -- append timestamped records until the file grew by >= <bytes>
  "$RIG_PY" - "$TRANS" "$1" "$2" <<'EOF'
import json, sys
from datetime import datetime, timezone
path, want, tag = sys.argv[1], int(sys.argv[2]), sys.argv[3]
ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
w = i = 0
with open(path, "a", encoding="utf-8", newline="\n") as fh:
    while w < want:
        pad = "x" * (37 + (i * 13) % 91)
        line = json.dumps({"type": "assistant", "timestamp": ts, "tag": tag, "i": i, "pad": pad}) + "\n"
        fh.write(line); w += len(line); i += 1
EOF
}
sums() { log_count ' (ok|partial) bytes='; }
ST="$HOOKS/state"
OKEY=win-e2e-1
NOW=$(date +%s)
# The piggyback sweep is OFF for these steps: on a real box a hook firing may
# detach `kb-flush-backfill.sh --auto` (C7), whose lines would break the
# closed-gate "no log line" and "0 requests" bounds below; case 31 pins the
# trigger itself, this lane pins the gate and the detach on real Git Bash.
export KB_SWEEP_INTERVAL=0

step "SessionEnd drains and KEEPS the offset file at the transcript size (never deleted)"
grow 20000 end
SIZE_END=$(wc -c <"$TRANS" | tr -d ' ')
SUMS=$(sums)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TRANS_WIN" "$PROJ_WIN" "$OKEY" SessionEnd)"
assert_eq "0" "$RC" "SessionEnd exits 0"
wait_firing "$SUMS" 60 || fail "the SessionEnd drain never completed: $(tail -2 "$LOGF" | tr '\n' '|')"
if [ -f "$ST/$OKEY.offset" ]; then pass "the offset file SURVIVES a drained SessionEnd"; else fail "the offset file was deleted on SessionEnd"; fi
assert_eq "$SIZE_END" "$(cat "$ST/$OKEY.offset" 2>/dev/null)" "offset == transcript size after SessionEnd"

step "a closed-gate Stop (nothing pending) makes 0 requests and writes no log line"
BEFORE=$(stub_total)
LINES=$(wc -l <"$LOGF" | tr -d ' ')
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TRANS_WIN" "$PROJ_WIN" "$OKEY" Stop)"
assert_eq "0" "$RC" "Stop exits 0"
settle
settle
assert_eq "$BEFORE" "$(stub_total)" "zero requests on a closed Stop"
assert_eq "$LINES" "$(wc -l <"$LOGF" | tr -d ' ')" "no log line on a closed Stop"
assert_eq "$SIZE_END" "$(cat "$ST/$OKEY.offset")" "offset untouched"

step "an open-gate Stop: the PARENT returns in < 3 s while its child drains the delta"
grow 70000 aged                                  # >= the 64 KB age floor
printf '%s' "$((NOW - 1900))" >"$ST/$OKEY.last"  # 30 min since the last drain start
rm -f "$ST/$OKEY.gate"                           # a real drain always leaves .last >= .gate
SIZE_OPEN=$(wc -c <"$TRANS" | tr -d ' ')
SUMS=$(sums)
T0=$(date +%s%N 2>/dev/null || date +%s)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TRANS_WIN" "$PROJ_WIN" "$OKEY" Stop)"
T1=$(date +%s%N 2>/dev/null || date +%s)
assert_eq "0" "$RC" "the open-gate Stop exits 0"
case "$T0$T1" in
  *[!0-9]*) fail "no usable clock for the wall time ([$T0] [$T1])" ;;
  *)
    if [ "${#T0}" -gt 12 ]; then MS=$(((T1 - T0) / 1000000)); else MS=$(((T1 - T0) * 1000)); fi
    note "open-gate Stop parent wall time: ${MS} ms (gate -> cached pin -> token file -> detach; no git, no mint in the parent)"
    assert_le "$MS" "2999" "the parent finished inside the dispatcher's 3 s Stop budget (${MS} ms)"
    ;;
esac
GATE=$(cat "$ST/$OKEY.gate" 2>/dev/null)
case "$GATE" in '' | *[!0-9]*) fail ".gate not stamped: [$GATE]" ;; *) pass ".gate stamped by the parent" ;; esac
wait_firing "$SUMS" 60 || fail "the open-gate drain never completed: $(tail -2 "$LOGF" | tr '\n' '|')"
assert_eq "$SIZE_OPEN" "$(cat "$ST/$OKEY.offset")" "the child drained to EOF"
assert_ge "$(cat "$ST/$OKEY.last")" "${GATE:-0}" ".last (written by the child) >= .gate"
assert_contains "$(tail -1 "$LOGF")" "ev=Stop sid=win-e2e-" "the ok line ends with ev=Stop sid="

step "the nohup'd drain survives a 1 s kb_timeout killing its parent"
grow 70000 timeout
printf '%s' "$((NOW - 1900))" >"$ST/$OKEY.last"
rm -f "$ST/$OKEY.gate"
SIZE_TO=$(wc -c <"$TRANS" | tr -d ' ')
SUMS=$(sums)
flush_event "$TRANS_WIN" "$PROJ_WIN" "$OKEY" Stop >"$HOME/ev.json"
# kb_timeout comes from the portable block loaded into THIS shell (never inside
# $(...): it memoises its backend). The wrapper lingers past its 1 s budget on
# purpose, so the watchdog really fires (rc 124) AFTER the hook has detached.
kb_timeout 1 bash -c 'bash "$1" <"$2"; sleep 4' _ "$HOOKS/flush-session-to-kb.sh" "$HOME/ev.json" </dev/null >/dev/null 2>&1
RCW=$?
assert_eq "124" "$RCW" "the watchdog fired (rc 124) on the wrapper around the hook"
wait_firing "$SUMS" 60 || fail "the drain did not survive the watchdog: $(tail -2 "$LOGF" | tr '\n' '|')"
assert_eq "$SIZE_TO" "$(cat "$ST/$OKEY.offset")" "the detached (nohup) drain still reached EOF"
if grep -q "FAILED\|NOT CONFIGURED\|lock lost" "$LOGF"; then fail "a failure was logged: $(grep -m1 'FAILED\|NOT CONFIGURED\|lock lost' "$LOGF")"; else pass "no failure logged across the wave-C steps"; fi

finish

#!/usr/bin/env bash
# CASE: a real end-to-end flush from Git Bash to the stub, with NATIVE paths
# PINS: the whole point of the exercise — on Windows the event JSON carries
#       `C:\Users\...\transcript.jsonl` (with spaces), the hook must convert it
#       before -f/basename, submit the delta, and checkpoint the offset so the
#       next firing resumes instead of re-sending. Every one of those steps has
#       a Windows-specific failure mode the Linux container cannot reach.
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
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    for i in range(15):
        fh.write(json.dumps({"type": "user", "tag": "delta", "i": i, "text": f"delta message {i}"}) + "\n")
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

finish

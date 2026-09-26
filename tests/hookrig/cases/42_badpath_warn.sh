#!/usr/bin/env bash
# IMAGE: winsim
# CASE: a transcript path that resolves to NO file is never a silent exit
#       (wave C.1, KB 40411 fix set item 5).
# PINS: flush-session-to-kb.sh's transcript check used to be
#       `[ -n "$T" ] && [ -f "$T" ] || exit 0`, reached BEFORE LOG/STATE are
#       defined and before the sweep piggyback -- so when every path conversion on
#       usama-home came out garbled, the box captured nothing for two weeks and
#       not one line said so anywhere. Now a NAMED transcript that is not a file:
#         * appends ONE `BUG: transcript not found` line carrying `ev=<event>` and
#           `sid=<sid8>` to the LITERAL $HOME/.claude/hooks/flush-session.log
#           (`BUG:` is a token the pre-brief's failure scanner lifts);
#         * is rate-limited per BOX (state/.badpath.stamp, 3600 s) -- a Stop or a
#           re-fired SessionEnd within the hour adds nothing, whatever the sid;
#         * still exits 0, prints nothing, and creates no offset and no pin.
#       Positive controls: an aged stamp logs again; an EXISTING transcript logs
#       no BUG line and really submits.
# RED on today's hooks (HEAD b2b37a3) -- expected FAIL lines:
#       "exactly ONE BUG line for sid=0badf11e -- expected [1], got [0]",
#       "...naming ev=SessionEnd -- expected [1], got [0]",
#       "the box-level stamp state/.badpath.stamp -- ... not a file",
#       "still ONE after a re-fire within the hour -- expected [1], got [0]",
#       "still ONE for another sid within the hour -- expected [1], got [0]",
#       "an aged stamp logs a second line -- expected [2], got [0]".
#       Exit 0 / empty stdout / no offset / no pin / the existing-transcript
#       control pass on both.
. /rig/lib.sh

RIG_HOMEBASE=/c/Users
new_home badpath
export KB_SWEEP_INTERVAL=0
cfg_rest
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"
ST="$HOOKS/state"
BADSTAMP="$ST/.badpath.stamp"

SID="0badf11e-1111-4111-8111-111111111111"
SID8="${SID:0:8}"
WIN_T="C:\\Users\\badpath\\.claude\\projects\\C--Users-badpath-gone\\$SID.jsonl"
WIN_CWD='C:\Users\badpath\gone'
end_event() { # <sid> <transcript> [event]
  "$RIG_JQ" -nc --arg s "$1" --arg t "$2" --arg c "$WIN_CWD" --arg e "${3:-SessionEnd}" \
    '{session_id:$s,transcript_path:$t,cwd:$c,hook_event_name:$e,reason:"exit"}'
}
offsets() { find "$ST" -maxdepth 1 -name '*.offset' 2>/dev/null | wc -l | tr -d ' '; }
bugs() { log_count "BUG: transcript not found.* sid=$1"; }

step "precondition: the named transcript really does not exist"
if [ -e "/c/Users/badpath/.claude/projects/C--Users-badpath-gone/$SID.jsonl" ]; then
  fail "fixture: the transcript exists"
else
  pass "C:\\...\\$SID.jsonl resolves to nothing"
fi

step "SessionEnd naming a missing transcript: one BUG line, exit 0, nothing else"
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(end_event "$SID" "$WIN_T")"
assert_eq "0" "$RC" "the hook exits 0"
assert_eq "" "$OUT" "...and prints nothing"
wait_log 'BUG: transcript not found' 5 || true
assert_eq "1" "$(bugs "$SID8")" "exactly ONE BUG line for sid=$SID8"
assert_eq "1" "$(log_count "BUG: transcript not found.* ev=SessionEnd sid=$SID8")" "...naming ev=SessionEnd"
note "the line: $(grep -F 'BUG: transcript not found' "$LOGF" 2>/dev/null | head -1)"
if [ -f "$BADSTAMP" ]; then pass "the box-level stamp state/.badpath.stamp exists"; else fail "the box-level stamp state/.badpath.stamp -- not a file"; fi
assert_eq "0" "$(offsets)" "no offset file was created"
assert_file_missing "$ST/$SID.project" "no pin was written"
settle
assert_eq "0" "$(stub_total)" "nothing was submitted"

step "rate limit: a re-fire within the hour adds nothing (same sid, then another sid)"
fire "$HOOKS/flush-session-to-kb.sh" "$(end_event "$SID" "$WIN_T")"
assert_eq "0" "$RC" "the re-fire exits 0"
settle
assert_eq "1" "$(log_count 'BUG: transcript not found')" "still ONE after a re-fire within the hour"
SID2="0badf22e-2222-4222-8222-222222222222"
fire "$HOOKS/flush-session-to-kb.sh" "$(end_event "$SID2" "C:\\Users\\badpath\\gone\\$SID2.jsonl" PreCompact)"
assert_eq "0" "$RC" "another session's firing exits 0"
settle
assert_eq "1" "$(log_count 'BUG: transcript not found')" "still ONE for another sid within the hour (the stamp is per BOX)"
assert_eq "0" "$(offsets)" "still no offset file"

step "POSITIVE CONTROL: an aged stamp logs again"
# Age every stamp that can gate this line, both ways a stamp is read (epoch
# content and mtime), without depending on the exact file name.
for _f in "$ST"/.badpath*; do
  [ -e "$_f" ] || continue
  printf '0\n' >"$_f"
  touch -d @1000000000 "$_f"
done
fire "$HOOKS/flush-session-to-kb.sh" "$(end_event "$SID" "$WIN_T")"
assert_eq "0" "$RC" "exits 0"
wait_log "BUG: transcript not found.*sid=$SID8" 5 || true
settle
assert_eq "2" "$(log_count 'BUG: transcript not found')" "an aged stamp logs a second line"

step "POSITIVE CONTROL: an EXISTING transcript logs no BUG line and submits"
mkdir -p "/c/Users/badpath/work/here"
mk_transcript "/c/Users/badpath/work/here/t.jsonl" 5 here
_before=$(log_count 'BUG: transcript not found')
_sums=$(log_count ' (ok|partial) bytes=')
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event 'C:\Users\badpath\work\here\t.jsonl' 'C:\Users\badpath\work\here' sess-here PreCompact)"
assert_eq "0" "$RC" "exits 0"
if wait_firing "$_sums" 30; then pass "the existing transcript drained"; else fail "the existing transcript never drained"; fi
assert_eq "$_before" "$(log_count 'BUG: transcript not found')" "no BUG line for a transcript that exists"
assert_ge "$(stub_count '.path=="/api/ingest-session" and .status==202')" "1" "...and it really submitted"

finish

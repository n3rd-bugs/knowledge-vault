#!/usr/bin/env bash
# CASE: BRANCH / FORK SEED (C1, Appendix C, KB 33784) — Claude Code resumes and
#       forks reuse a transcript's byte prefix under a NEW file id. Re-sending
#       that shared history would re-ingest the whole parent. The hook detects
#       the sibling with the same first line, cmp's to the first differing byte,
#       snaps DOWN to a newline and SEEDS the fork's offset there, BEFORE the
#       unseeded timestamp rule — so only the fork's own bytes ever leave.
# PINS (flush-session-to-kb.sh kb_branch_seed / kb_decide_offset):
#   * a fork = parent bytes + 2 appended lines sends ONLY the appended lines.
#   * the fork's .offset is pre-seeded to the parent's size (a line boundary).
#   * exactly one "branch of <parent8>: seeded offset=" line is logged.
#   * a PRE-STAMP parent (first record older than the checkpoint stamp) still
#     SEEDS — the branch check runs first, so the fork is never refused unseeded.
#   * a DIVERGENT fork (same first line, differs mid-file, parent kept growing)
#     fired under LC_ALL=C — where GNU cmp says "differ: char N", not "byte N"
#     — seeds at the last newline BEFORE the divergence and its own divergent
#     lines ARE sent (the parse-failure path once seeded at the parent's size and
#     silently skipped them).
#   * the seed is CAPPED at what the parent actually checkpointed: a parent with
#     NO offset file (never flushed) seeds nothing, and the fork falls to the
#     timestamp rule (here: refused as unseeded, loudly) rather than being seeded
#     past bytes nobody sent.
# DEPENDS ON C10 (RIG) for the flush_offset stamp assertion only; the seed is
#   also pinned stub-independently from the first submitted bytes.
. /rig/lib.sh

new_home branch
stub_reset
cfg_rest
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
# Stamp set to a time AFTER the fixtures' first record, so WITHOUT the branch
# check the fork would be refused as an unseeded pre-stamp transcript.
printf '2099-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"

WORK="$HOME/work/branchproj"
mkdir -p "$WORK"
PARENT="$WORK/parent-uuid.jsonl"
"$RIG_PY" - "$PARENT" <<'EOF'
import json, sys
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"type": "user", "timestamp": "2020-01-01T00:00:00Z", "text": "session opener"}) + "\n")
    for i in range(60):
        fh.write(json.dumps({"type": "assistant", "i": i, "text": "history line %d %s" % (i, "y" * 40)}) + "\n")
EOF
PSIZE=$(wc -c <"$PARENT")

step "the parent flushes normally (its history is now in the KB)"
KB_FLUSH_ALLOW_UNSEEDED=1 fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$PARENT" "$WORK" parent-uuid SessionEnd)"
wait_log "flush .* ok bytes=.* sid=parent-u" 60 || fail "parent never flushed"
assert_eq "$PSIZE" "$(cat "$HOOKS/state/parent-uuid.offset")" "parent checkpointed at its size"

step "a FORK = parent bytes + 2 new lines seeds at the parent size and sends only the 2 lines"
stub_reset
CHILD="$WORK/child-uuid.jsonl"
cp "$PARENT" "$CHILD"
"$RIG_PY" - "$CHILD" <<'EOF'
import json, sys
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"type": "user", "i": 100, "text": "forked question"}) + "\n")
    fh.write(json.dumps({"type": "assistant", "i": 101, "text": "forked answer"}) + "\n")
EOF
CSIZE=$(wc -c <"$CHILD")
APPENDED=$((CSIZE - PSIZE))
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$CHILD" "$WORK" child-uuid PreCompact)"
wait_log "flush .* ok bytes=.* sid=child-uu" 60 || fail "fork never drained: $(tail -3 "$LOGF" | tr '\n' '|')"

assert_eq "$CSIZE" "$(cat "$HOOKS/state/child-uuid.offset")" "the fork drained to its own EOF"
SENT=$(stub_field '.session_uri=="session:child-uuid"' '.transcript_bytes' | awk '{s+=$1} END{print s+0}')
assert_eq "$APPENDED" "$SENT" "ONLY the $APPENDED appended bytes were sent (not the shared history)"
stub_concat '.session_uri=="session:child-uuid"' '.transcript' >"$HOME/forkgot"
if grep -q "forked answer" "$HOME/forkgot" && ! grep -q "history line 3" "$HOME/forkgot"; then
  pass "the sent bytes are the fork's own lines, none of the parent history"
else
  fail "the fork sent shared history"
fi
assert_eq "1" "$(grep -c 'branch of parent-u.*: seeded offset=' "$LOGF")" "exactly one 'branch of' line"
assert_file_contains "$LOGF" "branch of parent-u: seeded offset=$PSIZE" "the logged seed is the parent size"
assert_not_contains "$(cat "$LOGF")" "child-uuid unseeded: refusing" "a pre-stamp parent seeds, never refused unseeded"
assert_file_missing "$HOOKS/state/child-uuid.unseeded" "no unseeded marker on a seeded fork"

step "the seed landed exactly on the parent-size line boundary"
# Stub-independent: the first submitted chunk BEGINS with the first appended line.
case "$(head -c 40 "$HOME/forkgot")" in
  '{"type": "user", "i": 100'*) pass "the first fork chunk starts at the first appended record (seed == PSIZE)" ;;
  *) fail "the first fork chunk does not start at the appended record: [$(head -c 60 "$HOME/forkgot")]" ;;
esac
# C10 (RIG): the stub records the flush_* stamps; the first chunk's offset is the seed.
FO=$(stub_field '.session_uri=="session:child-uuid"' '.flush_offset' | head -1)
assert_eq "$PSIZE" "$FO" "flush_offset of the first fork chunk == the seeded parent size (C10 stamp capture)"

# ------------------------------------------- divergent fork under LC_ALL=C
step "a DIVERGENT fork under LC_ALL=C seeds BEFORE the divergence and sends its own lines"
stub_reset
DIV="$WORK/diverg-uuid.jsonl"
read -r COMMON OWN <<<"$("$RIG_PY" - "$PARENT" "$DIV" <<'EOF'
import json, sys
parent, div = sys.argv[1], sys.argv[2]
lines = open(parent, "r", encoding="utf-8").read().splitlines(keepends=True)
common = "".join(lines[:30])           # first 30 records shared byte-for-byte
own = "".join(json.dumps({"type": "assistant", "i": i, "text": "DIVERGED line %d %s" % (i, "z" * 40)}) + "\n" for i in range(30, 45))
open(div, "w", encoding="utf-8").write(common + own)
print(len(common.encode()), len(own.encode()))
EOF
)"
DSIZE=$(wc -c <"$DIV")
assert_eq "$DSIZE" "$((COMMON + OWN))" "fixture: divergent fork = $COMMON shared + $OWN own bytes"
LC_ALL=C fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$DIV" "$WORK" diverg-uuid PreCompact)"
wait_log "flush .* ok bytes=.* sid=diverg-u" 60 || fail "divergent fork never drained: $(tail -3 "$LOGF" | tr '\n' '|')"
# Both flushed siblings (parent-uuid and child-uuid) share the same prefix, so
# either may be named as the parent; the pin is the seed VALUE.
assert_eq "1" "$(grep -cE "branch of (parent-u|child-uu): seeded offset=$COMMON " "$LOGF")" "seeded at the last newline before the divergence ($COMMON; cmp 'char N' parsed)"
DSENT=$(stub_field '.session_uri=="session:diverg-uuid"' '.transcript_bytes' | awk '{s+=$1} END{print s+0}')
assert_eq "$OWN" "$DSENT" "exactly the fork's own $OWN divergent bytes were sent"
stub_concat '.session_uri=="session:diverg-uuid"' '.transcript' >"$HOME/divgot"
assert_eq "15" "$(grep -c 'DIVERGED line' "$HOME/divgot")" "all 15 divergent lines are in the stub's reassembly"
assert_eq "0" "$(grep -c 'history line' "$HOME/divgot")" "no shared history was resent"
assert_eq "$DSIZE" "$(cat "$HOOKS/state/diverg-uuid.offset")" "the divergent fork drained to its EOF"

# --------------------------- parent with NO offset: no seed, timestamp rule
step "a fork of a parent with NO offset file is NOT seeded (falls to the timestamp rule)"
stub_reset
W2="$HOME/work/orphanproj"
mkdir -p "$W2"
OP="$W2/orphan-parent.jsonl"
cp "$PARENT" "$OP"                        # dated 2020, never flushed: no orphan-parent.offset
OF="$W2/orphan-fork.jsonl"
cp "$OP" "$OF"; printf '{"type":"user","i":900,"text":"fork of an unflushed parent"}\n' >>"$OF"
BEFORE=$(stub_total)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$OF" "$W2" orphan-fork PreCompact)"
settle; settle
assert_eq "$BEFORE" "$(stub_total)" "nothing sent: the seed would have skipped bytes nobody flushed"
assert_file_missing "$HOOKS/state/orphan-fork.offset" "no offset seeded from an unflushed parent"
assert_eq "0" "$(grep -c 'branch of orphan-p' "$LOGF")" "no 'branch of' line for an unflushed parent"
assert_file_contains "$LOGF" "unseeded: refusing to send" "the fork fell to the timestamp rule and was refused loudly (pre-stamp)"
# positive control: once the parent IS flushed, the same fork seeds
KB_FLUSH_ALLOW_UNSEEDED=1 fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$OP" "$W2" orphan-parent SessionEnd)"
wait_log "flush .* ok bytes=.* sid=orphan-p" 60 || fail "orphan parent never flushed"
stub_reset
rm -f "$HOOKS/state/orphan-fork.unseeded"   # lift the 24 h rate limit on the refusal line only
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$OF" "$W2" orphan-fork PreCompact)"
wait_log "flush .* ok bytes=.* sid=orphan-f" 60 || fail "the fork of the now-flushed parent never drained"
assert_eq "1" "$(grep -c 'branch of orphan-p.*: seeded offset=' "$LOGF")" "positive control: with the parent flushed the fork IS seeded"
assert_eq "$(wc -c <"$OF")" "$(cat "$HOOKS/state/orphan-fork.offset")" "and drained to its EOF"

finish

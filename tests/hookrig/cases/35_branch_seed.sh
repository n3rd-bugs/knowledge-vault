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

# ------------------ >1 MB newline-less prefix tail: the snap walks BACK to a newline
# PIN (wave C stage 3 A2, KB 33784): the seed snap uses the backfill's bf_snap
# arithmetic (count newlines, `head -n N | wc -c`) over 1 MB windows walking
# backwards -- never `tail -n1 | wc -c`, which uutils coreutils 0.8.0 (the
# owner box) truncates to 983040 bytes on a newline-less 1 MB window, landing
# the seed mid-line; and a single window with NO newline must not seed at its
# own start either. Parent = 20 lines + one 1.3 MB record; the fork copies the
# 20 lines and the first 1.2 MB of that record, then diverges. The shared
# prefix (cmp) therefore ends 1.2 MB past the last newline; the seed must be
# THAT newline (= PFX, the end of line 20), and everything after it is the
# fork's own (fragmented on the wire like any >SEND_MAX line).
step "a shared prefix whose last newline lies >1 MB behind it seeds at THAT newline"
stub_reset
W3="$HOME/work/giantproj"
mkdir -p "$W3"
GP="$W3/giant-parent.jsonl"
GF="$W3/giant-fork.jsonl"
read -r PFX GPSIZE GFSIZE <<<"$("$RIG_PY" - "$GP" "$GF" <<'EOF'
import json, sys
gp, gf = sys.argv[1], sys.argv[2]
head = "".join(json.dumps({"type": "user", "timestamp": "2020-01-01T00:00:00Z", "i": i, "text": "shared line %d" % i}) + "\n" for i in range(20)).encode()
giant = (json.dumps({"type": "assistant", "i": 20, "text": "G" * 1300000}) + "\n").encode()
own = "".join(json.dumps({"type": "assistant", "i": 30 + i, "text": "FORK-OWN line %d" % i}) + "\n" for i in range(3)).encode()
with open(gp, "wb") as fh:
    fh.write(head + giant)
fork_giant = giant[:1200000] + b'"}\n'  # the same first 1.2 MB of the record, then it diverges
with open(gf, "wb") as fh:
    fh.write(head + fork_giant + own)
print(len(head), len(head) + len(giant), len(head) + len(fork_giant) + len(own))
EOF
)"
assert_eq "20" "$(head -c $((PFX + 1200000)) "$GF" | tr -cd '\n' | wc -c | tr -d ' ')" "fixture: the only newlines before the divergence are the 20 shared lines (>1 MB of newline-less prefix follows)"
assert_ge "$((PFX + 1200000 - PFX))" "$((1048576 + 1))" "fixture: the divergence point is more than one 1 MB snap window past the last newline"
printf '%s' "$GPSIZE" >"$HOOKS/state/giant-parent.offset" # the parent is fully flushed (checkpoint = size)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$GF" "$W3" giant-fork PreCompact)"
wait_log "flush .* ok bytes=.* sid=giant-fo" 120 || fail "the giant fork never drained: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "1" "$(grep -c "branch of giant-pa: seeded offset=$PFX " "$LOGF")" "seeded at the newline after line 20 (PFX=$PFX), not mid-line"
assert_eq "" "$(head -c "$PFX" "$GF" | tail -c1)" "the byte before the seed is a newline"
GFO=$(stub_field '.session_uri=="session:giant-fork"' '.flush_offset' | head -1)
assert_eq "$PFX" "$GFO" "flush_offset of the first fork chunk == PFX"
stub_concat '.session_uri=="session:giant-fork"' '.transcript' >"$HOME/giantgot"
case "$(head -c 40 "$HOME/giantgot")" in
  '{"type": "assistant", "i": 20'*) pass "the first fork chunk starts at the giant record's first byte" ;;
  *) fail "the first fork chunk does not start at the giant record: [$(head -c 60 "$HOME/giantgot")]" ;;
esac
GSENT=$(stub_field '.session_uri=="session:giant-fork"' '.transcript_bytes' | awk '{s+=$1} END{print s+0}')
assert_eq "$((GFSIZE - PFX))" "$GSENT" "exactly the fork's own $((GFSIZE - PFX)) bytes were sent (nothing of the 20 shared lines)"
assert_eq "0" "$(grep -c 'shared line' "$HOME/giantgot")" "no shared line was resent"
assert_eq "3" "$(grep -c 'FORK-OWN line' "$HOME/giantgot")" "all 3 own lines arrived"
assert_eq "$GFSIZE" "$(cat "$HOOKS/state/giant-fork.offset")" "the giant fork drained to its EOF"

# ------------------ a 64 KiB newline-less window: the chunk trim holds it whole
# PIN (wave C stage 3, KB 33784 -- same defect class as the snap above): the
# chunk loop's last-newline trim uses the snap arithmetic (count newlines,
# `head -n N | wc -c`), never `tail -n1 | wc -c`. uutils coreutils 0.8.0 prints
# 0 bytes for `tail -n1` of a newline-less buffer of exactly 65536 bytes (983040
# for 1048576), so the old trim read a whole unterminated 64 KiB fragment as
# ending on a newline and SENT it as if complete. GNU prints the full buffer,
# so this step is a regression fence that the docker rig can only pass and the
# owner box (uutils) can fail. Transcript = one complete line + an unterminated
# record of exactly 65536 bytes: PreCompact sends the line and HOLDS the
# fragment (offset = the line's end); terminating it makes the next firing send
# the rest.
step "an unterminated 65536-byte fragment after one line is HELD, never sent as if complete"
stub_reset
# The pre-stamp-parent step above pushed the trust stamp to 2099; restore a past stamp so
# this fresh transcript is judged post-stamp and actually flows.
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"
W4="$HOME/work/fragproj"
mkdir -p "$W4"
FT="$W4/frag.jsonl"
read -r FL FSIZE <<<"$("$RIG_PY" - "$FT" <<'EOF'
import json, sys
from datetime import datetime, timedelta, timezone
ts = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")  # newer than the trust stamp
line = (json.dumps({"type": "user", "timestamp": ts, "i": 0, "text": "one whole line"}) + "\n").encode()
pre = b'{"type": "assistant", "i": 1, "text": "'
frag = pre + b"G" * (65536 - len(pre))
assert len(frag) == 65536, len(frag)
with open(sys.argv[1], "wb") as fh:
    fh.write(line + frag)
print(len(line), len(line) + len(frag))
EOF
)"
assert_eq "1" "$(tr -cd '\n' <"$FT" | wc -c | tr -d ' ')" "fixture: exactly one newline, then a 65536-byte unterminated fragment"
assert_eq "$((FSIZE - FL))" "65536" "fixture: the fragment is exactly 65536 bytes"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$FT" "$W4" frag-sess PreCompact)"
# A held tail is not a fully drained firing: the hook logs `partial bytes=`
# (will resume), never `ok bytes=`, for it.
wait_log "flush .* partial bytes=$FL \\(offset $FL/$FSIZE\\) .* sid=frag-ses" 60 || fail "the one whole line never drained as a partial firing: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "0" "$(grep -c 'ok bytes=.* sid=frag-ses' "$LOGF")" "no ok line: the firing is partial while the fragment is held"
assert_eq "$FL" "$(cat "$HOOKS/state/frag-sess.offset")" "the offset stops at the end of the one whole line (the fragment is HELD)"
assert_eq "$FL" "$(stub_field '.session_uri=="session:frag-sess"' '.transcript_bytes' | awk '{s+=$1} END{print s+0}')" "exactly the whole line's bytes were sent -- not the 65536-byte fragment"
assert_eq "0" "$(stub_concat '.session_uri=="session:frag-sess"' '.transcript' | grep -c 'GGGG')" "no fragment bytes on the wire"
printf '"}\n' >>"$FT"
FSIZE2=$(wc -c <"$FT")
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$FT" "$W4" frag-sess PreCompact)"
wait_log "flush .* ok bytes=$((FSIZE2 - FL)) .* sid=frag-ses" 60 || fail "the terminated record never drained: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "$FSIZE2" "$(cat "$HOOKS/state/frag-sess.offset")" "once terminated, the record is sent and the offset reaches EOF"

finish

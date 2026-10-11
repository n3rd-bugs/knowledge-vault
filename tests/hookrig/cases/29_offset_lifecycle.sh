#!/usr/bin/env bash
# CASE: OFFSET LIFECYCLE — never deleted by an event, swept only when the
#       transcript is gone (KB-FLUSH-LINEAGE never-delete-offsets v2, KB 33784).
# PINS (flush-session-to-kb.sh, wave C C1/C8; Appendix B writer invariant):
#   * SIZE=0 is UNREADABLE, not shrank: a failed/empty read never clobbers a good
#     checkpoint to 0 and never drains (V1-m4) — on the bearer arm AND the
#     CONNECTOR arm.
#   * a trailing partial line is HELD on EVERY bearer event INCLUDING SessionEnd
#     (V1-m7): the last un-terminated line is not sent, the offset stops at the
#     last newline, and nothing is lost (the sweep's tail-held state covers it).
#   * a shrank transcript (0 < SIZE < offset) logs reason=shrank and restarts 0,
#     and the 0 LANDS ON DISK even when nothing is sent afterwards (W3 replaces
#     the file — a noclobber write there would keep the stale value forever).
#   * the offset SURVIVES a fully-drained SessionEnd (no rm -f "$OFFF" anywhere).
#   * kb_state_sweep retires a session's state ONLY when the offset mtime > 30 d
#     AND the transcript is absent under BOTH $HOME/.claude/projects and
#     $CLAUDE_CONFIG_DIR/projects; a <okey>.retry beside it prints a loss note.
#     (The "child's own .lock.d is gone after a sweeping drain" pin lives in
#     case 36 — on this image flock exists and no dir is ever created.)
#   * an unseeded pre-stamp transcript is REFUSED (0 requests, <okey>.unseeded
#     written, one log line); KB_FLUSH_ALLOW_UNSEEDED=1 sends it from 0 and
#     clears the marker. A box with NO stamp gets one created as "now" by the
#     hook itself, so a DATED pre-existing transcript is refused there too,
#     while an UNDATED (synthetic) transcript still proceeds.
# NOTE: install.sh in this tree does not create $HOOKS/state (the hook does on
#       first firing), so every fixture written there is preceded by a mkdir.
# STATE FILE NAMES (Appendix I): <okey>.offset, .offset.lock[.d], .last, .gate,
#       .retry, .project, .unseeded, .sweep — the suffix hangs off <okey>, only
#       the lock keeps the .offset infix.
. /rig/lib.sh

new_home offlife
stub_reset
cfg_rest
install_hooks >/dev/null
mkdir -p "$HOOKS/state"

WORK="$HOME/work/lifeproj"
mkdir -p "$WORK"
# A stamp far in the PAST so freshly-written fixtures (their first record carries
# no timestamp, or a newer one) are treated as capturable, not pre-stamp.
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"

# ---------------------------------------------------------- 1. SIZE=0 guard
step "SIZE=0 is unreadable: no drain, no clobber of a good checkpoint"
T="$WORK/t.jsonl"
mk_transcript "$T" 30 alpha
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-life PreCompact)"
wait_firing 0 60 || fail "first drain never completed"
FULL=$(read_offset)
assert_ge "$FULL" "1" "a real drain advanced the offset"
stub_reset
: >"$T"                                   # truncate to zero bytes
BEFORE=$(stub_total)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-life PreCompact)"
assert_eq "0" "$RC" "hook exits 0 on a zero-byte transcript"
settle
settle
assert_eq "$BEFORE" "$(stub_total)" "SIZE=0 sent nothing"
assert_eq "$FULL" "$(read_offset)" "SIZE=0 did NOT clobber the checkpoint"
assert_not_contains "$(cat "$LOGF")" "reason=shrank" "SIZE=0 is not logged as shrank"
assert_file_contains "$LOGF" "size unreadable (0) — not draining" "SIZE=0 is logged as unreadable"

# ---------------------------------------------------- 1b. SIZE=0 on CONNECTOR
new_home offlifecon
cfg_connector
fake_claude_relay
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
WC="$HOME/work/conproj"
mkdir -p "$WC"
TC="$WC/t.jsonl"
mk_transcript "$TC" 20 conn
step "CONNECTOR arm: SIZE=0 is unreadable too (no relay, no clobber)"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TC" "$WC" sess-con PreCompact)"
wait_log "connector .* ok bytes=" 60 || fail "the connector drain never completed"
CFULL=$(cat "$HOOKS/state/sess-con.offset")
assert_ge "$CFULL" "1" "connector drain advanced the offset"
: >"$TC"
NREL=$(relay_count)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TC" "$WC" sess-con PreCompact)"
settle
settle
assert_eq "$NREL" "$(relay_count)" "SIZE=0 relayed nothing"
assert_eq "$CFULL" "$(cat "$HOOKS/state/sess-con.offset")" "SIZE=0 did NOT clobber the connector checkpoint"
assert_not_contains "$(cat "$LOGF")" "reason=shrank" "connector SIZE=0 is not logged as shrank"
# positive control: a REAL shrink on the connector arm is logged with the reason
mk_transcript "$TC" 5 rot
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TC" "$WC" sess-con PreCompact)"
wait_log "connector .* reason=shrank" 30 || fail "a real connector shrink was not logged with reason=shrank"
assert_file_contains "$LOGF" "reason=shrank — restarting from 0 ev=PreCompact sid=sess-con" "connector shrank line carries reason=shrank ev= sid="

# ---------------------------------------------------- 2. trailing partial line
new_home offhalf
stub_reset
cfg_rest
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"
W2="$HOME/work/halfproj"
mkdir -p "$W2"
T2="$W2/t.jsonl"
mk_transcript "$T2" 10 whole             # 10 complete newline-terminated lines
COMPLETE=$(wc -c <"$T2")
printf '{"type":"user","half":' >>"$T2"   # a final line with NO trailing newline
step "SessionEnd HOLDS the trailing partial line (never sent unterminated)"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T2" "$W2" sess-half SessionEnd)"
wait_firing 0 60 || fail "SessionEnd firing never completed"
assert_eq "$COMPLETE" "$(read_offset)" "offset stops at the last newline, half line held"
stub_concat '.transport=="rest"' '.transcript' >"$HOME/got"
if grep -q '"half":' "$HOME/got"; then
  fail "the unterminated half line was sent"
else
  pass "the half line was NOT sent"
fi
assert_file_contains "$HOOKS/state/sess-half.offset" "$COMPLETE" "offset file kept at the last newline"

# ---------------------------------------------------------- 3. shrank reason
new_home offshrank
stub_reset
cfg_rest
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"
W3="$HOME/work/shrproj"
mkdir -p "$W3"
T3="$W3/t.jsonl"
# The stale offset is planted BEFORE the transcript is written: a transcript last
# modified strictly before its checkpoint's last write is a FROZEN copy (`behind`,
# refused -- wave C.2 review, KB 49064), so the old order flaked into a refusal
# whenever a second boundary fell between the two writes. Written after it, the
# transcript is a genuine rewrite (mtime >= the checkpoint's) on every run.
printf '999999' >"$HOOKS/state/sess-shr.offset"   # a stale offset past EOF
mk_transcript "$T3" 40 big
step "0 < SIZE < offset restarts from 0 with reason=shrank"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T3" "$W3" sess-shr PreCompact)"
wait_firing 0 60 || fail "shrank drain never completed"
assert_file_contains "$LOGF" "reason=shrank" "the shrank restart is logged with reason=shrank"
assert_eq "$(wc -c <"$T3")" "$(read_offset)" "it re-drained from 0 to the new EOF"

step "the shrank 0 LANDS ON DISK even when nothing is sent afterwards (W3 replaces)"
# Shrink to a single UNTERMINATED line: the shrank path writes 0, then the loop
# holds the half line and sends nothing — so only the W3 write itself can put
# the 0 on disk. A noclobber write there would leave the stale value.
printf '{"type":"user","half":' >"$T3"
BEFORE=$(stub_total)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T3" "$W3" sess-shr PreCompact)"
wait_log "reason=shrank.*sid=sess-shr" 30 || true
settle
settle
assert_eq "$BEFORE" "$(stub_total)" "nothing was sent (the only line is unterminated)"
assert_eq "0" "$(cat "$HOOKS/state/sess-shr.offset")" "the offset file reads 0 after the shrank restart"
assert_file_missing "$HOOKS/state/sess-shr.offset.t" "no .offset.t temp file left behind"
assert_eq "2" "$(log_count 'reason=shrank')" "the second shrink logged reason=shrank once more (2 total)"
# and it does NOT log shrank again on the next firing (the 0 was persisted)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T3" "$W3" sess-shr PreCompact)"
settle
settle
assert_eq "2" "$(log_count 'reason=shrank')" "a persisted 0 is not re-logged as shrank on the next firing"

# ------------------------------------------------- 4. SessionEnd keeps the offset
new_home offkeep
stub_reset
cfg_rest
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"
W4="$HOME/work/keepproj"
mkdir -p "$W4"
T4="$W4/t.jsonl"
mk_transcript "$T4" 25 keep
S4=$(wc -c <"$T4")
step "a fully-drained SessionEnd KEEPS its offset file at SIZE (never deleted)"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T4" "$W4" sess-keep SessionEnd)"
wait_log "flush .* ok bytes=" 60 || fail "SessionEnd did not drain"
assert_file_contains "$HOOKS/state/sess-keep.offset" "$S4" "offset survives a drained SessionEnd and equals SIZE"
assert_file_contains "$LOGF" "ok bytes=$S4 (offset $S4/$S4) dup=0 ev=SessionEnd sid=sess-kee" "ok line keeps its shape and gains dup= ev= sid="
# a resume of the same session with 3 more lines sends ONLY those (the checkpoint was kept)
stub_reset
mk_transcript "$HOME/more.jsonl" 3 more
cat "$HOME/more.jsonl" >>"$T4"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T4" "$W4" sess-keep PreCompact)"
wait_stub 1 30 || fail "the resumed delta never arrived"
assert_eq "$(wc -c <"$HOME/more.jsonl")" "$(stub_field '.transport=="rest"' '.transcript_bytes' | tail -1)" "a resume after SessionEnd sends only the delta"

# ---------------------------------------------------- 5. state sweep, two roots
new_home offsweep
stub_reset
cfg_rest
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"
export CLAUDE_CONFIG_DIR="$HOME/altconfig"
mkdir -p "$HOME/.claude/projects/projA" "$CLAUDE_CONFIG_DIR/projects/projB"
ST="$HOOKS/state"
old_offset() { printf '100' >"$ST/$1.offset"; touch -d '45 days ago' "$ST/$1.offset" 2>/dev/null || touch -t 202001010000 "$ST/$1.offset"; }
# a 45-day-old offset whose transcript is present under root 1 -> survives
old_offset keepA; printf '{}\n' >"$HOME/.claude/projects/projA/keepA.jsonl"
# ... present under root 2 (CLAUDE_CONFIG_DIR) -> survives
old_offset keepB; printf '{}\n' >"$CLAUDE_CONFIG_DIR/projects/projB/keepB.jsonl"
# ... gone under BOTH, with a .retry -> retired + loss note, whole state set gone
old_offset goneC; printf 'x' >"$ST/goneC.retry"; printf 'x' >"$ST/goneC.project"; printf 'x' >"$ST/goneC.unseeded"; printf 'x' >"$ST/goneC.gate"
# ... gone under both but YOUNG (written now) -> survives (mtime > 30 d is required)
printf '100' >"$ST/youngD.offset"
rm -f "$ST/.state-sweep.last"
W5="$HOME/work/sweepproj"
mkdir -p "$W5"
T5="$W5/t.jsonl"
mk_transcript "$T5" 12 sweeptrigger
step "a real drain runs the state sweep after releasing its own lock"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T5" "$W5" sess-sweeptrigger PreCompact)"
wait_firing 0 60 || fail "the sweeping drain never completed"
wait_log "state sweep: retired sid=goneC" 20 || note "sweep line not yet seen"
if [ -f "$ST/keepA.offset" ]; then pass "45-day offset with transcript under root1 SURVIVES"; else fail "keepA was wrongly retired"; fi
if [ -f "$ST/keepB.offset" ]; then pass "45-day offset with transcript under root2 survives"; else fail "keepB wrongly retired"; fi
if [ -f "$ST/youngD.offset" ]; then pass "a young offset survives even with no transcript"; else fail "youngD (young) was wrongly retired"; fi
if [ -f "$ST/goneC.offset" ]; then fail "goneC (gone under both) was NOT retired"; else pass "goneC retired (transcript gone under both roots)"; fi
assert_file_missing "$ST/goneC.retry" "goneC.retry retired with it"
assert_file_missing "$ST/goneC.project" "goneC.project retired with it"
assert_file_missing "$ST/goneC.unseeded" "goneC.unseeded retired with it"
assert_file_missing "$ST/goneC.gate" "goneC.gate retired with it"
assert_file_contains "$LOGF" "retired sid=goneC transcript gone (retry marker present" "loss note when a .retry was pending"
if [ -f "$ST/.state-sweep.last" ]; then pass ".state-sweep.last stamped (once per 24 h)"; else fail "no .state-sweep.last stamp"; fi
# the firing session's OWN state is untouched by its sweep
assert_file_contains "$ST/sess-sweeptrigger.offset" "$(wc -c <"$T5")" "the sweeping session's own offset is intact"

# ---------------------------------------------------- 6. unseeded refusal + allow
new_home offunseeded
stub_reset
cfg_rest
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
W6="$HOME/work/unseededproj"
mkdir -p "$W6"
T6="$W6/t.jsonl"
# A DATED first record OLDER than a stamp that is present -> pre-stamp -> refuse.
"$RIG_PY" - "$T6" <<'EOF'
import json, sys
with open(sys.argv[1], "w") as fh:
    fh.write(json.dumps({"type": "user", "timestamp": "2020-01-01T00:00:00Z", "text": "old"}) + "\n")
    for i in range(20):
        fh.write(json.dumps({"type": "user", "i": i, "text": "body %d" % i}) + "\n")
EOF
printf '2030-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"
step "an unseeded pre-stamp transcript is REFUSED (no requests, marker written)"
BEFORE=$(stub_total)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T6" "$W6" sess-uns PreCompact)"
assert_eq "0" "$RC" "hook exits 0 on refusal"
settle
settle
assert_eq "$BEFORE" "$(stub_total)" "refused: nothing was sent"
assert_file_missing "$HOOKS/state/sess-uns.offset" "refused: no offset file created"
if [ -f "$HOOKS/state/sess-uns.unseeded" ]; then pass "the <okey>.unseeded marker was written"; else fail "no unseeded marker (sess-uns.unseeded)"; fi
assert_file_contains "$LOGF" "unseeded: refusing to send" "the refusal line was logged"
assert_file_contains "$LOGF" "--seed sess-uns current|" "the refusal names the --seed procedure"
# rate limit: a second refusal within 24 h logs NOTHING new
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T6" "$W6" sess-uns PreCompact)"
settle
settle
assert_eq "1" "$(log_count 'unseeded: refusing')" "the refusal line is rate-limited to one per 24 h"

step "KB_FLUSH_ALLOW_UNSEEDED=1 sends the same transcript from 0 and clears the marker"
stub_reset
S6=$(wc -c <"$T6")
KB_FLUSH_ALLOW_UNSEEDED=1 fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T6" "$W6" sess-uns PreCompact)"
wait_firing 0 60 || fail "the ALLOW override never drained"
assert_eq "$S6" "$(cat "$HOOKS/state/sess-uns.offset")" "override drained from 0 to EOF"
assert_file_missing "$HOOKS/state/sess-uns.unseeded" "the unseeded marker was cleared once seeded"

# -------------------------------------- 7. NO stamp on the box: hook creates one
new_home offnostamp
stub_reset
cfg_rest
install_hooks >/dev/null
W7="$HOME/work/nostampproj"
mkdir -p "$W7"
T7="$W7/old.jsonl"
cp "$T6" "$T7"                      # dated 2020, i.e. before any stamp written now
step "no stamp + a DATED pre-existing transcript -> refused (stamp created as now)"
# install.sh (C9) writes the trust stamp at install; this step is the HAND-COPIED-hook box
# that never ran it, so remove the stamp the installer just wrote.
rm -f "$HOOKS/state/.checkpoints-trusted-since"
assert_file_missing "$HOOKS/state/.checkpoints-trusted-since" "precondition: no stamp on this box"
BEFORE=$(stub_total)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T7" "$W7" sess-old PreCompact)"
settle
settle
assert_eq "$BEFORE" "$(stub_total)" "a dated old transcript on an unstamped box sends nothing"
assert_file_missing "$HOOKS/state/sess-old.offset" "no offset file created"
assert_file_contains "$LOGF" "unseeded: refusing to send" "refused as unseeded"
if [ -s "$HOOKS/state/.checkpoints-trusted-since" ]; then pass "the hook created .checkpoints-trusted-since"; else fail "no stamp was created"; fi
case "$(cat "$HOOKS/state/.checkpoints-trusted-since")" in
  20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z) pass "the stamp is ISO-8601 UTC (…Z)" ;;
  *) fail "stamp is not ISO UTC: [$(cat "$HOOKS/state/.checkpoints-trusted-since")]" ;;
esac
step "no stamp (fresh box) + an UNDATED synthetic transcript -> proceeds"
new_home offnostamp2
stub_reset
cfg_rest
install_hooks >/dev/null
W8="$HOME/work/undatedproj"
mkdir -p "$W8"
T8="$W8/u.jsonl"
mk_transcript "$T8" 15 undated       # mk_transcript writes no timestamp field
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T8" "$W8" sess-und PreCompact)"
wait_firing 0 60 || fail "the undated transcript never drained"
assert_eq "$(wc -c <"$T8")" "$(cat "$HOOKS/state/sess-und.offset")" "undated -> sent from 0 (the stamp cannot date it)"
step "a DATED transcript NEWER than the (now-created) stamp proceeds"
T9="$W8/new.jsonl"
"$RIG_PY" - "$T9" <<'EOF'
import json, sys
with open(sys.argv[1], "w") as fh:
    fh.write(json.dumps({"type": "user", "timestamp": "2099-01-01T00:00:00Z", "text": "new"}) + "\n")
    fh.write(json.dumps({"type": "user", "text": "body"}) + "\n")
EOF
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T9" "$W8" sess-new PreCompact)"
wait_firing 1 60 || fail "the post-stamp transcript never drained"
assert_eq "$(wc -c <"$T9")" "$(cat "$HOOKS/state/sess-new.offset")" "post-stamp -> sent from 0"

# ------------------------------------------- 8. corrupt / empty offset -> UNSEEDED
# PIN (wave C stage 3 A1, KB 33783): a non-numeric or empty checkpoint is NEVER a
# licence to resend from 0 -- the old "offset unreadable — restarting from 0"
# path is gone. It takes the SAME unseeded refusal as a pre-stamp transcript
# (same <okey>.unseeded marker, same one-line-per-24 h limit) with the reason
# `offset unreadable` and the --seed hint; the offset file is never written on
# that path, the Stop gate stays closed, and only the owner's --seed (or a valid
# offset) makes the session flow.
new_home offcorrupt
stub_reset
cfg_rest
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"
W9="$HOME/work/corruptproj"
mkdir -p "$W9"
# Under the projects dir, so `kb-flush-backfill.sh --seed <sid>` can find it by sid.
PD9="$HOME/.claude/projects/$(printf '%s' "$W9" | tr '/' '-')"
mkdir -p "$PD9"
T10="$PD9/sess-corrupt.jsonl"
mk_transcript "$T10" 25 corrupt
S10=$(wc -c <"$T10")
step "a NON-NUMERIC offset file is refused as unseeded (offset unreadable), never restarted from 0"
printf 'not-a-number\n' >"$HOOKS/state/sess-corrupt.offset"
BEFORE=$(stub_total)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T10" "$W9" sess-corrupt PreCompact)"
assert_eq "0" "$RC" "hook exits 0"
settle
settle
assert_eq "$BEFORE" "$(stub_total)" "0 stub requests: nothing was resent from 0"
assert_eq "not-a-number" "$(tr -d '\n' <"$HOOKS/state/sess-corrupt.offset")" "the offset file is UNCHANGED (never written on this path)"
if [ -f "$HOOKS/state/sess-corrupt.unseeded" ]; then pass "the <okey>.unseeded marker was written"; else fail "no unseeded marker (sess-corrupt.unseeded)"; fi
assert_eq "1" "$(log_count 'unseeded: refusing')" "exactly one unseeded line"
assert_file_contains "$LOGF" "unseeded: refusing to send $S10 from 0 (offset unreadable: " "the line names the reason: offset unreadable"
assert_file_contains "$LOGF" "--seed sess-corrupt current|$S10 ev=PreCompact sid=sess-cor" "...and the --seed hint, ev= and sid="
assert_not_contains "$(cat "$LOGF")" "restarting from 0" "no 'restarting from 0' for a corrupt offset (the old path is gone)"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T10" "$W9" sess-corrupt PreCompact)"
settle
settle
assert_eq "1" "$(log_count 'unseeded: refusing')" "the refusal is rate-limited by the same marker (one per 24 h)"
assert_eq "$BEFORE" "$(stub_total)" "still nothing sent"
step "an EMPTY offset file takes the same refusal"
: >"$HOOKS/state/sess-corrupt.offset"
rm -f "$HOOKS/state/sess-corrupt.unseeded" # lift the 24 h rate limit only
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T10" "$W9" sess-corrupt PreCompact)"
settle
settle
assert_eq "$BEFORE" "$(stub_total)" "empty offset: nothing sent"
assert_empty_file "$HOOKS/state/sess-corrupt.offset" "the empty offset file stays empty (not written)"
assert_eq "2" "$(log_count 'offset unreadable')" "logged once more (the marker had been lifted)"
if [ -f "$HOOKS/state/sess-corrupt.unseeded" ]; then pass "the marker is back"; else fail "no unseeded marker after the empty-offset refusal"; fi
step "Stop stays CLOSED on a corrupt+unseeded session (no .gate, no request)"
KB_FLUSH_STOP_MIN_BYTES=1 KB_FLUSH_STOP_MIN_INTERVAL=0 KB_SWEEP_INTERVAL=0 fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T10" "$W9" sess-corrupt Stop)"
assert_eq "0" "$RC" "Stop exits 0"
settle
assert_file_missing "$HOOKS/state/sess-corrupt.gate" "no .gate stamped: the marker closes the gate"
assert_eq "$BEFORE" "$(stub_total)" "0 requests on Stop"
step "--seed via the backfill makes it flow"
"$HOOKS/kb-flush-backfill.sh" --seed sess-corrupt 0 >"$HOME/seed.out" 2>&1
SEEDRC=$?
assert_eq "0" "$SEEDRC" "--seed exits 0 on a corrupt checkpoint"
assert_eq "seeded sess-cor offset=0 (snapped from 0)" "$(cat "$HOME/seed.out")" "--seed wrote a numeric checkpoint"
assert_eq "0" "$(cat "$HOOKS/state/sess-corrupt.offset")" "the offset file now reads 0"
assert_file_missing "$HOOKS/state/sess-corrupt.unseeded" "--seed cleared the unseeded marker"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T10" "$W9" sess-corrupt PreCompact)"
wait_firing 0 60 || fail "the seeded session never drained: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "$S10" "$(cat "$HOOKS/state/sess-corrupt.offset")" "seeded at 0 -> drained to EOF"
assert_eq "1" "$(($(stub_total) - BEFORE))" "exactly one request: the transcript left ONCE, after the owner's seed"
assert_eq "2" "$(log_count 'unseeded: refusing')" "no further refusal once seeded"

# ------------------------------------------- 9. the CONNECTOR arm: same refusal
# PIN (wave C stage 3 A1, KB 33783 -- arm-agnostic): `a resend from 0 is never
# automatic` holds for the usage-metered CONNECTOR arm too. A corrupt/empty
# checkpoint there used to be silently coerced to 0 and the WHOLE transcript
# re-relayed through `claude -p` (one model call per 24 KB). It now takes the
# shared refusal: 0 relay calls, the file untouched, one `unseeded: refusing
# ... (offset unreadable: ...)` line with the --seed hint, the .unseeded marker;
# a numeric checkpoint (the owner's --seed) relays only the delta and clears
# the marker.
new_home offcorruptconn
fake_claude_relay
cfg_connector
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
W10="$HOME/work/connproj"
mkdir -p "$W10"
T11="$W10/conn.jsonl"
mk_transcript "$T11" 25 connc
S11=$(wc -c <"$T11")
step "CONNECTOR: a NON-NUMERIC offset file is refused as unseeded, nothing is relayed, the file is untouched"
printf 'not-a-number\n' >"$HOOKS/state/conn-corrupt.offset"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T11" "$W10" conn-corrupt PreCompact)"
assert_eq "0" "$RC" "hook exits 0"
settle
settle
assert_eq "0" "$(relay_count)" "0 relay calls: nothing was re-relayed from 0"
assert_eq "not-a-number" "$(tr -d '\n' <"$HOOKS/state/conn-corrupt.offset")" "the offset file is UNCHANGED"
if [ -f "$HOOKS/state/conn-corrupt.unseeded" ]; then pass "the <okey>.unseeded marker was written"; else fail "no unseeded marker (conn-corrupt.unseeded)"; fi
assert_eq "1" "$(log_count 'unseeded: refusing')" "exactly one unseeded line"
assert_file_contains "$LOGF" "unseeded: refusing to send $S11 from 0 (offset unreadable: " "the line names the reason: offset unreadable"
assert_file_contains "$LOGF" "--seed conn-corrupt current|$S11 ev=PreCompact sid=conn-cor" "...and the --seed hint, ev= and sid="
assert_not_contains "$(cat "$LOGF")" "connector connproj ok bytes=" "no relay ok line"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T11" "$W10" conn-corrupt PreCompact)"
settle
settle
assert_eq "1" "$(log_count 'unseeded: refusing')" "rate-limited by the same marker (one per 24 h)"
assert_eq "0" "$(relay_count)" "still nothing relayed"
step "CONNECTOR: a numeric checkpoint relays only the delta and clears the marker"
HALF=$(head -n 12 "$T11" | wc -c | tr -d ' ')
printf '%s' "$HALF" >"$HOOKS/state/conn-corrupt.offset"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T11" "$W10" conn-corrupt PreCompact)"
wait_relay 1 30 || fail "the seeded connector session never relayed: $(tail -3 "$LOGF" | tr '\n' '|')"
wait_log "connector connproj ok bytes=$((S11 - HALF)) " 30 || fail "no ok line for the delta: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "$S11" "$(cat "$HOOKS/state/conn-corrupt.offset")" "drained from the numeric checkpoint to EOF"
assert_eq "$((S11 - HALF))" "$(relay_concat | wc -c | tr -d ' ')" "exactly the delta was relayed (nothing from 0)"
assert_file_missing "$HOOKS/state/conn-corrupt.unseeded" "a numeric checkpoint clears the stale unseeded marker"
assert_eq "1" "$(log_count 'unseeded: refusing')" "no further refusal"

finish

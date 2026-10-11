#!/usr/bin/env bash
# CASE: A MARKED COPY IS REFUSED AT ANY SIZE -- a copy named by
#       state/<okey>.behind or .diverged is never drained from the shared
#       checkpoint, even after it grows PAST it, and a behind copy's marker
#       keeps the size it was frozen at, so the owner's seed is exact (wave C.2
#       review 2, must-fix 1, KB 49064).
# PINS: flush-session-to-kb.sh kb_decide_offset / the connector arm (a path a marker names at SIZE >= OFFSET -> kb_refuse_marked; a .diverged path carrying the checkpoint holder's prefix -> kb_marker_holds releases it), the SESSION COPIES block's kb_marker_write / kb_marker_frozen (frozen=) / kb_window_matches, kb-flush-backfill.sh's marked rows (bf_marker_holds) and --seed
#   The checkpoint is the SESSION's (state/<sid>.offset), so once a marked copy
#   grows to it or past it, that checkpoint is ANOTHER copy's: a drain from it
#   starts mid-record and loses [frozen, checkpoint) for good (about 3.4 MB on
#   rm1's geometry). The marker's frozen= field (LAST, so ` <path> size=<n>
#   checkpoint=<m>` substrings still match) is the path's size when a marker
#   FIRST named it; every rewrite naming the same path carries it.
#   1. BEHIND, RESUMED BELOW THE CHECKPOINT. The live copy flushed and was then
#      REMOVED; the frozen byte-prefix copy A (F bytes, mtime before the
#      checkpoint's last write) is marked by `--auto`: `size=F checkpoint=CP
#      frozen=F`. A is then resumed (records appended, still below CP) and
#      PreCompact fires: exit 0, empty stdout, ZERO requests, the checkpoint
#      byte-identical; the marker reads `size=<grown> checkpoint=CP frozen=F`;
#      the hook's ONE `reason=behind` line says `resumed since it was marked:
#      its records past F were NOT sent -- kb-flush-backfill.sh --seed <sid> F
#      sends exactly them` and never offers `current`. The dry-run row is
#      `behind` with REMAIN = grown - F and the same seed (never `current (its
#      bytes were sent`); `--auto` fires nothing and `.sweep-last` carries
#      `behind=1 unflushed=<grown - F>`.
#   2. BEHIND, GROWN PAST THE CHECKPOINT. A appended past CP. Stop (gate opened:
#      KB_FLUSH_STOP_MIN_BYTES=1, KB_FLUSH_STOP_MIN_INTERVAL=0), PreCompact and
#      SessionEnd each exit 0 with empty stdout, send ZERO requests and leave
#      the checkpoint byte-identical; the marker is rewritten on every firing
#      (PreCompact runs inside the 24 h stamp: silent, the rewrite is its only
#      trace) and keeps frozen=F; the Stop and SessionEnd lines (stamp aged
#      past 24 h first) say `has grown to the checkpoint CP or past it ...
#      another copy's (draining from it would start mid-record) reason=behind
#      -- refused, nothing sent, no restart` and name `--seed <sid> F`. The
#      dry-run row is `behind`, REMAIN = size - F, reason `has grown to
#      checkpoint CP or past it`; `--auto` (quiet window satisfied) fires
#      nothing, `.sweep-last` `behind=1 unflushed=<size - F>`.
#   3. THE EXACT SEED. `--seed <sid> F` prints `seeded <sid8> offset=F (snapped
#      from F)` and `cleared: state/<okey>.behind (it named A)`; the marker is
#      gone. The next SessionEnd sends EXACTLY A[F:size]: the first chunk's
#      flush_offset is F, byte F-1 of A is a newline (a record boundary), the
#      bytes received equal A's tail from F byte for byte, the checkpoint lands
#      at A's size and the table reads `flushed`.
#   4. DIVERGED, GROWN PAST THE CHECKPOINT WHILE THE LIVE COPY EXISTS. Copy A
#      resumed in the original dir (tail differs) is marked diverged by a
#      SessionEnd (cover= the live copy), then grows past CP. PreCompact and
#      SessionEnd: ZERO requests, the checkpoint byte-identical, never
#      `restarting from 0`; the marker still names A (no cover= at SIZE >= CP,
#      frozen= the size it was first marked at); the PreCompact line (stamp
#      aged) says `has grown to the checkpoint CP or past it ...
#      reason=stale-copy-diverged -- refused, nothing sent, no restart`. The
#      dry-run row is `diverged`, REMAIN = A's whole size, the reason names
#      moving the copy out (the live copy still reaches CP) and offers no
#      --seed; `--auto` fires nothing, `.sweep-last` `diverged=1
#      unflushed=<A's size>`.
#   5. CONNECTOR ARM. Leg 1+2's geometry on a CONNECTOR box (recording relay):
#      the hook marks the frozen copy behind (frozen=F), A grows past CP, and a
#      PreCompact relays NOTHING, leaves the checkpoint byte-identical, keeps
#      frozen=F and logs the `has grown to the checkpoint` line.
#   6. RELEASED (review 3, must 1). A covered diverged copy A is marked, then
#      moved out of ~/.claude/projects (the remedy) and the session resumed from
#      A's directory: A's path now holds the LIVE transcript plus new records,
#      past CP. A dry run tables it `ready` (REMAIN = size - CP, no diverged
#      count) and leaves the marker untouched; Stop (gate open) removes the
#      marker and its stamp with ONE `marker cleared: state/<okey>.diverged
#      named A, which now carries the checkpoint holder's prefix ev=Stop` line
#      and sends ONE request, flush_offset CP, bytes exactly A[CP:size]; the
#      checkpoint lands at A's size, the table reads A `flushed` and the old
#      live copy `stale-copy`. RED on 1ddef59 (the review-2 tree): the dry row
#      is `diverged` with REMAIN = the whole file, Stop sends nothing and keeps
#      the marker (frozen= carried), no clear line.
# FIXTURES: as case 46 (the session started in $HOME/work/a-home, resumed in
#   $HOME/work/binary-rewriter-project; every record names its session id;
#   every direct hook firing runs with KB_SWEEP_INTERVAL=0 for that firing
#   only). A 24 h log stamp is AGED (touch -d, 25 h back) where a leg needs the
#   firing's line -- the same as a day passing; markers and offsets are never
#   touched by hand.
# RED on f89f61c (the first review-fix round; KB_SKILL_DIR=<f89f61c's
#   contrib/claude/kb-session-flush>): no frozen= field, and a marked path at
#   SIZE >= OFFSET is drained from the shared checkpoint. Author's run OUTSIDE
#   docker (sandbox HOMEs, a local stub_server.py; the rig run is the owner's):
#   58 of 113 checks FAIL there, 0 on the fixed hooks (legs 1-5; leg 6 came
#   with review 3: on 1ddef59 14 of its checks FAIL, legs 1-5 stay green).
#   1: every `frozen=` check, the hook line's `--seed <sid> F` (it offers
#      `current|<bytes>`), the row's REMAIN (`got [0]`) and reason (`current
#      (its bytes were sent`), `.sweep-last unflushed` (`got [0]`). GREEN there:
#      exit codes, zero requests, the checkpoint, the ONE line.
#   2: from the Stop firing on, every check but the exit codes and the empty
#      stdout: each firing sends 1 request (Stop moves the checkpoint 59880 ->
#      A's size, a summary line instead of a refusal), the marker is neither
#      rewritten nor carries frozen=, the table reads `flushed`, `--auto` fires
#      it, `.sweep-last behind=0`.
#   3: GREEN there -- a seed at F and the drain from it were already right;
#      the leg pins the exact remedy the new texts name, end to end.
#   4: the setup marker (no frozen=), the PreCompact past CP drains A (the
#      checkpoint moves to A's size), the line, the silent rewrite, the table
#      (A `flushed`, the live copy `diverged`), `--auto` fires it.
#   5: the frozen= marker, a 4th chunk relayed, the checkpoint moves, the line.
. /rig/lib.sh

slug_of() { printf '%s' "$1" | tr '/' '-'; }
mtime_of() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }
size_of() { wc -c <"$1" | tr -d ' '; }

setup_paths() {
  ST="$HOOKS/state"
  PROJDIR="$HOME/.claude/projects"
  BF="$HOOKS/kb-flush-backfill.sh"
  mkdir -p "$ST"
  printf '2000-01-01T00:00:00Z\n' >"$ST/.checkpoints-trusted-since"
  ORIG="$HOME/work/a-home"                     # where the session STARTED (its dir sorts first)
  RESUMED="$HOME/work/binary-rewriter-project" # where it was RESUMED
  mkdir -p "$ORIG" "$RESUMED"
  STALE_DIR="$PROJDIR/$(slug_of "$ORIG")"
  LIVE_DIR="$PROJDIR/$(slug_of "$RESUMED")"
}
setup_home() { # <name> -- a clean-room REST box with a past trust stamp and the two cwds
  new_home "$1"
  stub_reset
  cfg_rest
  install_hooks >/dev/null
  setup_paths
}

mk_lines() { # <path> <n> <cwd> <sid> [w|a] -- real-shaped records naming their session
  mkdir -p "$(dirname "$1")"
  "$RIG_PY" - "$1" "$2" "$3" "$4" "${5:-w}" <<'EOF'
import json, sys
path, n, cwd, sid, mode = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
with open(path, mode, encoding="utf-8") as fh:
    for i in range(n):
        fh.write(json.dumps({"type": "user", "timestamp": "2026-10-08T10:00:00.000Z", "cwd": cwd,
                             "i": i, "text": "%s %s line %d %s" % (sid, mode, i, "y" * 200)}) + "\n")
EOF
}
mk_resumed_session() { # <sid> -> LIVE STALE LIVE_SIZE STALE_SIZE
  LIVE="$LIVE_DIR/$1.jsonl"
  STALE="$STALE_DIR/$1.jsonl"
  mk_lines "$LIVE" 100 "$ORIG" "$1"
  STALE_SIZE=$(size_of "$LIVE")
  mk_lines "$LIVE" 30 "$RESUMED" "$1" a
  LIVE_SIZE=$(size_of "$LIVE")
}

fire_flush() { # <transcript> <cwd> <sid> <event> -- piggyback sweep OFF for this firing only
  KB_SWEEP_INTERVAL=0 fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$1" "$2" "$3" "$4")"
}
summaries() { log_count ' (ok|partial) bytes='; }
age_stamp() { touch -d @"$(($(date +%s) - 90000))" "$1"; } # 25 h back: the next line is due

# Wait for a firing that must LOG a refusal: a new <pattern> line -- or, on a
# hook that sends instead, its summary line (every request then counted).
await_line() { # <pattern> <lines before> <summaries before>
  local i=0
  while [ "$i" -lt 120 ]; do
    [ "$(log_count "$1")" -gt "$2" ] && break
    [ "$(summaries)" -gt "$3" ] && break
    i=$((i + 1))
    sleep 0.25
  done
  [ "$i" -lt 120 ] || note "neither a new [$1] line nor a summary line within 30 s"
  settle
}
# Wait for a SILENT refusal (the 24 h stamp is fresh): its only trace is the
# marker's rewrite. The caller sleeps past a second boundary first, so the
# rewrite's timestamp differs; a hook that sends ends the wait with its summary.
await_rewrite() { # <marker> <its line before> <summaries before>
  local i=0
  while [ "$i" -lt 120 ]; do
    [ "$(head -n1 "$1" 2>/dev/null)" != "$2" ] && break
    [ "$(summaries)" -gt "$3" ] && break
    i=$((i + 1))
    sleep 0.25
  done
  [ "$i" -lt 120 ] || note "neither a rewrite of $1 nor a summary line within 30 s"
  settle
}

same_offset() { # <saved copy> <offset file> <label>
  if cmp -s "$1" "$2"; then
    pass "$3"
  else
    fail "$3 -- the shared checkpoint moved: $(cat "$1" 2>/dev/null) -> $(cat "$2" 2>/dev/null)"
  fi
}
last_line() { grep -E "$1" "$LOGF" 2>/dev/null | tail -n1; }
run_bf() {
  OUT=$("$BF" "$@" 2>&1)
  RC=$?
  printf '%s\n' "$OUT" >"$HOME/bf.out"
}
sweep_last() { sed -n "s/.* $1=\([0-9]*\).*/\1/p" "$ST/.sweep-last" 2>/dev/null | head -1; }
quiet_auto() { # --auto with the quiet window satisfied (both copies long idle), as case 33
  for _q in "$@"; do [ -e "$_q" ] && touch -d @1700000000 "$_q"; done
  pin_sweep_clock "$(date +%s)"
  run_bf --auto
  unpin_sweep_clock
}

# ================================== 1. BEHIND, resumed below the checkpoint
step "1. setup: the live copy flushes and is REMOVED; --auto marks the frozen copy A behind with frozen=F"
setup_home c2rfrozen
SID_B="b0fe2047-0000-4000-8000-000000000047"
B8=${SID_B:0:8}
mk_resumed_session "$SID_B"
LIVE_B=$LIVE A_B=$STALE CP=$LIVE_SIZE F=$STALE_SIZE
fire_flush "$LIVE_B" "$RESUMED" "$SID_B" SessionEnd
wait_firing 0 60 || fail "setup: the live copy never flushed: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "$CP" "$(cat "$ST/$SID_B.offset" 2>/dev/null)" "setup: the shared checkpoint sits at the live copy's size"
SUM_SETUP=$(summaries)
mk_prefix_copy "$LIVE_B" "$A_B" "$F"
rm -f "$LIVE_B"
touch -d @"$(($(mtime_of "$ST/$SID_B.offset") - 3600))" "$A_B" # FROZEN: older than the checkpoint's last write
cp "$ST/$SID_B.offset" "$HOME/offB.before"
stub_reset
pin_sweep_clock "$(date +%s)"
run_bf --auto
unpin_sweep_clock
assert_eq "0" "$RC" "--auto exits 0"
assert_file_contains "$ST/$SID_B.behind" " $A_B size=$F checkpoint=$CP frozen=$F" \
  "--auto wrote state/<okey>.behind naming A with frozen=F ($F)"
assert_eq "0" "$(stub_total)" "--auto sent nothing"

step "1. A resumed BELOW the checkpoint, PreCompact: refused; the marker keeps frozen=F; the line names --seed <sid> F"
mk_lines "$A_B" 5 "$ORIG" "$SID_B" a
G1=$(size_of "$A_B")
if [ "$G1" -gt "$F" ] && [ "$G1" -lt "$CP" ]; then
  pass "fixture: A grew past F and is still behind the checkpoint ($F < $G1 < $CP)"
else
  fail "fixture: A is $G1 bytes -- not between F=$F and the checkpoint $CP"
fi
N0=$(summaries)
L0=$(log_count "reason=behind .*sid=$B8\$")
fire_flush "$A_B" "$ORIG" "$SID_B" PreCompact
assert_eq "0" "$RC" "the hook exits 0"
assert_eq "" "$OUT" "...and prints nothing"
await_line "reason=behind .*sid=$B8\$" "$L0" "$N0"
assert_eq "0" "$(stub_total)" "ZERO requests"
same_offset "$HOME/offB.before" "$ST/$SID_B.offset" "the shared checkpoint is byte-identical ($CP)"
assert_eq "0" "$(log_count "restarting from 0")" "never 'restarting from 0'"
assert_file_contains "$ST/$SID_B.behind" " $A_B size=$G1 checkpoint=$CP frozen=$F" "the marker reads size=<grown> and KEEPS frozen=F"
assert_eq "1" "$(log_count "reason=behind .*sid=$B8\$")" "ONE reason=behind line"
L=$(last_line "reason=behind .*sid=$B8\$")
note "hook line: ${L#* flush }"
assert_contains "$L" "refused, nothing sent, no restart" "...refused, nothing sent, no restart"
assert_contains "$L" "resumed since it was marked: its records past $F were NOT sent -- kb-flush-backfill.sh --seed $SID_B $F sends exactly them" \
  "...names the exact seed, --seed <sid> F"
assert_not_contains "$L" "current" "...and never offers 'current'"

step "1. SWEEP: the row is behind with REMAIN = grown - F, named in unflushed=; --auto fires nothing"
pin_sweep_clock "$(date +%s)"
run_bf
unpin_sweep_clock
assert_eq "behind" "$(sweep_row_state "$HOME/bf.out" "$SID_B" "$G1")" "A reads behind"
assert_eq "$((G1 - F))" "$(sweep_row_remain "$HOME/bf.out" "$SID_B" "$G1")" "...REMAIN = grown - F ($((G1 - F)): the records written after the resume)"
R=$(sweep_row_reason "$HOME/bf.out" "$SID_B" "$G1")
note "reason: $R"
assert_contains "$R" "kb-flush-backfill.sh --seed $SID_B $F sends exactly them" "...the reason names --seed <sid> F"
assert_not_contains "$R" "current (its bytes were sent" "...and never 'current (its bytes were sent'"
quiet_auto "$A_B"
assert_eq "0" "$RC" "--auto exits 0"
assert_eq "0" "$(stub_total)" "--auto fires nothing"
same_offset "$HOME/offB.before" "$ST/$SID_B.offset" "the checkpoint is byte-identical"
assert_eq "1" "$(sweep_last behind)" ".sweep-last behind=1"
assert_eq "$((G1 - F))" "$(sweep_last unflushed)" ".sweep-last unflushed=$((G1 - F)) (the resumed copy's unsent bytes)"
assert_file_contains "$ST/$SID_B.behind" " frozen=$F" "--auto's rewrite keeps frozen=F"

# ================================== 2. BEHIND, grown past the checkpoint
step "2. A grown PAST the checkpoint: Stop (gate open), PreCompact and SessionEnd send nothing"
mk_lines "$A_B" 40 "$ORIG" "$SID_B" a
G2=$(size_of "$A_B")
if [ "$G2" -gt "$CP" ]; then pass "fixture: A grew past the checkpoint ($G2 > $CP)"; else fail "fixture: A ($G2) did not pass the checkpoint $CP"; fi
age_stamp "$ST/$SID_B.behind.stamp"
N0=$(summaries)
L0=$(log_count "reason=behind .*sid=$B8\$")
KB_FLUSH_STOP_MIN_BYTES=1 KB_FLUSH_STOP_MIN_INTERVAL=0 fire_flush "$A_B" "$ORIG" "$SID_B" Stop
assert_eq "0" "$RC" "Stop exits 0"
assert_eq "" "$OUT" "...and prints nothing"
await_line "reason=behind .*sid=$B8\$" "$L0" "$N0"
assert_eq "0" "$(stub_total)" "Stop: ZERO requests (never drained from another copy's checkpoint)"
same_offset "$HOME/offB.before" "$ST/$SID_B.offset" "Stop: the checkpoint is byte-identical"
assert_file_contains "$ST/$SID_B.behind" " $A_B size=$G2 checkpoint=$CP frozen=$F" "Stop: the marker names A at its new size, frozen=F kept"
assert_eq "$((L0 + 1))" "$(log_count "reason=behind .*sid=$B8\$")" "Stop: one new reason=behind line (the stamp was aged)"
L=$(last_line "reason=behind .*sid=$B8\$")
note "hook line: ${L#* flush }"
assert_contains "$L" "stale copy: $A_B ($G2) has grown to the checkpoint $CP or past it; state/$SID_B.behind names this file and that checkpoint is another copy's (draining from it would start mid-record) reason=behind" \
  "...it says A has grown past the checkpoint, another copy's"
assert_contains "$L" "refused, nothing sent, no restart" "...refused, nothing sent, no restart"
assert_contains "$L" "kb-flush-backfill.sh --seed $SID_B $F sends exactly them" "...and names --seed <sid> F"
assert_not_contains "$L" "current" "...never 'current'"

MK=$(head -n1 "$ST/$SID_B.behind" 2>/dev/null)
L0=$(log_count "reason=behind .*sid=$B8\$")
N0=$(summaries)
sleep 1.1 # past a second boundary: the rewrite's timestamp differs
fire_flush "$A_B" "$ORIG" "$SID_B" PreCompact
assert_eq "0" "$RC" "PreCompact exits 0"
assert_eq "" "$OUT" "...and prints nothing"
await_rewrite "$ST/$SID_B.behind" "$MK" "$N0"
assert_ne "$MK" "$(head -n1 "$ST/$SID_B.behind" 2>/dev/null)" "PreCompact rewrote the marker (inside the 24 h stamp: its only trace)"
assert_file_contains "$ST/$SID_B.behind" " $A_B size=$G2 checkpoint=$CP frozen=$F" "...still naming A with frozen=F"
assert_eq "$L0" "$(log_count "reason=behind .*sid=$B8\$")" "...and logged no line (one per 24 h)"
assert_eq "0" "$(stub_total)" "PreCompact: ZERO requests"
same_offset "$HOME/offB.before" "$ST/$SID_B.offset" "PreCompact: the checkpoint is byte-identical"

age_stamp "$ST/$SID_B.behind.stamp"
N0=$(summaries)
fire_flush "$A_B" "$ORIG" "$SID_B" SessionEnd
assert_eq "0" "$RC" "SessionEnd exits 0"
assert_eq "" "$OUT" "...and prints nothing"
await_line "reason=behind .*sid=$B8\$" "$L0" "$N0"
assert_eq "0" "$(stub_total)" "SessionEnd: ZERO requests"
same_offset "$HOME/offB.before" "$ST/$SID_B.offset" "SessionEnd: the checkpoint is byte-identical"
assert_eq "$((L0 + 1))" "$(log_count "reason=behind .*sid=$B8\$")" "SessionEnd: one new reason=behind line"
assert_contains "$(last_line "reason=behind .*sid=$B8\$")" "has grown to the checkpoint $CP or past it" "...saying A has grown past the checkpoint"
assert_file_contains "$ST/$SID_B.behind" " $A_B size=$G2 checkpoint=$CP frozen=$F" "the marker still names A, frozen=F"
assert_eq "0" "$(log_count "restarting from 0")" "never 'restarting from 0'"
assert_eq "$SUM_SETUP" "$(summaries)" "no firing logged a send summary since the setup's"

step "2. SWEEP: A is tabled behind (REMAIN = size - F), never fired"
pin_sweep_clock "$(date +%s)"
run_bf
unpin_sweep_clock
assert_eq "behind" "$(sweep_row_state "$HOME/bf.out" "$SID_B" "$G2")" "A reads behind (not ready: a drain would start mid-record)"
assert_eq "$((G2 - F))" "$(sweep_row_remain "$HOME/bf.out" "$SID_B" "$G2")" "...REMAIN = size - F ($((G2 - F)))"
R=$(sweep_row_reason "$HOME/bf.out" "$SID_B" "$G2")
note "reason: $R"
assert_contains "$R" "has grown to checkpoint $CP or past it" "...the reason says it grew past the checkpoint"
assert_contains "$R" "kb-flush-backfill.sh --seed $SID_B $F sends exactly them" "...and names --seed <sid> F"
quiet_auto "$A_B"
assert_eq "0" "$RC" "--auto exits 0"
assert_eq "0" "$(stub_total)" "--auto fires nothing"
same_offset "$HOME/offB.before" "$ST/$SID_B.offset" "the checkpoint is byte-identical"
assert_eq "1" "$(sweep_last behind)" ".sweep-last behind=1"
assert_eq "$((G2 - F))" "$(sweep_last unflushed)" ".sweep-last unflushed=$((G2 - F))"
assert_file_contains "$ST/$SID_B.behind" " $A_B size=$G2 checkpoint=$CP frozen=$F" "--auto's rewrite keeps frozen=F"

# ================================== 3. the exact seed
step "3. --seed <sid> F, then SessionEnd: exactly A[F:size] is sent, from a record boundary"
run_bf --seed "$SID_B" "$F"
assert_eq "0" "$RC" "--seed exits 0"
note "seed: $(printf '%s' "$OUT" | tr '\n' '|')"
assert_contains "$OUT" "seeded $B8 offset=$F (snapped from $F)" "it seeds at F (a line end of A)"
assert_contains "$OUT" "cleared: state/$SID_B.behind (it named $A_B)" "it clears the behind marker naming A"
assert_not_contains "$OUT" "note:" "...with no note (a behind copy is settled by the seed)"
assert_file_missing "$ST/$SID_B.behind" "the marker is gone"
assert_eq "$F" "$(cat "$ST/$SID_B.offset" 2>/dev/null)" "the checkpoint reads F"
if [ "$(tail -c +"$F" "$A_B" | head -c 1 | od -An -c | tr -d ' ')" = '\n' ]; then
  pass "byte F-1 of A is a newline: F is a record boundary"
else
  fail "byte F-1 of A is not a newline: F is not a record boundary"
fi
stub_reset
N0=$(summaries)
fire_flush "$A_B" "$ORIG" "$SID_B" SessionEnd
assert_eq "0" "$RC" "SessionEnd exits 0"
wait_firing "$N0" 60 || fail "the SessionEnd drain never completed: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "$F" "$(stub_field ".session_uri==\"session:$SID_B\"" '.flush_offset' | head -n1)" "the first chunk's flush_offset is F"
stub_concat ".session_uri==\"session:$SID_B\"" '.transcript' >"$HOME/seed.got"
tail -c +"$((F + 1))" "$A_B" >"$HOME/seed.want"
if cmp -s "$HOME/seed.want" "$HOME/seed.got"; then
  pass "the bytes received are exactly A[F:size] ($((G2 - F)) bytes)"
else
  fail "the bytes received are exactly A[F:size] -- got $(size_of "$HOME/seed.got") of $((G2 - F)) bytes"
fi
assert_eq "$G2" "$(cat "$ST/$SID_B.offset" 2>/dev/null)" "the checkpoint lands at A's size"
assert_eq "0" "$(log_count "restarting from 0")" "never 'restarting from 0'"
run_bf
assert_eq "flushed" "$(sweep_row_state "$HOME/bf.out" "$SID_B" "$G2")" "the table reads flushed"

# ======================== 4. DIVERGED, grown past the checkpoint, live copy present
step "4. setup: A (resumed in the original dir, tail differs) is marked diverged by a SessionEnd"
setup_home c2rdiv
SID_D="d1e52047-0000-4000-8000-000000000047"
D8=${SID_D:0:8}
mk_resumed_session "$SID_D"
LIVE_D=$LIVE A_D=$STALE CP_D=$LIVE_SIZE PREFIX_D=$STALE_SIZE LIVE_SIZE_D=$LIVE_SIZE
fire_flush "$LIVE_D" "$RESUMED" "$SID_D" SessionEnd
wait_firing 0 60 || fail "setup: the live copy never flushed: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "$CP_D" "$(cat "$ST/$SID_D.offset" 2>/dev/null)" "setup: the checkpoint sits at the live copy's size"
mk_prefix_copy "$LIVE_D" "$A_D" "$PREFIX_D"
mk_lines "$A_D" 5 "$ORIG" "$SID_D" a
FD=$(size_of "$A_D")
cp "$ST/$SID_D.offset" "$HOME/offD.before"
stub_reset
N0=$(summaries)
fire_flush "$A_D" "$ORIG" "$SID_D" SessionEnd
await_line "reason=stale-copy-diverged .*sid=$D8\$" 0 "$N0"
assert_file_contains "$ST/$SID_D.diverged" " $A_D size=$FD checkpoint=$CP_D cover=$LIVE_D frozen=$FD" \
  "setup: state/<okey>.diverged names A (cover= the live copy, frozen= its size)"

step "4. A grown PAST the checkpoint while the live copy exists: PreCompact and SessionEnd send nothing"
mk_lines "$A_D" 40 "$ORIG" "$SID_D" a
GD=$(size_of "$A_D")
if [ "$GD" -gt "$CP_D" ] && [ -f "$LIVE_D" ]; then
  pass "fixture: A grew past the checkpoint ($GD > $CP_D) and the live copy is still there"
else
  fail "fixture: A ($GD) did not pass the checkpoint $CP_D, or the live copy is gone"
fi
age_stamp "$ST/$SID_D.diverged.stamp"
N0=$(summaries)
L0=$(log_count "reason=stale-copy-diverged .*sid=$D8\$")
fire_flush "$A_D" "$ORIG" "$SID_D" PreCompact
assert_eq "0" "$RC" "PreCompact exits 0"
assert_eq "" "$OUT" "...and prints nothing"
await_line "reason=stale-copy-diverged .*sid=$D8\$" "$L0" "$N0"
assert_eq "0" "$(stub_total)" "PreCompact: ZERO requests"
same_offset "$HOME/offD.before" "$ST/$SID_D.offset" "PreCompact: the checkpoint is byte-identical"
assert_eq "$((L0 + 1))" "$(log_count "reason=stale-copy-diverged .*sid=$D8\$")" "one new reason=stale-copy-diverged line (the stamp was aged)"
L=$(last_line "reason=stale-copy-diverged .*sid=$D8\$")
note "hook line: ${L#* flush }"
assert_contains "$L" "stale copy: $A_D ($GD) has grown to the checkpoint $CP_D or past it; state/$SID_D.diverged names this file and that checkpoint is another copy's (draining from it would start mid-record) reason=stale-copy-diverged" \
  "...it says A has grown past the checkpoint, another copy's"
assert_contains "$L" "refused, nothing sent, no restart" "...refused, nothing sent, no restart"
assert_file_contains "$ST/$SID_D.diverged" " $A_D size=$GD checkpoint=$CP_D frozen=$FD" "the marker names A at its new size, frozen= kept, no cover="
MK=$(head -n1 "$ST/$SID_D.diverged" 2>/dev/null)
N0=$(summaries)
sleep 1.1 # past a second boundary: the rewrite's timestamp differs
fire_flush "$A_D" "$ORIG" "$SID_D" SessionEnd
assert_eq "0" "$RC" "SessionEnd exits 0"
await_rewrite "$ST/$SID_D.diverged" "$MK" "$N0"
assert_ne "$MK" "$(head -n1 "$ST/$SID_D.diverged" 2>/dev/null)" "SessionEnd rewrote the marker (inside the 24 h stamp: silent)"
assert_file_contains "$ST/$SID_D.diverged" " $A_D size=$GD checkpoint=$CP_D frozen=$FD" "...still naming A, frozen= kept"
assert_eq "0" "$(stub_total)" "SessionEnd: ZERO requests"
same_offset "$HOME/offD.before" "$ST/$SID_D.offset" "SessionEnd: the checkpoint is byte-identical"
assert_eq "0" "$(log_count "restarting from 0")" "never 'restarting from 0'"

step "4. SWEEP: A is tabled diverged (REMAIN = its size), the reason names moving it out; --auto fires nothing"
pin_sweep_clock "$(date +%s)"
run_bf
unpin_sweep_clock
assert_eq "diverged" "$(sweep_row_state "$HOME/bf.out" "$SID_D" "$GD")" "A reads diverged"
assert_eq "$GD" "$(sweep_row_remain "$HOME/bf.out" "$SID_D" "$GD")" "...REMAIN is A's whole size"
R=$(sweep_row_reason "$HOME/bf.out" "$SID_D" "$GD")
note "reason: $R"
assert_contains "$R" "has grown to checkpoint $CP_D or past it" "...the reason says it grew past the checkpoint"
assert_contains "$R" "move this copy out of ~/.claude/projects" "...and names moving the copy out (the live copy reaches the checkpoint)"
assert_not_contains "$R" "kb-flush-backfill.sh --seed" "...never a --seed (it would not stick)"
assert_eq "flushed" "$(sweep_row_state "$HOME/bf.out" "$SID_D" "$LIVE_SIZE_D")" "the live copy reads flushed"
quiet_auto "$A_D" "$LIVE_D"
assert_eq "0" "$RC" "--auto exits 0"
assert_eq "0" "$(stub_total)" "--auto fires nothing"
same_offset "$HOME/offD.before" "$ST/$SID_D.offset" "the checkpoint is byte-identical"
assert_eq "1" "$(sweep_last diverged)" ".sweep-last diverged=1"
assert_eq "$GD" "$(sweep_last unflushed)" ".sweep-last unflushed=$GD (A's whole size)"

# =============================================== 5. the CONNECTOR arm
step "5. CONNECTOR: the hook marks the frozen copy behind; grown past the checkpoint, a PreCompact relays nothing"
new_home c2rconn
fake_claude_relay
cfg_connector
install_hooks >/dev/null
setup_paths
SID_C="c0fe2047-0000-4000-8000-000000000047"
C8=${SID_C:0:8}
mk_resumed_session "$SID_C"
LIVE_C=$LIVE A_C=$STALE CP_C=$LIVE_SIZE F_C=$STALE_SIZE
N0=$(summaries)
fire_flush "$LIVE_C" "$RESUMED" "$SID_C" SessionEnd
wait_firing "$N0" 60 || fail "setup: the live copy was never relayed: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "$CP_C" "$(cat "$ST/$SID_C.offset" 2>/dev/null)" "setup: the checkpoint sits at the live copy's size"
mk_prefix_copy "$LIVE_C" "$A_C" "$F_C"
rm -f "$LIVE_C"
touch -d @"$(($(mtime_of "$ST/$SID_C.offset") - 3600))" "$A_C"
cp "$ST/$SID_C.offset" "$HOME/offC.before"
N0=$(summaries)
fire_flush "$A_C" "$ORIG" "$SID_C" PreCompact
await_line "reason=behind .*sid=$C8\$" 0 "$N0"
assert_file_contains "$ST/$SID_C.behind" " $A_C size=$F_C checkpoint=$CP_C frozen=$F_C" "the connector arm wrote state/<okey>.behind, frozen=F"
mk_lines "$A_C" 40 "$ORIG" "$SID_C" a
GC=$(size_of "$A_C")
if [ "$GC" -gt "$CP_C" ]; then pass "fixture: A grew past the checkpoint ($GC > $CP_C)"; else fail "fixture: A ($GC) did not pass $CP_C"; fi
NR=$(relay_count)
age_stamp "$ST/$SID_C.behind.stamp"
N0=$(summaries)
L0=$(log_count "reason=behind .*sid=$C8\$")
fire_flush "$A_C" "$ORIG" "$SID_C" PreCompact
assert_eq "0" "$RC" "PreCompact exits 0"
assert_eq "" "$OUT" "...and prints nothing"
await_line "reason=behind .*sid=$C8\$" "$L0" "$N0"
assert_eq "$NR" "$(relay_count)" "NOTHING relayed (still $NR chunk(s) from the setup)"
same_offset "$HOME/offC.before" "$ST/$SID_C.offset" "the checkpoint is byte-identical"
assert_file_contains "$ST/$SID_C.behind" " $A_C size=$GC checkpoint=$CP_C frozen=$F_C" "the marker names A at its new size, frozen=F kept"
assert_contains "$(last_line "reason=behind .*sid=$C8\$")" "has grown to the checkpoint $CP_C or past it" "the line says A has grown past the checkpoint"
assert_eq "0" "$(log_count "restarting from 0")" "never 'restarting from 0'"

# ============ 6. RELEASED: the diverged copy moved out, the session resumed there
step "6. setup: A (resumed in the original dir, tail differs) is marked diverged; the owner moves A out"
setup_home c2rrel
SID_R="e1ea2047-0000-4000-8000-000000000047"
R8=${SID_R:0:8}
mk_resumed_session "$SID_R"
LIVE_R=$LIVE A_R=$STALE CP_R=$LIVE_SIZE PREFIX_R=$STALE_SIZE
fire_flush "$LIVE_R" "$RESUMED" "$SID_R" SessionEnd
wait_firing 0 60 || fail "setup: the live copy never flushed: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "$CP_R" "$(cat "$ST/$SID_R.offset" 2>/dev/null)" "setup: the checkpoint sits at the live copy's size"
mk_prefix_copy "$LIVE_R" "$A_R" "$PREFIX_R"
mk_lines "$A_R" 5 "$ORIG" "$SID_R" a
FR=$(size_of "$A_R")
stub_reset
N0=$(summaries)
fire_flush "$A_R" "$ORIG" "$SID_R" SessionEnd
await_line "reason=stale-copy-diverged .*sid=$R8\$" 0 "$N0"
assert_file_contains "$ST/$SID_R.diverged" " $A_R size=$FR checkpoint=$CP_R cover=$LIVE_R frozen=$FR" \
  "setup: state/<okey>.diverged names A (cover= the live copy)"
mkdir -p "$HOME/quarantine"
mv "$A_R" "$HOME/quarantine/"

step "6. resumed from A's directory: A's path holds the LIVE transcript plus new records; a dry run tables it ready, not diverged"
cp "$LIVE_R" "$A_R"
mk_lines "$A_R" 6 "$ORIG" "$SID_R" a
GR=$(size_of "$A_R")
if [ "$GR" -gt "$CP_R" ]; then pass "fixture: A's path is past the checkpoint ($GR > $CP_R)"; else fail "fixture: A ($GR) is not past $CP_R"; fi
MK=$(head -n1 "$ST/$SID_R.diverged" 2>/dev/null)
touch -d @1700000000 "$A_R" "$LIVE_R"
pin_sweep_clock "$(date +%s)"
run_bf
unpin_sweep_clock
assert_eq "ready" "$(sweep_row_state "$HOME/bf.out" "$SID_R" "$GR")" "A's path reads ready (it carries the checkpoint holder's prefix)"
assert_eq "$((GR - CP_R))" "$(sweep_row_remain "$HOME/bf.out" "$SID_R" "$GR")" "...REMAIN = size - checkpoint ($((GR - CP_R)))"
assert_not_contains "$OUT" "diverged copy(ies)" "...and the summary counts no diverged copy"
assert_eq "$MK" "$(head -n1 "$ST/$SID_R.diverged" 2>/dev/null)" "a dry run leaves the marker untouched (it never writes state)"
assert_eq "0" "$(log_count " marker cleared: ")" "...and logs no clear"

step "6. Stop (gate open) releases the marker with ONE line and drains exactly A[CP:size]"
stub_reset
N0=$(summaries)
KB_FLUSH_STOP_MIN_BYTES=1 KB_FLUSH_STOP_MIN_INTERVAL=0 fire_flush "$A_R" "$ORIG" "$SID_R" Stop
assert_eq "0" "$RC" "Stop exits 0"
assert_eq "" "$OUT" "...and prints nothing"
wait_firing "$N0" 60 || fail "the Stop drain never completed: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_file_missing "$ST/$SID_R.diverged" "the diverged marker is released"
assert_file_missing "$ST/$SID_R.diverged.stamp" "...with its stamp"
assert_eq "1" "$(log_count " marker cleared: state/$SID_R\.diverged named .*, which now carries the checkpoint holder's prefix ev=Stop sid=$R8\$")" \
  "ONE 'marker cleared ... which now carries the checkpoint holder's prefix ev=Stop' line"
assert_contains "$(last_line " marker cleared: ")" "named $A_R, which now carries" "...naming A's path"
assert_eq "1" "$(stub_count ".session_uri==\"session:$SID_R\"")" "ONE request"
assert_eq "$CP_R" "$(stub_field ".session_uri==\"session:$SID_R\"" '.flush_offset' | head -n1)" "...from flush_offset CP"
stub_concat ".session_uri==\"session:$SID_R\"" '.transcript' >"$HOME/rel.got"
tail -c +"$((CP_R + 1))" "$A_R" >"$HOME/rel.want"
if cmp -s "$HOME/rel.want" "$HOME/rel.got"; then
  pass "the bytes received are exactly A[CP:size] ($((GR - CP_R)) bytes)"
else
  fail "the bytes received are exactly A[CP:size] -- got $(size_of "$HOME/rel.got") of $((GR - CP_R)) bytes"
fi
assert_eq "$GR" "$(cat "$ST/$SID_R.offset" 2>/dev/null)" "the checkpoint lands at A's size"
assert_eq "0" "$(log_count "restarting from 0")" "never 'restarting from 0'"
run_bf
assert_eq "flushed" "$(sweep_row_state "$HOME/bf.out" "$SID_R" "$GR")" "the table reads A flushed"
assert_eq "stale-copy" "$(sweep_row_state "$HOME/bf.out" "$SID_R" "$CP_R")" "...and the old live copy a stale copy of it"

finish

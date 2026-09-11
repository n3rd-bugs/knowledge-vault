#!/usr/bin/env bash
# CASE: kb-flush-backfill.sh --auto / --seed / --status -- the SWEEP the timer
#       and the hooks' piggyback run (wave C item C7, KB 33784; root cause
#       KB 33783; proposal_v2 App. B W4 for --seed, App. I for the state files).
# PINS (kb-flush-backfill.sh, offset accounting only; the hook is the transport):
#   * --auto has EMPTY stdout and an exit-code contract: 0 ran, 2 tripwire /
#     refused --seed, 3 sweep lock held (silent); a bad flag is 1.
#   * QUIET WINDOW: a transcript modified within KB_SWEEP_QUIET_SECONDS is
#     `live` and skipped (clock pinned with pin_sweep_clock, no sleeping); the
#     same transcript drains once the clock moves on. .sweep-last is written
#     after every completed --auto (ts= from the pinned clock) and the
#     piggyback .sweep.trigger is CONSUMED.
#   * BUSY (a held session flock): no drain, no NOT CAPTURED, no stall
#     increment, counted busy= in .sweep-last; drains once released.
#   * SWEEP LOCK held: --auto exits 3 silently, --push exits 3 with one line,
#     --status reports HELD.
#   * BUDGET: KB_SWEEP_BUDGET_BYTES defers the rest to the next run;
#     KB_SWEEP_MAX_TRANSCRIPTS bounds the count; oldest first.
#   * A `Backfill` event HOLDS the trailing partial line like PreCompact and the
#     checkpoint SURVIVES (never deleted); the REST stamp says
#     flush_event=Backfill and no SessionEnd is ever synthesised.
#   * STALL: KB_SWEEP_STALL_RUNS (6) consecutive no-progress runs log exactly ONE
#     `STALLED` line (never again for the same stall), the transcript backs off
#     for KB_SWEEP_STALL_BACKOFF and is retried after it -- never abandoned.
#   * TRIPWIRE: a hook without the line-2 lineage marker, or one that still
#     contains `rm -f "$OFFF"`, is refused with ONE `BUG: hook lineage mismatch`
#     line, exit 2 and ZERO requests; the real hook is the positive control.
#   * --seed <sid> <bytes|current>: snapped DOWN to a line end, printed as
#     `seeded <sid8> offset=N (snapped from M)`, logged; refused (exit 2) past
#     the size, under a held session lock, under --auto, for an unknown sid.
#   * an OFF-LATTICE seed under the NEW hook costs exactly ONE new row, then
#     every chunk is duplicate:true (dup == N - 1 on the ok line).
#   * THE HOOK'S OWN RULE for a transcript with NO checkpoint: a first record
#     NEWER than .checkpoints-trusted-since is `ready` at 0 and --auto drains it
#     (a session killed before its first drain is the sweep's headline case);
#     one that PREDATES the stamp is `unknown` and --auto never fires it, not
#     even with --include-unknown (.sweep-last counts unknown=). With
#     KB_FLUSH_BRANCH_SCAN=0 a fork is not `branch` but whatever the hook would
#     do without the scan (post-stamp: ready from 0).
#   * FD HYGIENE: the hook is NOT handed the sweep's flock fd 8 (nor the index
#     fd 3), so a detached drain that outlives the run cannot keep .sweep.lock
#     HELD -- an immediate --status says `free` while that drain still runs.
#   * the tripwire's LOG copy is rate-limited (one an hour, state/.tripwire.stamp)
#     while the stdout line is per run -- the timer must not flood the 500-line
#     log the brief scans.
#   * the stall back-off gates --auto only: an explicit --push inside the
#     back-off fires the transcript (the owner's retry).
#   * --projects / --timeout keep working for the owner.
# DEPENDS ON C10 (RIG): pin_sweep_clock; the stub's (session_uri, hash) dedupe
#   key and flush_* capture; and on the stage-1 hook's Backfill handling.
. /rig/lib.sh

new_home sweep
stub_reset
cfg_rest
install_hooks >/dev/null
BF="$HOOKS/kb-flush-backfill.sh"
PROJDIR="$HOME/.claude/projects"
WORK="$HOME/work"
ST="$HOOKS/state"
mkdir -p "$ST"
printf '2000-01-01T00:00:00Z\n' >"$ST/.checkpoints-trusted-since"

slug_of() { printf '%s' "$1" | tr '/' '-'; }
# Every record carries a timestamp, like a real transcript; the default is far
# past the case's 2000-01-01 trust stamp (a POST-stamp session). RIG_FIXTURE_TS
# overrides it, exactly as for lib.sh's mk_transcript -- a PRE-stamp value
# (1999-...) builds the transcript the hook's unseeded rule refuses.
mk_session() { # <project-dir-name> <session-id> <lines> [cwd] -> path (old mtime)
  mkdir -p "$PROJDIR/$1"
  "$RIG_PY" - "$PROJDIR/$1/$2.jsonl" "$3" "${4:-}" "${RIG_FIXTURE_TS:-2026-09-11T00:00:00.000Z}" <<'EOF'
import json, sys
path, n, cwd, ts = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
with open(path, "w", encoding="utf-8") as fh:
    for i in range(n):
        rec = {"type": "user", "timestamp": ts, "i": i, "text": "sweep line %d %s" % (i, "y" * 40)}
        if cwd:
            rec["cwd"] = cwd
        fh.write(json.dumps(rec) + "\n")
EOF
  touch -d @1700000000 "$PROJDIR/$1/$2.jsonl"
  printf '%s' "$PROJDIR/$1/$2.jsonl"
}
run_bf() {
  OUT=$("$BF" "$@" 2>&1)
  RC=$?
  printf '%s\n' "$OUT" >"$HOME/bf.out"
  export RC
}
bf_row() { awk -v s="$1" '$2 == s { print $1, $2, $3, $4, $5, $6 }' "$HOME/bf.out"; }
state_of() { bf_row "$1" | awk '{ print $6 }'; }
hold_flock() { # <lock file> -> $HOLD (a background process holding the flock)
  (
    exec 9>"$1"
    flock 9 || exit 1
    exec sleep 300
  ) &
  HOLD=$!
  sleep 0.5
}
release() { kill "$HOLD" 2>/dev/null || true; wait "$HOLD" 2>/dev/null || true; }
dup_of() { grep " sid=$1" "$LOGF" | grep -oE 'dup=[0-9]+' | tail -1 | cut -d= -f2; }
sweep_last() { sed -n "s/.* $1=\([0-9]*\).*/\1/p" "$ST/.sweep-last" | head -1; }

NOWR=$(date +%s)
pin_sweep_clock "$NOWR"

# ======================================================= 1. quiet window
step "--auto skips a transcript modified inside the quiet window (live), stdout empty, trigger consumed"
mkdir -p "$WORK/quiet"
T_Q=$(mk_session "$(slug_of "$WORK/quiet")" s-quiet 20 "$WORK/quiet")
SZ_Q=$(wc -c <"$T_Q")
printf 0 >"$ST/s-quiet.offset" # seeded at 0 -> ready
touch "$T_Q"                   # modified NOW
touch "$ST/.sweep.trigger"
run_bf --auto
assert_eq "0" "$RC" "--auto exits 0"
assert_eq "" "$OUT" "--auto prints NOTHING"
settle
settle
assert_eq "0" "$(stub_total)" "a live transcript was not sent"
assert_eq "0" "$(cat "$ST/s-quiet.offset")" "its checkpoint did not move"
assert_file_missing "$ST/.sweep.trigger" ".sweep.trigger was consumed by the run"
assert_file_contains "$ST/.sweep-last" "ts=$NOWR " ".sweep-last carries the (pinned) clock"
assert_eq "1" "$(sweep_last live)" ".sweep-last counts live=1"
run_bf
assert_eq "live" "$(state_of s-quiet)" "the dry table calls it live"
run_bf --projects "$PROJDIR" --timeout 30
assert_eq "0" "$RC" "--projects <dir> --timeout <secs> are accepted"
assert_eq "live" "$(state_of s-quiet)" "...and table the same transcript"
run_bf --projects "$HOME/no-such-projects"
assert_eq "0" "$RC" "--projects pointing nowhere is 'nothing to do', exit 0"
assert_contains "$OUT" "nothing to do" "...and says so"
pin_sweep_clock "$((NOWR + 1000))"
run_bf --auto
assert_eq "0" "$RC" "--auto exits 0 once the window has passed"
assert_eq "" "$OUT" "still silent"
assert_eq "$SZ_Q" "$(cat "$ST/s-quiet.offset")" "the same transcript drained once the clock moved on"
assert_eq "Backfill" "$(stub_field '.transport=="rest"' '.flush_event' | tail -1)" "the REST stamp says flush_event=Backfill"
assert_file_contains "$LOGF" "backfill s-quiet quiet pushed base=0 size=$SZ_Q sent=$SZ_Q" "the sweep's own line for the drained transcript"
assert_eq "1" "$(sweep_last pushed)" ".sweep-last counts pushed=1"

# ======================================================= 1b. branch state
step "a fork of a drained transcript reads 'branch', and --auto sends only the fork's own bytes"
T_F="$(dirname "$T_Q")/s-forkof.jsonl"
cp "$T_Q" "$T_F"
"$RIG_PY" - "$HOME/fork.tail" <<'EOF'
import json, sys
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"type": "user", "i": 100, "text": "forked question"}) + "\n")
    fh.write(json.dumps({"type": "assistant", "i": 101, "text": "forked answer"}) + "\n")
EOF
cat "$HOME/fork.tail" >>"$T_F"
touch -d @1700000000 "$T_F"
TAIL_F=$(wc -c <"$HOME/fork.tail")
run_bf
assert_eq "quiet s-forkof $((SZ_Q + TAIL_F)) $SZ_Q $TAIL_F branch" "$(bf_row s-forkof)" "no checkpoint + a same-first-line sibling with one -> branch, remainder = the fork's tail"
assert_file_contains "$HOME/bf.out" "branch of s-quiet:" "the row names the parent"
KB_FLUSH_BRANCH_SCAN=0 run_bf
assert_eq "quiet s-forkof $((SZ_Q + TAIL_F)) 0 $((SZ_Q + TAIL_F)) ready" "$(bf_row s-forkof)" \
  "KB_FLUSH_BRANCH_SCAN=0: not branch but what the hook would do without the scan (post-stamp -> ready from 0, the whole copy)"
B1=$(stub_total)
run_bf --auto
assert_eq "$((SZ_Q + TAIL_F))" "$(cat "$ST/s-forkof.offset" 2>/dev/null)" "the fork drained to EOF (the hook seeded it at the shared prefix)"
assert_eq "1" "$(( $(stub_total) - B1 ))" "exactly one request for the fork"
assert_eq "$TAIL_F" "$(stub_field '.session_uri=="session:s-forkof"' '.transcript_bytes')" "...carrying only the fork's own $TAIL_F bytes"
assert_file_contains "$LOGF" "branch of s-quiet: seeded offset=$SZ_Q" "the hook logged the branch seed"

# ======================================================= 1c. no checkpoint: the hook's own rule
step "a killed session with NO checkpoint and a post-stamp first record is ready at 0 and --auto drains it"
mkdir -p "$WORK/killed"
T_K=$(mk_session "$(slug_of "$WORK/killed")" s-killed 20 "$WORK/killed")
SZ_K=$(wc -c <"$T_K")
run_bf
assert_eq "killed s-killed $SZ_K 0 $SZ_K ready" "$(bf_row s-killed)" "no checkpoint + first record newer than the trust stamp -> ready at 0 (the hook's own rule)"
assert_file_contains "$HOME/bf.out" "post-dates the trust stamp" "the row says why it is fireable"
assert_file_missing "$ST/s-killed.offset.lock" "a DRY RUN probes the session lock read-only (no lock file created)"
B2=$(stub_total)
run_bf --auto
assert_eq "0" "$RC" "--auto exits 0"
assert_eq "$SZ_K" "$(cat "$ST/s-killed.offset" 2>/dev/null)" "--auto drained it from 0 (the hook wrote the checkpoint itself)"
assert_eq "1" "$(( $(stub_total) - B2 ))" "exactly one request for it"
assert_file_contains "$LOGF" "ev=Backfill sid=s-killed" "the hook's own ok line says ev=Backfill"
step "a PRE-stamp transcript with no checkpoint is unknown: --auto never fires it, not even with --include-unknown"
mkdir -p "$WORK/old"
T_O=$(RIG_FIXTURE_TS=1999-01-01T00:00:00.000Z mk_session "$(slug_of "$WORK/old")" s-old 20 "$WORK/old")
SZ_O=$(wc -c <"$T_O")
run_bf
assert_eq "old s-old $SZ_O - $SZ_O unknown" "$(bf_row s-old)" "predates the 2000-01-01 stamp -> unknown"
assert_file_contains "$HOME/bf.out" "predates the trust stamp" "...with the reason and the way out"
B3=$(stub_total)
run_bf --auto --include-unknown
assert_eq "0" "$RC" "exits 0"
assert_eq "" "$OUT" "silent"
settle
assert_eq "$B3" "$(stub_total)" "--auto --include-unknown sent NOTHING for the unknown transcript"
assert_file_missing "$ST/s-old.offset" "...and no checkpoint appeared"
assert_eq "1" "$(sweep_last unknown)" ".sweep-last counts unknown=1"

# ======================================================= 2. busy (held flock)
step "a held session lock is BUSY: nothing sent, no NOT CAPTURED, no stall increment"
mkdir -p "$WORK/busy"
T_B=$(mk_session "$(slug_of "$WORK/busy")" s-busy 20 "$WORK/busy")
SZ_B=$(wc -c <"$T_B")
printf 0 >"$ST/s-busy.offset"
hold_flock "$ST/s-busy.offset.lock"
run_bf
assert_eq "busy" "$(state_of s-busy)" "the dry table reads busy while the lock is held"
BEFORE=$(stub_total)
: >"$LOGF"
run_bf --auto
assert_eq "0" "$RC" "--auto exits 0 around a busy session"
settle
assert_eq "$BEFORE" "$(stub_total)" "nothing was sent for the busy session"
assert_eq "0" "$(cat "$ST/s-busy.offset")" "its checkpoint did not move"
assert_not_contains "$(cat "$LOGF")" "NOT CAPTURED" "busy is never a NOT CAPTURED"
assert_file_missing "$ST/s-busy.sweep" "busy never increments the stall counter (no s-busy.sweep)"
assert_eq "1" "$(sweep_last busy)" ".sweep-last counts busy=1"
release
run_bf --auto
assert_eq "$SZ_B" "$(cat "$ST/s-busy.offset")" "positive control: released -> drained"

# ======================================================= 3. the sweep lock
step "the sweep lock: --auto exits 3 silently, --push exits 3 with one line, --status says HELD"
hold_flock "$ST/.sweep.lock"
run_bf --auto
assert_eq "3" "$RC" "--auto exits 3 under a held sweep lock"
assert_eq "" "$OUT" "...silently"
run_bf --push
assert_eq "3" "$RC" "--push exits 3 too"
assert_contains "$OUT" ".sweep.lock" "...naming the lock"
run_bf --status
assert_contains "$OUT" "sweep lock : HELD" "--status reports the held lock"
release
run_bf --status
assert_eq "0" "$RC" "--status exits 0"
assert_contains "$OUT" "sweep lock : free" "--status reports it free again"
assert_contains "$OUT" "lineage ok" "--status reports the hook lineage"
assert_contains "$OUT" "last sweep :" "--status reports the last sweep"

# ======================================================= 3b. fd hygiene
step "the hook is NOT handed the sweep's flock fd 8: a drain that outlives the run cannot keep .sweep.lock held"
# A stand-in hook with the lineage marker that (a) records whether fd 8 reached
# it and (b) detaches a child that outlives the sweep, exactly like the real
# hook's setsid drain. Without the close on the firing line the child inherits
# the flock and the next --auto would exit 3 in silence for as long as it lives.
mkdir -p "$WORK/inh"
T_I=$(mk_session "$(slug_of "$WORK/inh")" s-inherit 5 "$WORK/inh")
printf 0 >"$ST/s-inherit.offset"
cat >"$HOME/inherit.sh" <<EOF
#!/usr/bin/env bash
# KB-FLUSH-LINEAGE: never-delete-offsets v2
if { : >&8; } 2>/dev/null; then printf open >"$HOME/fd8"; else printf closed >"$HOME/fd8"; fi
if { : <&3; } 2>/dev/null; then printf open >"$HOME/fd3"; else printf closed >"$HOME/fd3"; fi
setsid bash -c 'exec sleep 6' </dev/null >/dev/null 2>&1 &
exit 0
EOF
chmod +x "$HOME/inherit.sh"
run_bf --push --hook "$HOME/inherit.sh" --timeout 20
assert_eq "0" "$RC" "the push completes (the stand-in logs nothing: a no-progress verdict, not an exit code)"
assert_eq "closed" "$(cat "$HOME/fd8" 2>/dev/null)" "fd 8 (the sweep flock) is CLOSED for the hook"
assert_eq "closed" "$(cat "$HOME/fd3" 2>/dev/null)" "fd 3 (the index) is CLOSED for the hook"
run_bf --status
assert_contains "$OUT" "sweep lock : free" "--status says free while the detached child is still alive"
run_bf --auto
assert_eq "0" "$RC" "...and the next --auto is not locked out (exit 0, not 3)"
run_bf --seed s-inherit current >/dev/null
assert_eq "0" "$RC" "retire the fixture: seeded at current"

# ======================================================= 4. budgets
step "KB_SWEEP_BUDGET_BYTES defers the rest; KB_SWEEP_MAX_TRANSCRIPTS bounds the count; oldest first"
mkdir -p "$WORK/bud"
T_1=$(mk_session "$(slug_of "$WORK/bud")" s-bud1 400 "$WORK/bud")
T_2=$(mk_session "$(slug_of "$WORK/bud")" s-bud2 400 "$WORK/bud")
T_3=$(mk_session "$(slug_of "$WORK/bud")" s-bud3 400 "$WORK/bud")
touch -d @1700000100 "$T_1"
touch -d @1700000200 "$T_2"
touch -d @1700000300 "$T_3"
printf 0 >"$ST/s-bud1.offset"
printf 0 >"$ST/s-bud2.offset"
printf 0 >"$ST/s-bud3.offset"
SZ_1=$(wc -c <"$T_1")
note "each budget transcript is $SZ_1 bytes; budget = 1.4 x that"
KB_SWEEP_BUDGET_BYTES=$((SZ_1 + SZ_1 * 4 / 10)) run_bf --auto
assert_eq "0" "$RC" "exits 0"
assert_eq "$SZ_1" "$(cat "$ST/s-bud1.offset")" "the oldest drained"
assert_eq "$SZ_1" "$(cat "$ST/s-bud2.offset")" "the second drained on the remaining budget"
assert_eq "0" "$(cat "$ST/s-bud3.offset")" "the third was DEFERRED (budget spent)"
assert_eq "1" "$(sweep_last deferred)" ".sweep-last counts deferred=1"
T_4=$(mk_session "$(slug_of "$WORK/bud")" s-bud4 400 "$WORK/bud")
touch -d @1700000400 "$T_4"
printf 0 >"$ST/s-bud4.offset"
KB_SWEEP_MAX_TRANSCRIPTS=1 run_bf --auto
assert_eq "$SZ_1" "$(cat "$ST/s-bud3.offset")" "max-transcripts=1: the oldest deferred one goes"
assert_eq "0" "$(cat "$ST/s-bud4.offset")" "...and the newer one waits"
run_bf --auto
assert_eq "$SZ_1" "$(cat "$ST/s-bud4.offset")" "the next run continues from where the budget stopped"

# ======================================================= 5. Backfill holds the tail
step "a Backfill event HOLDS the trailing partial line and the checkpoint SURVIVES"
mkdir -p "$WORK/half"
T_H=$(mk_session "$(slug_of "$WORK/half")" s-half 10 "$WORK/half")
COMPLETE=$(wc -c <"$T_H")
printf '{"type":"user","half":' >>"$T_H"
touch -d @1700000000 "$T_H"
printf 0 >"$ST/s-half.offset"
run_bf --auto
assert_eq "$COMPLETE" "$(cat "$ST/s-half.offset")" "the checkpoint stops at the last newline (half line held)"
stub_concat '.transport=="rest"' '.transcript' >"$HOME/got"
if grep -q '"half":' "$HOME/got"; then fail "the unterminated half line was sent"; else pass "the half line was NOT sent"; fi
assert_eq "0" "$(log_count 'ev=SessionEnd')" "no SessionEnd was synthesised by the sweep"
assert_eq "0" "$(stub_count '.transport=="rest" and .flush_event=="SessionEnd"')" "no request was stamped SessionEnd"
run_bf
assert_eq "tail-held" "$(state_of s-half)" "the dry table reads tail-held"
printf '"x"}\n' >>"$T_H"
touch -d @1700000000 "$T_H"
SZ_H=$(wc -c <"$T_H")
run_bf --auto
assert_eq "$SZ_H" "$(cat "$ST/s-half.offset")" "once the record is complete the rest drains"
if [ -f "$ST/s-half.offset" ]; then pass "the offset file survived the drained sweep"; else fail "the offset file was deleted"; fi

# ======================================================= 6. tripwire
step "TRIPWIRE: a delete-lineage hook is refused with ONE BUG line, exit 2, zero requests"
mkdir -p "$WORK/trip"
T_T=$(mk_session "$(slug_of "$WORK/trip")" s-trip 20 "$WORK/trip")
SZ_T=$(wc -c <"$T_T")
printf 0 >"$ST/s-trip.offset"
sed '2s/.*/# an older hook without the lineage marker/' "$HOOKS/flush-session-to-kb.sh" >"$HOME/oldhook.sh"
chmod +x "$HOME/oldhook.sh"
BEFORE=$(stub_total)
run_bf --push --hook "$HOME/oldhook.sh"
assert_eq "2" "$RC" "no line-2 marker -> exit 2"
assert_eq "1" "$(printf '%s\n' "$OUT" | grep -c 'BUG: hook lineage mismatch — refusing to sweep with a delete-lineage hook')" "exactly ONE BUG line"
settle
assert_eq "$BEFORE" "$(stub_total)" "zero requests"
assert_eq "0" "$(cat "$ST/s-trip.offset")" "no checkpoint moved"
assert_file_contains "$LOGF" "BUG: hook lineage mismatch" "the BUG line is also in the flush log (the brief surfaces it)"
cp "$HOOKS/flush-session-to-kb.sh" "$HOME/delhook.sh"
printf '\n# appended by the rig: the delete the never-delete lineage removed\nrm -f "$OFFF"\n' >>"$HOME/delhook.sh"
run_bf --auto --hook "$HOME/delhook.sh"
assert_eq "2" "$RC" "marker present but rm -f \"\$OFFF\" present -> exit 2 (under --auto too)"
assert_contains "$OUT" "BUG: hook lineage mismatch" "--auto prints the one BUG line (its only permitted stdout)"
assert_eq "$BEFORE" "$(stub_total)" "still zero requests"
assert_eq "1" "$(log_count 'BUG: hook lineage mismatch')" "the LOG copy is rate-limited to one an hour (state/.tripwire.stamp): the timer must not flood the log the brief scans"
assert_file_contains "$ST/.tripwire.stamp" "$KB_BACKFILL_NOW" "the stamp carries the (pinned) clock of the logged one"
run_bf --push
assert_eq "0" "$RC" "positive control: the real hook passes the tripwire"
assert_eq "$SZ_T" "$(cat "$ST/s-trip.offset")" "...and drains the transcript"

# ======================================================= 7. --seed
step "--seed snaps DOWN to a line end, prints and logs it, and is refused when it should be"
mkdir -p "$WORK/seed"
# PRE-stamp on purpose: a post-stamp no-checkpoint transcript is simply `ready`
# (section 1c); --seed exists for the one the hook's unseeded rule refuses.
T_S=$(RIG_FIXTURE_TS=1999-01-01T00:00:00.000Z mk_session "$(slug_of "$WORK/seed")" s-seedme 40 "$WORK/seed")
SZ_S=$(wc -c <"$T_S")
run_bf
assert_eq "unknown" "$(state_of s-seedme)" "precondition: no checkpoint + predates the trust stamp -> unknown"
run_bf --seed s-seedme
assert_eq "1" "$RC" "--seed with a missing operand is a usage error (exit 1)"
assert_not_contains "$OUT" "shift count" "...the script's own message, not bash's 'shift count out of range'"
M=$((SZ_S / 2 + 7)) # mid-line by construction (lines are equal-length + a 1-digit index drift)
run_bf --seed s-seedme "$M"
assert_eq "0" "$RC" "--seed exits 0"
N=$(cat "$ST/s-seedme.offset" 2>/dev/null)
assert_eq "seeded s-seedme offset=$N (snapped from $M)" "$OUT" "prints seeded <sid8> offset=N (snapped from M)"
assert_le "$N" "$M" "the snap never goes UP"
assert_ne "$N" "$M" "fixture: M was mid-line, so the snap moved"
assert_eq "" "$(head -c "$N" "$T_S" | tail -c1)" "the byte before the seed is a newline"
assert_file_contains "$LOGF" "backfill s-seedme seed seed offset=$N (was none) size=$SZ_S" "the seed is logged with the previous value"
run_bf
assert_eq "seed s-seedme $SZ_S $N $((SZ_S - N)) ready" "$(bf_row s-seedme)" "the table now shows the seeded remainder"
run_bf --seed s-seedme "$((SZ_S + 1))"
assert_eq "2" "$RC" "past the size -> refused (exit 2)"
assert_eq "$N" "$(cat "$ST/s-seedme.offset")" "...and the checkpoint is untouched"
hold_flock "$ST/s-seedme.offset.lock"
run_bf --seed s-seedme current
assert_eq "2" "$RC" "under a held session lock -> refused (exit 2)"
assert_eq "$N" "$(cat "$ST/s-seedme.offset")" "...checkpoint untouched"
release
run_bf --auto --seed s-seedme current
assert_eq "2" "$RC" "--seed under --auto -> refused (exit 2)"
assert_eq "$N" "$(cat "$ST/s-seedme.offset")" "...checkpoint untouched"
run_bf --seed no-such-session current
assert_eq "2" "$RC" "an unknown sid -> refused (exit 2)"
run_bf --seed s-seedme current
assert_eq "0" "$RC" "current -> exits 0"
assert_eq "seeded s-seedme offset=$SZ_S (snapped from $SZ_S)" "$OUT" "current = the size (already a line end)"
run_bf
assert_eq "flushed" "$(state_of s-seedme)" "seeded at current -> flushed"
run_bf --bogus
assert_eq "1" "$RC" "exit-code matrix: a bad flag is 1"

# ======================================================= 8. off-lattice seed = 1 row
step "an OFF-LATTICE seed under the new hook costs exactly ONE new row, then duplicate:true"
export KB_FLUSH_CHUNK=200000 KB_FLUSH_SEND_MAX=260000
mkdir -p "$WORK/lat"
LDIR="$PROJDIR/$(slug_of "$WORK/lat")"
mkdir -p "$LDIR"
T_L="$LDIR/s-lattice.jsonl"
mk_big_transcript "$HOME/lat.big" 1500000
# One leading record carries the cwd (mk_big_transcript writes none), so the
# sweep's cwd comes from the transcript, never from decoding the directory slug.
{
  "$RIG_JQ" -nc --arg c "$WORK/lat" '{type:"user",timestamp:"2026-09-11T00:00:00.000Z",cwd:$c,text:"opener"}'
  cat "$HOME/lat.big"
} >"$T_L"
touch -d @1700000000 "$T_L"
SZ_L=$(wc -c <"$T_L")
printf 0 >"$ST/s-lattice.offset"
stub_reset
run_bf --auto
assert_eq "$SZ_L" "$(cat "$ST/s-lattice.offset")" "the from-0 pass drained to EOF"
NL=$(stub_count '.session_uri=="session:s-lattice"')
assert_ge "$NL" "5" "cut into several cells ($NL)"
stub_requests | "$RIG_JQ" -r 'select(.session_uri=="session:s-lattice") | .transcript_bytes' |
  awk '{ s += $1; print s }' >"$HOME/lat.ends"
V=$("$RIG_PY" - "$T_L" <<'EOF'
import sys
data = open(sys.argv[1], "rb").read()
print(data.rfind(b"\n", 0, 100000) + 1)
EOF
)
if grep -qx "$V" "$HOME/lat.ends"; then fail "fixture: V=$V is a lattice point"; else pass "fixture: V=$V is off-lattice"; fi
run_bf --seed s-lattice "$V"
assert_eq "seeded s-lattic offset=$V (snapped from $V)" "$OUT" "a line-boundary seed is not moved"
B8=$(stub_count '.session_uri=="session:s-lattice"')
run_bf --auto
assert_eq "$SZ_L" "$(cat "$ST/s-lattice.offset")" "the seeded pass reached EOF"
N8=$(($(stub_count '.session_uri=="session:s-lattice"') - B8))
assert_ge "$N8" "2" "the seeded pass sent chunks ($N8)"
assert_eq "$((N8 - 1))" "$(dup_of s-lattic)" "dup == N - 1: exactly ONE new row ([seed, next B_k)), the rest duplicate:true"
assert_eq "1" "$(stub_requests | "$RIG_JQ" -c 'select(.session_uri=="session:s-lattice")' | tail -n "$N8" | "$RIG_JQ" -c 'select(.result.duplicate==false)' | wc -l | tr -d ' ')" \
  "the stub agrees: one non-duplicate row among the seeded pass"
unset KB_FLUSH_CHUNK KB_FLUSH_SEND_MAX

# ======================================================= 9. stall (fresh HOME, dead endpoint)
new_home stall
stub_reset
cfg_rest "$RIG_TOKEN" "http://127.0.0.1:9" # nothing listens: every chunk fails at once
install_hooks >/dev/null
BF="$HOOKS/kb-flush-backfill.sh"
PROJDIR="$HOME/.claude/projects"
WORK="$HOME/work"
ST="$HOOKS/state"
mkdir -p "$ST" "$WORK/stall"
printf '2000-01-01T00:00:00Z\n' >"$ST/.checkpoints-trusted-since"
pin_sweep_clock "$NOWR"
T_ST=$(mk_session "$(slug_of "$WORK/stall")" s-stall 20 "$WORK/stall")
printf 0 >"$ST/s-stall.offset"
step "six no-progress runs log ONE STALLED line; the transcript backs off, then is retried"
# The sweep's stall counter is its OWN back-off (hybrid_v1 s2.5): the hook's
# per-session `.retry` marker (C6's transport back-off) must not decide whether
# a Backfill firing counts here, so it is cleared before every run.
for i in 1 2 3 4 5 6; do
  rm -f "$ST/s-stall.retry"
  run_bf --auto
  assert_eq "0" "$RC" "run $i exits 0 (a failed chunk is reported, not an exit code)"
  assert_eq "$i" "$(sed -n 's/^fails=\([0-9]*\).*/\1/p' "$ST/s-stall.sweep")" "run $i: fails=$i in s-stall.sweep"
done
assert_eq "1" "$(log_count 'STALLED')" "exactly ONE STALLED line after $i runs"
assert_eq "0" "$(cat "$ST/s-stall.offset")" "the checkpoint never moved (nothing was accepted)"
assert_ge "$(log_count 'chunk FAILED')" "6" "the hook's own failure lines are there (>= 6)"
assert_eq "1" "$(sweep_last stalled)" ".sweep-last counts stalled=1"
run_bf
assert_eq "stalled" "$(state_of s-stall)" "the dry table reads stalled"
FAILS_BEFORE=$(log_count 'chunk FAILED')
rm -f "$ST/s-stall.retry"
run_bf --auto
assert_eq "$FAILS_BEFORE" "$(log_count 'chunk FAILED')" "inside the back-off the transcript is NOT fired by --auto"
assert_eq "1" "$(log_count 'STALLED')" "still exactly one STALLED line"
rm -f "$ST/s-stall.retry"
run_bf --push
assert_eq "0" "$RC" "--push inside the back-off exits 0"
assert_ge "$(log_count 'chunk FAILED')" "$((FAILS_BEFORE + 1))" "...and FIRES the stalled transcript: an explicit --push is the owner's retry"
assert_eq "7" "$(sed -n 's/^fails=\([0-9]*\).*/\1/p' "$ST/s-stall.sweep")" "the no-progress --push still counts (fails=7)"
assert_file_contains "$ST/.sweep-last" "mode=push" ".sweep-last is refreshed by a completed --push too (a timer-less box's only sweep)"
FAILS_BEFORE=$(log_count 'chunk FAILED')
pin_sweep_clock "$((NOWR + 7 * 3600))"
rm -f "$ST/s-stall.retry"
run_bf --auto
assert_ge "$(log_count 'chunk FAILED')" "$((FAILS_BEFORE + 1))" "after the back-off --auto retries it (never abandoned)"
assert_eq "8" "$(sed -n 's/^fails=\([0-9]*\).*/\1/p' "$ST/s-stall.sweep")" "fails keeps counting past the threshold"
assert_eq "1" "$(log_count 'STALLED')" "...without a second STALLED line"
assert_not_contains "$(cat "$LOGF")" "NOT CAPTURED" "a stall is never a NOT CAPTURED"
unpin_sweep_clock

finish

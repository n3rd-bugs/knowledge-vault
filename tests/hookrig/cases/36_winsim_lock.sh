#!/usr/bin/env bash
# IMAGE: winsim
# CASE: mkdir LOCK on a box with NO flock (Git Bash) — the pid-owned fallback
#       (C3, Appendix B, KB 33784; V1-M2). Reclaim ONLY a DEAD pid past the
#       no-progress bound; NEVER on wall-clock age; the trap removes only a dir
#       this process owns; every offset write re-checks ownership first.
# PINS (flush-session-to-kb.sh KB LOCK TRIO v1 block + the drain child):
#   unit (the trio sourced verbatim from the installed hook):
#   * kb_lock_take/owned/touch/release lifecycle on the mkdir path.
#   * a held HEALTHY dir => a second take is BUSY (refused), dir kept.
#   * a DEAD-pid dir that is STALE (no mtime progress past KB_FLUSH_LOCK_STALE_MIN)
#     => reclaimed; a dead-pid dir that is FRESH => NOT reclaimed (no age-only reclaim).
#   * a FROZEN holder (alive but kill -STOP'd, mtime back-dated) is NEVER reclaimed.
#   * kb_lock_owned is FALSE when the pid file names another pid.
#   * a TERM'd holder EXITS (143) and its EXIT trap removes its own dir.
#   end-to-end (the real detached child, its $$, exported STATE, trap, release):
#   * SessionEnd against a held dir logs "SessionEnd deferred (lock held" and no
#     NOT CAPTURED; the offset is untouched (nothing is lost, the next firing
#     resumes).
#   * a FROZEN REAL holder: kill -STOP the draining child, back-date its dir, a
#     second firing is busy (no rows, no offset move), kill -CONT -> exactly one
#     drain: offset == SIZE, reassembly byte-identical, stub rows == chunk count
#     (no rewind, no re-send), and the child's own .lock.d is GONE after the ok
#     line (released before the state sweep).
#   * a LOST lock mid-drain (pid file overwritten by a "reclaimer"): the holder
#     logs exactly one "lock lost — stopping", never advances the offset after
#     it, and does not remove the dir it no longer owns.
# DEPENDS ON C10 (RIG): Dockerfile.winsim parks `flock` off PATH so the mkdir
#   path is the one under test (setsid is already parked on this image).
. /rig/lib.sh

new_home winlock
if command -v flock >/dev/null 2>&1; then
  fail "flock is on PATH — the winsim image must park it so the mkdir path runs"
else
  pass "no flock on PATH (mkdir lock path is under test)"
fi

cfg_rest
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"

# --- extract the lock trio VERBATIM from the installed hook and source it ---
sed -n '/^# >>> KB LOCK TRIO v1/,/^# <<< KB LOCK TRIO v1/p' "$HOOKS/flush-session-to-kb.sh" >"$HOME/trio.sh"
assert_ge "$(wc -l <"$HOME/trio.sh")" "20" "the delimited lock-trio block was extracted"
STATE="$HOOKS/state"
export STATE KB_FLUSH_LOCK_STALE_MIN=120
# shellcheck disable=SC1090
. "$HOME/trio.sh"

step "lifecycle: take -> owned -> touch -> release"
kb_lock_take L1; assert_eq "0" "$?" "take L1"
assert_eq "$$" "$(cat "$STATE/L1.offset.lock.d/pid")" "the dir pid is this process"
kb_lock_owned; assert_eq "0" "$?" "owned after take"
kb_lock_touch; assert_eq "0" "$?" "touch ok"
kb_lock_release; assert_eq "0" "$?" "release"
assert_file_missing "$STATE/L1.offset.lock.d" "release removed our own dir"

step "a held HEALTHY dir (live pid) => a second take is BUSY, dir kept"
sleep 300 & HOLDER=$!
mkdir -p "$STATE/L2.offset.lock.d"; printf '%s' "$HOLDER" >"$STATE/L2.offset.lock.d/pid"
kb_lock_take L2; rc=$?
assert_eq "1" "$rc" "a live-pid dir is not taken (busy)"
assert_eq "$HOLDER" "$(cat "$STATE/L2.offset.lock.d/pid")" "the healthy holder's dir is kept"

step "a live-pid dir with a BACK-DATED mtime is STILL not reclaimed (frozen holder, no age reclaim)"
touch -d '3 hours ago' "$STATE/L2.offset.lock.d" 2>/dev/null || touch -t 202001010000 "$STATE/L2.offset.lock.d"
kill -STOP "$HOLDER" 2>/dev/null || true
kb_lock_take L2; rc=$?
assert_eq "1" "$rc" "a frozen (alive) holder past the stale bound is NEVER stolen"
kill -CONT "$HOLDER" 2>/dev/null || true
kill "$HOLDER" 2>/dev/null || true
wait "$HOLDER" 2>/dev/null || true
rm -rf "$STATE/L2.offset.lock.d"

step "a DEAD-pid dir: FRESH => kept; STALE => reclaimed at once"
DEAD=$(bash -c 'echo $$'); # a pid that has already exited
mkdir -p "$STATE/L3.offset.lock.d"; printf '%s' "$DEAD" >"$STATE/L3.offset.lock.d/pid"
kb_lock_take L3; rc=$?
assert_eq "1" "$rc" "a dead-pid but FRESH dir is NOT reclaimed (no age-only reclaim)"
touch -d '3 hours ago' "$STATE/L3.offset.lock.d" 2>/dev/null || touch -t 202001010000 "$STATE/L3.offset.lock.d"
kb_lock_take L3; rc=$?
assert_eq "0" "$rc" "a dead-pid STALE dir is reclaimed"
assert_eq "$$" "$(cat "$STATE/L3.offset.lock.d/pid")" "the reclaimed dir is now ours"
kb_lock_release

step "kb_lock_owned is FALSE when another pid owns the dir (the drain's lock-loss gate)"
kb_lock_take L4 >/dev/null; assert_eq "0" "$?" "take L4"
printf '999999' >"$STATE/L4.offset.lock.d/pid"   # someone else reclaimed it
kb_lock_owned; assert_eq "1" "$?" "owned is false once the pid file names another process"
trap - EXIT INT TERM
rm -rf "$STATE/L4.offset.lock.d"

step "a TERM'd holder EXITS (143) and its EXIT trap removes its own dir"
R=$(bash -c '. "$HOME/trio.sh"; kb_lock_take L5 || exit 8; kill -TERM $$; sleep 2; echo survived' 2>/dev/null; echo "rc=$?")
assert_eq "rc=143" "$R" "TERM -> exit 143, the holder did not carry on"
assert_file_missing "$STATE/L5.offset.lock.d" "the EXIT trap removed the TERM'd holder's dir"

# ------------------------------------------------ end-to-end: held lock => busy
step "a live firing against a held dir does NOTHING and logs no NOT CAPTURED"
WORK="$HOME/work/wl"; mkdir -p "$WORK"
T="$WORK/t.jsonl"; mk_transcript "$T" 30 wl
S=$(wc -c <"$T")
sleep 300 & HOLD2=$!
OKEY=sess-wl
mkdir -p "$STATE/$OKEY.offset.lock.d"; printf '%s' "$HOLD2" >"$STATE/$OKEY.offset.lock.d/pid"
printf '100' >"$STATE/$OKEY.offset"
: >"$LOGF"
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" "$OKEY" PreCompact)"
assert_eq "0" "$RC" "the firing exits 0 while the lock is held"
settle; settle
assert_eq "0" "$(stub_total)" "a held lock drained nothing"
assert_not_contains "$(cat "$LOGF" 2>/dev/null)" "NOT CAPTURED" "a busy lock is NOT a NOT CAPTURED (offset survives, resume later)"
assert_eq "100" "$(cat "$STATE/$OKEY.offset")" "the offset is untouched by a busy PreCompact"
step "SessionEnd against a held dir is DEFERRED (logged), not NOT CAPTURED; offset untouched"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" "$OKEY" SessionEnd)"
wait_log "SessionEnd deferred \(lock held" 20 || fail "no 'SessionEnd deferred (lock held' line: $(tail -2 "$LOGF" | tr '\n' '|')"
assert_not_contains "$(cat "$LOGF" 2>/dev/null)" "NOT CAPTURED" "SessionEnd under a held lock is not a NOT CAPTURED"
assert_eq "0" "$(stub_total)" "SessionEnd under a held lock sent nothing"
assert_eq "100" "$(cat "$STATE/$OKEY.offset")" "the offset is untouched by a deferred SessionEnd"
assert_eq "$HOLD2" "$(cat "$STATE/$OKEY.offset.lock.d/pid")" "the holder's dir was not stolen"
kill "$HOLD2" 2>/dev/null || true; wait "$HOLD2" 2>/dev/null || true
rm -rf "$STATE/$OKEY.offset.lock.d"
# positive control: with the dir gone the same firing drains (from 100 to EOF)
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" "$OKEY" PreCompact)"
wait_log "flush .* ok bytes=.* sid=sess-wl" 60 || fail "positive control: the freed session did not drain"
assert_eq "$S" "$(cat "$STATE/$OKEY.offset")" "positive control: drained to EOF once the dir was gone"
assert_file_missing "$STATE/$OKEY.offset.lock.d" "the child's own .lock.d is gone after its drain (released before the sweep)"

# ---------------------------------- end-to-end: a FROZEN real holder, no rewind
# A long multi-chunk drain (tiny chunks) opens a window in which the REAL child
# can be frozen mid-drain. Everything below reads the child's own pid file.
new_home winfrozen
cfg_rest
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"
STATE="$HOOKS/state"
W2="$HOME/work/frz"; mkdir -p "$W2"
T2="$W2/t.jsonl"; mk_big_transcript "$T2" 3000000
S2=$(wc -c <"$T2")
export KB_FLUSH_CHUNK=20000 KB_FLUSH_SEND_MAX=30000 KB_FLUSH_MAX=209715200
OK2=sess-frz
LD="$STATE/$OK2.offset.lock.d"
lc() { local n; n=$(grep -cE "$1" "$LOGF" 2>/dev/null); echo "${n:-0}"; } # log_count without its "0\n0" on a zero count
wait_pidfile() { # <dir> [timeout-s]
  local i=0; while [ "$i" -lt "$(( ${2:-20} * 20 ))" ]; do [ -s "$1/pid" ] && return 0; i=$((i + 1)); sleep 0.05; done; return 1
}
step "a frozen REAL holder is never reclaimed; after CONT exactly one drain, no rewind, no re-send"
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T2" "$W2" "$OK2" PreCompact)"
wait_pidfile "$LD" 20 || fail "the child never created its lock dir: $(ls "$STATE")"
CPID=$(cat "$LD/pid")
kill -STOP "$CPID" 2>/dev/null && pass "froze the draining child (pid $CPID)" || fail "could not freeze pid [$CPID]"
sleep 0.5
OFF_FROZEN=$(cat "$STATE/$OK2.offset" 2>/dev/null || echo 0)
ROWS_FROZEN=$(stub_total)
assert_le "$OFF_FROZEN" "$((S2 - 1))" "the drain was caught mid-way (offset $OFF_FROZEN of $S2)"
touch -d '3 hours ago' "$LD" 2>/dev/null || touch -t 202001010000 "$LD"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T2" "$W2" "$OK2" PreCompact)"
settle; settle
assert_eq "$CPID" "$(cat "$LD/pid")" "the second firing did NOT steal the frozen holder's dir (alive pid, back-dated mtime)"
assert_eq "$ROWS_FROZEN" "$(stub_total)" "the second firing sent nothing while the holder was frozen"
assert_eq "$OFF_FROZEN" "$(cat "$STATE/$OK2.offset" 2>/dev/null || echo 0)" "the offset did not move while the holder was frozen"
kill -CONT "$CPID" 2>/dev/null || fail "could not resume pid $CPID"
wait_log "flush .* ok bytes=.* sid=sess-frz" 240 || fail "the resumed holder never finished: $(tail -2 "$LOGF" | tr '\n' '|')"
assert_eq "1" "$(log_count ' ok bytes=.* sid=sess-frz')" "exactly ONE drain completed"
assert_eq "$S2" "$(cat "$STATE/$OK2.offset")" "offset == SIZE after the resumed drain"
stub_concat '.transport=="rest"' '.transcript' >"$HOME/re"
if cmp -s "$T2" "$HOME/re"; then pass "reassembly is byte-identical (no rewind, no re-send)"; else fail "reassembly differs: $(cmp "$T2" "$HOME/re" 2>&1 | head -1)"; fi
NCH=$(stub_count '.transport=="rest"')
assert_eq "$NCH" "$(stub_count '.transport=="rest" and .result.duplicate==false')" "every stub row is a NEW chunk (zero duplicates => nothing was re-sent)"
assert_eq "0" "$(lc 'lock lost')" "the holder never lost its lock"
assert_file_missing "$LD" "the child's own .lock.d is absent after the ok line (released before the state sweep)"

# ----------------------------------- end-to-end: the lock is LOST mid-drain
new_home winlost
cfg_rest
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"
STATE="$HOOKS/state"
W3="$HOME/work/lost"; mkdir -p "$W3"
T3="$W3/t.jsonl"; mk_big_transcript "$T3" 3000000
S3=$(wc -c <"$T3")
export KB_FLUSH_CHUNK=20000 KB_FLUSH_SEND_MAX=30000
OK3=sess-lost
LD3="$STATE/$OK3.offset.lock.d"
step "a holder whose pid file is overwritten mid-drain logs 'lock lost — stopping' once and never advances again"
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T3" "$W3" "$OK3" PreCompact)"
wait_pidfile "$LD3" 20 || fail "the child never created its lock dir"
printf '999999' >"$LD3/pid"            # a "reclaimer" took the dir
wait_log "lock lost — stopping" 60 || fail "no 'lock lost — stopping' line: $(tail -2 "$LOGF" | tr '\n' '|')"
OFF_LOST=$(cat "$STATE/$OK3.offset" 2>/dev/null || echo 0)
settle; settle; settle
assert_eq "1" "$(log_count 'lock lost — stopping')" "exactly one 'lock lost — stopping' line"
assert_eq "$OFF_LOST" "$(cat "$STATE/$OK3.offset" 2>/dev/null || echo 0)" "the offset never advanced after the lock was lost"
assert_le "$OFF_LOST" "$((S3 - 1))" "the drain stopped short of EOF ($OFF_LOST of $S3)"
assert_eq "0" "$(lc ' ok bytes=.* sid=sess-los')" "no ok line from a drain that lost its lock"
assert_eq "999999" "$(cat "$LD3/pid" 2>/dev/null)" "the stopping holder did NOT remove a dir it no longer owns"
rm -rf "$LD3"

finish

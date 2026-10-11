#!/usr/bin/env bash
# CASE: STALE COPY -- a session resumed from ANOTHER directory is never
#       restarted from 0 (wave C.2, KB 49064).
# PINS: a stale <sid>.jsonl copy below the shared checkpoint is never re-sent (C.2)
#   Resuming session <sid> from a different cwd makes Claude Code keep TWO
#   transcripts named <sid>.jsonl under ~/.claude/projects: the frozen STALE one
#   (a byte-prefix of the live file) and the live one. The hooks key the
#   checkpoint by SESSION ID (flush-session-to-kb.sh OKEY; the sweep's bf_okey),
#   so both share state/<sid>.offset, and the live file's flushes advance it PAST
#   the stale file's size. Before C.2 the stale file therefore read as
#   `0 < SIZE < offset`: the sweep tabled it `shrank` (a fireable state), fired
#   it, and kb_decide_offset logged `reason=shrank — restarting from 0` and
#   re-sent it from byte 0; the live file's next firing then re-sent from the
#   stale copy's progress (rm1 bd162e9f: 411 rows / 282 MB, ~3,750 duplicates).
#   * HOOK (kb_decide_offset): a transcript smaller than its session's checkpoint
#     while ANOTHER <sid>.jsonl (another directory under the projects root) is
#     >= that checkpoint is a STALE COPY: exit 0, empty stdout, NO restart, NO
#     offset write (the file stays byte-identical), NOTHING sent, and ONE line
#     containing `stale copy` per 24 h -- on every event (SessionEnd, the
#     incident's Backfill) -- labelled `stale-copy` (`skipped`, NO marker) when
#     its tail is byte-identical to the covering copy at the same position, else
#     `stale-copy-diverged`: still nothing sent, but REFUSED visibly (`refused,
#     nothing sent, no restart`) with `state/<okey>.diverged` naming the copy
#     (wave C.2 review must-fix 1; case 46 pins the growing variant, notify and
#     the marker's clearing).
#   * SWEEP (kb-flush-backfill.sh): the dry-run table lists BOTH copies; the
#     stale one is `stale-copy` (a diverged one `diverged`) with a reason that
#     names the covering copy and says `no restart`; the live copy is `flushed`
#     at the checkpoint, `ready` past it. `--auto` (quiet window satisfied) never
#     fires a stale or diverged copy -- zero requests for that session,
#     checkpoint byte-identical -- while the SAME run still drains a live copy's
#     own delta (from the checkpoint, never from 0; the positive control).
#   * CONTROL (GREEN before AND after C.2): a SINGLE transcript truncated below
#     its checkpoint, with no other copy anywhere and its mtime set explicitly
#     NEWER than the checkpoint's (a rewrite, never a frozen `behind` copy --
#     case 46), keeps today's behaviour: one
#     `reason=shrank` / `restarting from 0` line, never a `stale copy` line, the
#     re-drain starts at offset 0 (flush_offset=0 on the first chunk) and
#     re-sends the truncated transcript byte for byte; truncated to a single
#     unterminated line, the 0 itself lands on disk (as case 29).
#   * --seed <sid> current with several copies seeds the LARGEST copy's size and
#     names the copy it chose (it used to be refused: "matches 2 transcripts").
#     The stale copy sorts FIRST in the glob and is the NEWER file, so neither a
#     first-match nor a newest-mtime pick passes.
# FIXTURES: the session started in $HOME/work/a-home and was resumed in
#   $HOME/work/binary-rewriter-project, so the stale copy lives in
#   projects/-home-tester-<case>-work-a-home/ and the live one in
#   projects/-home-tester-<case>-work-binary-rewriter-project/. Records carry a
#   timestamp and a cwd like real ones; every record names its session id, so
#   two sessions' first lines never match (no accidental `branch of`). Every
#   direct hook firing runs with KB_SWEEP_INTERVAL=0 FOR THAT FIRING ONLY, so a
#   detached piggyback --auto cannot sweep the same projects dir behind the
#   case's back (it stays unset for the explicit --auto runs: 0 is the OFF switch).
# RED on today's hooks (HEAD 809dea7): the old hook restarts the stale copy from
#   0 and re-sends it; the old sweep tables it `shrank` and fires it; the old
#   --seed refuses an ambiguous sid. Expected FAIL lines:
#   A (prefix copy): "ZERO requests: the stale copy was not re-sent -- expected
#     [0], got [1]"; "the shared checkpoint is byte-identical (...) -- the
#     shared checkpoint moved: <live size> -> <stale size>"; "exactly ONE 'stale
#     copy' line -- expected [1], got [0]"; "...labelled stale-copy --
#     [stale-copy] not found in []"; "never 'restarting from 0' -- expected [0],
#     got [1]"; "...and never reason=shrank -- expected [0], got [1]"; on the
#     re-fires "still ONE 'stale copy' line (one per 24 h)" and "...after a
#     Backfill firing" (expected [1], got [0]), and -- because the FIRST firing
#     already re-sent -- "still ZERO requests across all three firings",
#     "the checkpoint is still byte-identical", "still never 'restarting from 0'".
#   A' (diverged copy): "ZERO requests: the diverged copy was not re-sent",
#     the checkpoint, "exactly ONE 'stale copy' line", "...labelled
#     stale-copy-diverged -- [stale-copy-diverged] not found in []", "never
#     'restarting from 0'", "a re-fire adds no second line", "still ZERO requests".
#   B (sweep): "the prefix stale copy reads stale-copy -- expected [stale-copy],
#     got [shrank]"; "...its reason names the covering copy -- [binary-rewriter-
#     project] not found in [checkpoint N is past the size: the hook restarts
#     from 0 (reason=shrank)]"; "...and says no restart"; "...and never
#     'restarts from 0' -- [restarts from 0] unexpectedly present"; "the diverged
#     stale copy reads stale-copy* -- got [shrank]"; after --auto "ZERO requests
#     for the prefix session -- expected [0], got [1]", "its checkpoint is
#     byte-identical", "positive control: the diverged session's first request
#     starts at the checkpoint (...) -- expected [<flushed>], got [0]",
#     "...and carries exactly the live delta", "...byte for byte", "no
#     'restarting from 0' anywhere in the run -- expected [0], got [2]", "after
#     the run the prefix copy still reads stale-copy -- expected [stale-copy],
#     got [flushed]".
#   C (control): GREEN on both -- no FAIL line.
#   RED on the C.2 snapshot before its review fixes (68b98c1), which skipped a
#   diverged copy silently and tabled it `stale-copy`: A' "...it says refused,
#   nothing sent, no restart -- [refused, nothing sent, no restart] not found";
#   "...and never skipped -- [skipped] unexpectedly present"; "state/<okey>.
#   diverged names the diverged copy"; B "the diverged stale copy reads diverged
#   -- expected [diverged], got [stale-copy]". Everything else GREEN there.
#   D (--seed): "--seed with two copies exits 0 (it was refused as ambiguous) --
#     expected [0], got [2]"; "seeded at the LARGEST copy's size -- expected
#     [<live>], got []"; "prints the seed line -- [seeded 5eed0045 offset=...]
#     not found in [kb-flush-backfill: --seed refused: ... matches 2
#     transcripts ...]"; "...and names the copy it chose"; "...and refuses
#     nothing -- [refused] unexpectedly present"; "the table: the seeded live
#     copy reads flushed -- expected [flushed], got [ready]"; "...and the stale
#     copy reads stale-copy -- expected [stale-copy], got [ready]".
. /rig/lib.sh

slug_of() { printf '%s' "$1" | tr '/' '-'; }

setup_home() { # <name> -- a clean-room REST box with a past trust stamp and the two cwds
  new_home "$1"
  stub_reset
  cfg_rest
  install_hooks >/dev/null
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

# The live file = the history written under ORIG + what the resumed session added
# under RESUMED. STALE_SIZE = the history's size (where the stale copy froze).
mk_resumed_session() { # <sid> -> LIVE STALE LIVE_SIZE STALE_SIZE
  LIVE="$LIVE_DIR/$1.jsonl"
  STALE="$STALE_DIR/$1.jsonl"
  mk_lines "$LIVE" 100 "$ORIG" "$1"
  STALE_SIZE=$(wc -c <"$LIVE")
  mk_lines "$LIVE" 30 "$RESUMED" "$1" a
  LIVE_SIZE=$(wc -c <"$LIVE")
}

fire_flush() { # <transcript> <cwd> <sid> <event> -- piggyback sweep OFF for this firing only
  KB_SWEEP_INTERVAL=0 fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$1" "$2" "$3" "$4")"
}

# Wait for whichever verdict the hook writes for a firing at a stale copy: the
# C.2 `stale copy` line, or the old shrank restart -- whose re-send is then
# followed to its summary line, so every request it makes is counted (RED is
# deterministic, never a race with the detached child).
await_verdict() { # <summary lines before this firing>
  wait_log "stale copy|restarting from 0" 30 || note "no 'stale copy' and no 'restarting from 0' line within 30 s"
  if [ "$(log_count 'restarting from 0')" -gt 0 ]; then wait_firing "$1" 60 || true; fi
  settle
  settle
}

same_offset() { # <saved copy> <offset file> <label>
  if cmp -s "$1" "$2"; then
    pass "$3"
  else
    fail "$3 -- the shared checkpoint moved: $(cat "$1" 2>/dev/null) -> $(cat "$2" 2>/dev/null)"
  fi
}

stale_line() { grep 'stale copy' "$LOGF" 2>/dev/null | tail -n1; }

run_bf() {
  OUT=$("$BF" "$@" 2>&1)
  RC=$?
  printf '%s\n' "$OUT" >"$HOME/bf.out"
}

# ======================================== A. the HOOK fired at a prefix copy
step "A. setup: the live copy flushes; its byte-prefix stale copy sits in the ORIGINAL directory"
setup_home c2hook
SID_A="bd162e9f-0000-4000-8000-000000000045"
mk_resumed_session "$SID_A"
fire_flush "$LIVE" "$RESUMED" "$SID_A" SessionEnd
wait_firing 0 60 || fail "setup: the live copy never flushed: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_ge "$(stub_total)" "1" "positive control: the live copy's flush reached the stub"
assert_eq "$LIVE_SIZE" "$(cat "$ST/$SID_A.offset" 2>/dev/null)" "setup: the shared checkpoint sits at the live copy's size"
mk_prefix_copy "$LIVE" "$STALE" "$STALE_SIZE"
if cmp -s -n "$STALE_SIZE" "$STALE" "$LIVE" && [ "$STALE_SIZE" -lt "$LIVE_SIZE" ]; then
  pass "fixture: the stale copy is a byte-PREFIX of the live one ($STALE_SIZE < $LIVE_SIZE)"
else
  fail "fixture: the stale copy is not a strict byte-prefix of the live one"
fi
cp "$ST/$SID_A.offset" "$HOME/offset.before"
stub_reset

step "A. SessionEnd naming the STALE copy: nothing sent, checkpoint byte-identical, ONE 'stale copy' line (stale-copy)"
fire_flush "$STALE" "$ORIG" "$SID_A" SessionEnd
assert_eq "0" "$RC" "the hook exits 0"
assert_eq "" "$OUT" "...and prints nothing"
await_verdict 1
assert_eq "0" "$(stub_total)" "ZERO requests: the stale copy was not re-sent"
same_offset "$HOME/offset.before" "$ST/$SID_A.offset" "the shared checkpoint is byte-identical ($LIVE_SIZE): no restart, no offset write"
assert_eq "1" "$(log_count 'stale copy')" "exactly ONE 'stale copy' line"
L=$(stale_line)
note "hook line: ${L#* flush }"
assert_contains "$L" "stale-copy" "...labelled stale-copy"
assert_not_contains "$L" "stale-copy-diverged" "...not stale-copy-diverged (its tail IS the live copy's bytes)"
assert_eq "0" "$(log_count 'restarting from 0')" "never 'restarting from 0'"
assert_eq "0" "$(log_count 'reason=shrank')" "...and never reason=shrank"
assert_file_missing "$ST/$SID_A.diverged" "a byte-prefix stale copy writes no diverged marker"
assert_file_missing "$ST/$SID_A.behind" "...and no behind marker"

step "A. fired again (SessionEnd, then the incident's Backfill): still ONE line, still nothing sent"
fire_flush "$STALE" "$ORIG" "$SID_A" SessionEnd
assert_eq "0" "$RC" "the re-fire exits 0"
settle
settle
assert_eq "1" "$(log_count 'stale copy')" "still ONE 'stale copy' line (one per 24 h)"
fire_flush "$STALE" "$ORIG" "$SID_A" Backfill
assert_eq "0" "$RC" "a Backfill firing exits 0"
settle
settle
assert_eq "1" "$(log_count 'stale copy')" "still ONE 'stale copy' line after a Backfill firing"
assert_eq "0" "$(stub_total)" "still ZERO requests across all three firings"
same_offset "$HOME/offset.before" "$ST/$SID_A.offset" "the checkpoint is still byte-identical"
assert_eq "0" "$(log_count 'restarting from 0')" "still never 'restarting from 0'"

# ======================================== A'. the HOOK fired at a DIVERGED copy
step "A'. a DIVERGED stale copy (same size, different tail): labelled stale-copy-diverged, nothing sent"
setup_home c2div
SID_V="d1fe0045-0000-4000-8000-000000000045"
mk_resumed_session "$SID_V"
fire_flush "$LIVE" "$RESUMED" "$SID_V" SessionEnd
wait_firing 0 60 || fail "setup: the live copy never flushed: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "$LIVE_SIZE" "$(cat "$ST/$SID_V.offset" 2>/dev/null)" "setup: the shared checkpoint sits at the live copy's size"
mk_prefix_copy "$LIVE" "$STALE" "$STALE_SIZE"
diverge_last_line "$STALE"
assert_eq "$STALE_SIZE" "$(wc -c <"$STALE" | tr -d ' ')" "fixture: the diverged copy keeps the stale size"
if cmp -s -n "$STALE_SIZE" "$STALE" "$LIVE"; then
  fail "fixture: the diverged copy is still a byte-prefix of the live one"
else
  pass "fixture: its tail differs from the live copy's bytes at the same position"
fi
assert_eq "$(head -n1 "$LIVE")" "$(head -n1 "$STALE")" "fixture: ...while its first line is the live copy's"
cp "$ST/$SID_V.offset" "$HOME/offset.before"
stub_reset
fire_flush "$STALE" "$ORIG" "$SID_V" SessionEnd
assert_eq "0" "$RC" "the hook exits 0"
assert_eq "" "$OUT" "...and prints nothing"
await_verdict 1
assert_eq "0" "$(stub_total)" "ZERO requests: the diverged copy was not re-sent"
same_offset "$HOME/offset.before" "$ST/$SID_V.offset" "the shared checkpoint is byte-identical ($LIVE_SIZE)"
assert_eq "1" "$(log_count 'stale copy')" "exactly ONE 'stale copy' line"
L=$(stale_line)
note "hook line: ${L#* flush }"
assert_contains "$L" "stale-copy-diverged" "...labelled stale-copy-diverged"
assert_contains "$L" "refused, nothing sent, no restart" "...it says refused, nothing sent, no restart"
assert_not_contains "$L" "skipped" "...and never skipped (a diverged copy is refused visibly)"
assert_file_contains "$ST/$SID_V.diverged" " $STALE size=$STALE_SIZE checkpoint=$LIVE_SIZE cover=$LIVE" \
  "state/<okey>.diverged names the diverged copy, its size, the checkpoint and the covering copy"
assert_eq "0" "$(log_count 'restarting from 0')" "never 'restarting from 0'"
fire_flush "$STALE" "$ORIG" "$SID_V" SessionEnd
settle
settle
assert_eq "1" "$(log_count 'stale copy')" "a re-fire adds no second line (one per 24 h)"
assert_eq "0" "$(stub_total)" "still ZERO requests"

# ======================================== B. the SWEEP: table + --auto
step "B. setup: two resumed sessions -- one live copy AT the checkpoint, one that grew past it"
setup_home c2sweep
SID_B="5ee90045-0000-4000-8000-000000000045" # prefix stale copy; live copy == checkpoint
SID_W="5ee9d1fe-0000-4000-8000-000000000045" # diverged stale copy; live copy grew after its flush
mk_resumed_session "$SID_B"
LIVE_B=$LIVE STALE_B=$STALE LIVE_SIZE_B=$LIVE_SIZE STALE_SIZE_B=$STALE_SIZE
fire_flush "$LIVE_B" "$RESUMED" "$SID_B" SessionEnd
wait_firing 0 60 || fail "setup: $SID_B's live copy never flushed: $(tail -3 "$LOGF" | tr '\n' '|')"
mk_resumed_session "$SID_W"
LIVE_W=$LIVE STALE_W=$STALE FLUSHED_W=$LIVE_SIZE STALE_SIZE_W=$STALE_SIZE
fire_flush "$LIVE_W" "$RESUMED" "$SID_W" SessionEnd
wait_firing 1 60 || fail "setup: $SID_W's live copy never flushed: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "$LIVE_SIZE_B" "$(cat "$ST/$SID_B.offset" 2>/dev/null)" "setup: $SID_B checkpointed at its live size"
assert_eq "$FLUSHED_W" "$(cat "$ST/$SID_W.offset" 2>/dev/null)" "setup: $SID_W checkpointed at its live size"
mk_lines "$LIVE_W" 10 "$RESUMED" "$SID_W" a # the live session kept going after that flush
GROWN_W=$(wc -c <"$LIVE_W" | tr -d ' ')
mk_prefix_copy "$LIVE_B" "$STALE_B" "$STALE_SIZE_B"
mk_prefix_copy "$LIVE_W" "$STALE_W" "$STALE_SIZE_W"
diverge_last_line "$STALE_W"
# The quiet window, the way case 33 satisfies it: every transcript long idle.
# The stale copies are the OLDER files, so an old sweep fires them first.
touch -d @1700000000 "$STALE_B" "$STALE_W"
touch -d @1700000100 "$LIVE_B" "$LIVE_W"
pin_sweep_clock "$(date +%s)"
cp "$ST/$SID_B.offset" "$HOME/offB.before"
stub_reset

step "B. the dry-run table: both copies listed, the stale ones read stale-copy (no restart)"
run_bf
assert_eq "0" "$RC" "the dry run exits 0"
assert_eq "2" "$(sweep_row_count "$HOME/bf.out" "$SID_B")" "the table lists BOTH copies of $SID_B"
assert_eq "stale-copy" "$(sweep_row_state "$HOME/bf.out" "$SID_B" "$STALE_SIZE_B")" "the prefix stale copy reads stale-copy"
R=$(sweep_row_reason "$HOME/bf.out" "$SID_B" "$STALE_SIZE_B")
note "reason: $R"
assert_contains "$R" "binary-rewriter-project" "...its reason names the covering copy"
assert_contains "$R" "no restart" "...and says no restart"
assert_not_contains "$R" "restarts from 0" "...and never 'restarts from 0'"
assert_eq "flushed" "$(sweep_row_state "$HOME/bf.out" "$SID_B" "$LIVE_SIZE_B")" "its live copy (size == checkpoint) reads flushed"
assert_eq "2" "$(sweep_row_count "$HOME/bf.out" "$SID_W")" "the table lists BOTH copies of $SID_W"
assert_eq "diverged" "$(sweep_row_state "$HOME/bf.out" "$SID_W" "$STALE_SIZE_W")" "the diverged stale copy reads diverged (never fired, its records unflushed)"
assert_eq "ready" "$(sweep_row_state "$HOME/bf.out" "$SID_W" "$GROWN_W")" "the live copy that grew past the checkpoint reads ready"
assert_eq "0" "$(stub_total)" "the dry run sent nothing"

step "B. --auto never fires a stale copy, and still drains the live copy's own delta"
run_bf --auto
assert_eq "0" "$RC" "--auto exits 0"
assert_eq "" "$OUT" "--auto prints nothing"
settle
assert_eq "0" "$(stub_count ".session_uri==\"session:$SID_B\"")" "ZERO requests for the prefix session"
same_offset "$HOME/offB.before" "$ST/$SID_B.offset" "its checkpoint is byte-identical ($LIVE_SIZE_B)"
assert_eq "$FLUSHED_W" "$(stub_field ".session_uri==\"session:$SID_W\"" '.flush_offset' | head -n1)" \
  "positive control: the diverged session's first request starts at the checkpoint ($FLUSHED_W), never at 0"
assert_eq "$((GROWN_W - FLUSHED_W))" "$(stub_field ".session_uri==\"session:$SID_W\"" '.transcript_bytes' | awk '{s+=$1} END{print s+0}')" \
  "...and carries exactly the live delta ($((GROWN_W - FLUSHED_W)) bytes), nothing of the stale copy"
tail -c +"$((FLUSHED_W + 1))" "$LIVE_W" >"$HOME/delta.want"
stub_concat ".session_uri==\"session:$SID_W\"" '.transcript' >"$HOME/delta.got"
if cmp -s "$HOME/delta.want" "$HOME/delta.got"; then
  pass "...byte for byte"
else
  fail "...byte for byte -- the stub's reassembly is not the live delta ($(wc -c <"$HOME/delta.got" | tr -d ' ') bytes)"
fi
assert_eq "$GROWN_W" "$(cat "$ST/$SID_W.offset" 2>/dev/null)" "the live copy drained to its EOF"
assert_eq "0" "$(log_count 'restarting from 0')" "no 'restarting from 0' anywhere in the run"
run_bf
assert_eq "stale-copy" "$(sweep_row_state "$HOME/bf.out" "$SID_B" "$STALE_SIZE_B")" "after the run the prefix copy still reads stale-copy"
assert_eq "flushed" "$(sweep_row_state "$HOME/bf.out" "$SID_W" "$GROWN_W")" "...and the drained live copy reads flushed"
unpin_sweep_clock

# ======================================== C. CONTROL: a genuine shrink, no copy
# GREEN on today's hooks too: with no other <sid>.jsonl anywhere this is the
# shrank restart case 29 pins, and C.2 must leave it exactly as it is.
step "C. CONTROL: a SINGLE transcript truncated below its checkpoint still restarts from 0"
setup_home c2ctl
SID_C="c0de0045-0000-4000-8000-000000000045"
T_C="$LIVE_DIR/$SID_C.jsonl"
mk_lines "$T_C" 60 "$RESUMED" "$SID_C"
FULL_C=$(wc -c <"$T_C" | tr -d ' ')
fire_flush "$T_C" "$RESUMED" "$SID_C" SessionEnd
wait_firing 0 60 || fail "setup: the transcript never flushed: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "$FULL_C" "$(cat "$ST/$SID_C.offset" 2>/dev/null)" "setup: checkpointed at its size"
assert_eq "1" "$(find "$PROJDIR" -name "$SID_C.jsonl" | wc -l | tr -d ' ')" "fixture: no other copy of $SID_C exists"
head -n 25 "$T_C" >"$HOME/trunc" && cat "$HOME/trunc" >"$T_C"
# Explicitly NEWER than the checkpoint's last write: a rewrite, never a frozen
# (`behind`) copy -- whatever second boundaries fall between the two writes.
touch -d @"$(($(stat -c %Y "$ST/$SID_C.offset") + 5))" "$T_C"
TRUNC_C=$(wc -c <"$T_C" | tr -d ' ')
stub_reset
fire_flush "$T_C" "$RESUMED" "$SID_C" PreCompact
wait_firing 1 60 || fail "the shrank re-drain never completed: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "1" "$(log_count 'reason=shrank.*restarting from 0')" "one 'reason=shrank — restarting from 0' line"
assert_file_contains "$LOGF" "transcript shrank ($TRUNC_C < offset $FULL_C)" "...naming the size and the checkpoint"
assert_eq "0" "$(log_count 'stale copy')" "a genuine shrink is never called a stale copy"
assert_eq "0" "$(stub_field ".session_uri==\"session:$SID_C\"" '.flush_offset' | head -n1)" "the re-drain starts at offset 0 (flush_offset of the first chunk)"
stub_concat ".session_uri==\"session:$SID_C\"" '.transcript' >"$HOME/ctl.got"
if cmp -s "$T_C" "$HOME/ctl.got"; then
  pass "...and re-sends the truncated transcript byte for byte"
else
  fail "...and re-sends the truncated transcript byte for byte -- got $(wc -c <"$HOME/ctl.got" | tr -d ' ') of $TRUNC_C bytes"
fi
assert_eq "$TRUNC_C" "$(cat "$ST/$SID_C.offset" 2>/dev/null)" "the checkpoint follows to the new EOF"
step "C. CONTROL: truncated to one unterminated line, the restart's 0 lands on disk"
printf '{"type":"user","half":' >"$T_C"
touch -d @"$(($(stat -c %Y "$ST/$SID_C.offset") + 5))" "$T_C"
HALF_C=$(wc -c <"$T_C" | tr -d ' ')
B_C=$(stub_total)
fire_flush "$T_C" "$RESUMED" "$SID_C" PreCompact
wait_log "transcript shrank \\($HALF_C < offset $TRUNC_C\\)" 30 || fail "no shrank line for the second truncation"
settle
settle
assert_eq "0" "$(cat "$ST/$SID_C.offset" 2>/dev/null)" "the offset file reads 0"
assert_eq "$B_C" "$(stub_total)" "nothing was sent (the only line is unterminated)"
assert_eq "0" "$(log_count 'stale copy')" "still never a stale copy"
assert_file_missing "$ST/$SID_C.behind" "a genuine rewrite writes no behind marker"
assert_file_missing "$ST/$SID_C.diverged" "...and no diverged marker"

# ======================================== D. --seed with two copies
step "D. --seed <sid> current with two copies seeds the LARGEST and names it"
setup_home c2seed
SID_D="5eed0045-0000-4000-8000-000000000045"
mk_resumed_session "$SID_D"
mk_prefix_copy "$LIVE" "$STALE" "$STALE_SIZE"
# The stale copy sorts FIRST in the glob AND is the newer file: only "largest" is right.
touch -d @1700000100 "$LIVE"
touch -d @1700000200 "$STALE"
assert_file_missing "$ST/$SID_D.offset" "precondition: no checkpoint yet"
run_bf --seed "$SID_D" current
assert_eq "0" "$RC" "--seed with two copies exits 0 (it was refused as ambiguous)"
assert_eq "$LIVE_SIZE" "$(cat "$ST/$SID_D.offset" 2>/dev/null)" "seeded at the LARGEST copy's size ($LIVE_SIZE, not $STALE_SIZE)"
assert_contains "$OUT" "seeded ${SID_D:0:8} offset=$LIVE_SIZE (snapped from $LIVE_SIZE)" "prints the seed line"
assert_contains "$OUT" "binary-rewriter-project" "...and names the copy it chose (the live one)"
assert_not_contains "$OUT" "refused" "...and refuses nothing"
run_bf
assert_eq "flushed" "$(sweep_row_state "$HOME/bf.out" "$SID_D" "$LIVE_SIZE")" "the table: the seeded live copy reads flushed"
assert_eq "stale-copy" "$(sweep_row_state "$HOME/bf.out" "$SID_D" "$STALE_SIZE")" "...and the stale copy reads stale-copy"
assert_eq "0" "$(stub_total)" "a seed sends nothing"

finish

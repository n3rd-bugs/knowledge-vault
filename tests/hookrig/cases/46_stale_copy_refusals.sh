#!/usr/bin/env bash
# CASE: STALE-COPY REFUSALS -- a DIVERGED copy and a FROZEN copy BEHIND the
#       shared checkpoint are refused visibly, never restarted from 0; the
#       markers that say so clear only by the documented paths (wave C.2
#       review fixes, KB 49064).
# PINS (flush-session-to-kb.sh kb_stale_copy_skip / kb_marker_gone_clear,
#   the '# >>> KB SESSION COPIES v1' kb_shrink_verdict shared with
#   kb-flush-backfill.sh, notify-complete.sh kb_progress):
#   1. GROWING / DIVERGED. The live copy flushed the shared checkpoint to its
#      size; copy A in the ORIGINAL project dir is a byte-prefix of it that was
#      then RESUMED there (records appended), so A is behind the checkpoint AND
#      its tail differs from the live copy at the same position. Its appended
#      records were never sent and the shared checkpoint cannot carry them.
#      * HOOK (SessionEnd, then PreCompact): exit 0, empty stdout, ZERO
#        requests, the checkpoint byte-identical, never `restarting from 0`;
#        `state/<okey>.diverged` names A (rewritten on EVERY firing); exactly
#        ONE `reason=stale-copy-diverged` line (24 h stamp) that says `refused,
#        nothing sent, no restart` -- not the stale-copy `skipped`.
#      * SWEEP: the dry-run row for A is `diverged` with REMAIN = A's whole size
#        and a reason saying `a --seed cannot settle this while a copy reaches
#        the checkpoint` and naming the owner's remedy, `move this copy out of
#        ~/.claude/projects` -- never a `kb-flush-backfill.sh --seed` (review 2
#        must-fix 2); the summary counts `1 diverged copy(ies)`; `--auto` (quiet window satisfied, as
#        case 33) fires nothing for the session, and `.sweep-last` carries
#        `diverged=1`, `stale=0` and `unflushed=<A's size>`.
#      * NOTIFY: a Stop ping for A AND for the live copy says `kb ?/<size>
#        diverged`, never `synced`.
#      * --seed <sid> <checkpoint> keeps the checkpoint, removes the marker and
#        its stamp, and says `cleared: state/<okey>.diverged (it named <A>)` on
#        stdout and `cleared: diverged` in its log line -- AND `note: <A> is
#        still behind the checkpoint and another copy reaches it -- the next
#        sweep or firing marks it again; move it out ...` (review 2 must-fix 2).
#      * The documented re-mark: the next `--auto` writes the marker again
#        (`.sweep-last` `diverged=1`), the table still names the move, no seed.
#      * The remedy: A moved OUT of ~/.claude/projects, the next `--auto` clears
#        the marker with ONE `backfill <sid8> ... marker cleared: ...` line,
#        `.sweep-last` `diverged=0`, and notify for the live copy reads `kb
#        <size> synced`, never `diverged`.
#   2. LIVE COPY REMOVED / BEHIND. Two sessions (H: the hook rules first; S: the
#      sweep rules first) whose live copy flushed and was then REMOVED, leaving
#      only the frozen byte-prefix copy, its mtime set explicitly BEFORE the
#      checkpoint's last write (relative to the offset file's own mtime -- the
#      offset file itself is never touched).
#      * HOOK at H (SessionEnd, then PreCompact): refused -- ZERO requests,
#        checkpoint byte-identical, no restart, `state/<okey>.behind` names the
#        file, ONE `reason=behind` line naming `--seed <sid> current|<bytes>`.
#      * SWEEP: both rows `behind` (REMAIN 0; H's reason: the marker names it,
#        S's: the file is frozen), `2 behind the checkpoint`; a dry run writes
#        no marker; `--auto` fires neither, `.sweep-last` `behind=2`
#        `unflushed=0`, and it WRITES S's marker.
#      * RESUMED after the live copy is gone (records appended, mtime now NEWER
#        than the checkpoint): still refused because the marker names the path
#        -- zero requests, no restart, the marker rewritten with the new size;
#        S (whose marker the SWEEP wrote) logs its first `reason=behind` line,
#        `... state/<okey>.behind names this file` naming the exact seed
#        `--seed <sid> <frozen>`; the table still says behind, H's REMAIN =
#        resumed size - frozen and never `current (its bytes were sent`. Both
#        markers keep `frozen=` the size they were first written at (review 2
#        must-fix 1; case 47 pins the copy grown PAST the checkpoint).
#   3. CLEARING. H's frozen copy removed and its live copy restored: the next
#      hook firing logs ONE `marker cleared: state/<okey>.behind named <path>,
#      which no longer exists` and removes marker + stamp (nothing sent: the
#      restored copy is at the checkpoint). S's frozen copy removed: a dry run
#      leaves the marker, the next `--auto` pass removes it and logs `backfill
#      <sid8> <proj> marker cleared: ...`. (--seed clearing: leg 1.)
#   4. CONTROL (GREEN before AND after the fixes): a SINGLE transcript rewritten
#      smaller AFTER the checkpoint (mtime set explicitly newer, no copy, no
#      marker) tables `shrank`, logs ONE `reason=shrank -- restarting from 0`,
#      re-sends from flush_offset 0 byte for byte, never writes a marker; cut to
#      one unterminated line, the 0 lands on disk.
# FIXTURES: as case 45 -- the session started in $HOME/work/a-home (the stale
#   copy's dir sorts first) and was resumed in $HOME/work/binary-rewriter-project;
#   every record names its session id. Every direct hook firing runs with
#   KB_SWEEP_INTERVAL=0 FOR THAT FIRING ONLY (no piggyback --auto behind the
#   case's back). Notify runs with a recording `curl` first on ITS PATH only
#   (the hook's REST transport keeps the real curl), exactly as case 39 records.
# RED on the C.2 snapshot before the review fixes (68b98c1; KB_SKILL_DIR=
#   /home/usama/data-server/contrib/claude/kb-session-flush), whose hook skipped
#   only a copy some OTHER copy covers (a diverged one as `skipped`, no marker)
#   and restarted anything else from 0, and whose sweep tabled a covered
#   diverged copy `stale-copy` and an uncovered one `shrank`:
#   (author's dry run OUTSIDE docker -- this script under a sandbox HOME, a
#   local stub_server.py, GNU-less uutils host: 68 of 142 checks FAIL there,
#   0 on the fixed hooks; the rig run is the owner's):
#   1: "...it says refused, nothing sent, no restart"; "...and names the
#      marker"; "...and never skipped"; "state/<okey>.diverged names A";
#      "state/<okey>.diverged was rewritten by the PreCompact firing" and
#      "...naming A again"; "the row for A reads diverged -- got [stale-copy]";
#      "...REMAIN is A's whole size -- got [0]"; "...says its records past the
#      shared prefix were not sent"; "...and names the owner's --seed"; "the
#      summary counts it"; ".sweep-last diverged=1 -- got []", "stale=0 -- got
#      [1]", "unflushed=<A> -- got [0]"; both notify "... diverged -- [kb 47KB
#      synced]" and "...never synced"; "precondition: the marker exists", "it
#      prints cleared:", "the seed's log line says cleared: diverged". GREEN
#      there too: exit codes, zero requests, the checkpoint, no restart, the
#      one-line count, --auto firing nothing (the old sweep tabled it
#      stale-copy), the seed's offset.
#   2: every hook assertion at H but the exit codes (the old hook restarts the
#      frozen copy from 0 and re-sends it: 1 request, the checkpoint moves to
#      the copy's size, `restarting from 0`, no marker, no `reason=behind`);
#      the table ("H ... got [flushed]" after that re-send, "S ... got
#      [shrank]", the reasons, "REMAIN 0 -- got [<size>]", "2 behind the
#      checkpoint", and "the dry run sent nothing" -- the H re-send above);
#      "--auto fires nothing for S -- got [1]", both checkpoints, "behind=2 --
#      got []", "unflushed=0 -- got [<size>]", S's marker, "no 'restarting
#      from 0' anywhere -- got [2]"; after the resume every check (the old hook
#      drains the appended delta from the moved checkpoint). GREEN there: "a
#      dry run writes no marker for S", "...nor for H" (already re-sent).
#   3: "precondition: H's marker exists", "ONE 'marker cleared' line",
#      "...naming the gone path" (both legs), "nothing was sent" (both: the old
#      checkpoints sit below the restored/resumed sizes), "the dry run leaves
#      S's marker", "S's checkpoint is unchanged". The "marker/stamp gone"
#      checks are GREEN there (there never was one) -- the preconditions carry
#      the RED.
#   4: GREEN on both -- no FAIL line.
# RED on f89f61c (the first review-fix round; review 2's additions -- author's
#   run outside docker as above: 12 of 168 checks FAIL there, 0 on the fixed
#   hooks): 1: the reason's "a --seed cannot settle it", "...move the copy
#   out", "...never offering a --seed" (it named `--seed <sid> <checkpoint>`),
#   the seed's `note:` line, and after the re-mark "...names moving the copy
#   out", "...offers no --seed". GREEN there: the re-mark itself, the move-out
#   clearing and notify's `synced` (the files already decided; the leg pins the
#   documented sequence). 2: every `frozen=` check, S's exact-seed line, H's
#   REMAIN (`got [0]`) and its reason's `current`.
. /rig/lib.sh

slug_of() { printf '%s' "$1" | tr '/' '-'; }
mtime_of() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }
size_of() { wc -c <"$1" | tr -d ' '; }
# The rounding notify's kb_human uses (%.0f at KB, %.1f at MB), as case 39.
human_of() { awk -v s="$1" 'BEGIN { if (s >= 1048576) printf "%.1fMB", s/1048576; else printf "%.0fKB", s/1024 }'; }

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
  STALE_SIZE=$(size_of "$LIVE")
  mk_lines "$LIVE" 30 "$RESUMED" "$1" a
  LIVE_SIZE=$(size_of "$LIVE")
}

fire_flush() { # <transcript> <cwd> <sid> <event> -- piggyback sweep OFF for this firing only
  KB_SWEEP_INTERVAL=0 fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$1" "$2" "$3" "$4")"
}
summaries() { log_count ' (ok|partial) bytes='; }

# Wait for whichever verdict the hook writes for a firing at a copy behind the
# checkpoint: the `stale copy:` refusal line, or the old shrank restart -- whose
# re-send is then followed to its summary line, so every request it makes is
# counted (RED is deterministic, never a race with the detached child).
await_verdict() { # <sid8> <summary lines before this firing>
  wait_log "stale copy: .* sid=$1\$|restarting from 0 .*sid=$1\$" 30 ||
    note "no 'stale copy' and no 'restarting from 0' line for $1 within 30 s"
  if [ "$(log_count "restarting from 0 .*sid=$1\$")" -gt 0 ]; then wait_firing "$2" 60 || true; fi
  settle
  settle
}
# A refusal that logs NOTHING (the 24 h stamp is fresh) still rewrites its
# marker: wait for the marker to name <size>. An old hook sends instead -- its
# summary line ends the wait just as deterministically.
await_marker() { # <marker> <size> <summary lines before this firing>
  local i=0
  while [ "$i" -lt 120 ]; do
    grep -q " size=$2 checkpoint=" "$1" 2>/dev/null && break
    [ "$(summaries)" -gt "$3" ] && break
    i=$((i + 1))
    sleep 0.25
  done
  [ "$i" -lt 120 ] || note "neither the marker $1 naming size=$2 nor a summary line within 30 s"
  settle
}
await_file() { # <path> [timeout-s]
  local i=0 t="${2:-30}"
  while [ "$i" -lt "$((t * 4))" ]; do
    [ -e "$1" ] && return 0
    i=$((i + 1))
    sleep 0.25
  done
  return 1
}

same_offset() { # <saved copy> <offset file> <label>
  if cmp -s "$1" "$2"; then
    pass "$3"
  else
    fail "$3 -- the shared checkpoint moved: $(cat "$1" 2>/dev/null) -> $(cat "$2" 2>/dev/null)"
  fi
}
assert_file_exists() { if [ -e "$1" ]; then pass "$2"; else fail "$2 -- $1 does not exist"; fi; }
last_line() { grep -E "$1" "$LOGF" 2>/dev/null | tail -n1; }

run_bf() {
  OUT=$("$BF" "$@" 2>&1)
  RC=$?
  printf '%s\n' "$OUT" >"$HOME/bf.out"
}
sweep_last() { sed -n "s/.* $1=\([0-9]*\).*/\1/p" "$ST/.sweep-last" 2>/dev/null | head -1; }

# notify-complete.sh with a curl that RECORDS the ntfy body (case 39's recorder),
# first on notify's PATH only -- the flush hook's REST transport keeps real curl.
notify_setup() {
  mkdir -p "$HOME/nbin"
  cat >"$HOME/nbin/curl" <<'EOF'
#!/usr/bin/env bash
prev=""
for a in "$@"; do [ "$prev" = "-d" ] && printf '%s\n' "$a" >>"$HOME/pushed"; prev="$a"; done
cat >/dev/null
exit 0
EOF
  chmod +x "$HOME/nbin/curl"
  printf 'NTFY_URL=http://127.0.0.1:9\nNTFY_TOPIC=rig-topic\nNTFY_MACHINE=rigbox\n' >"$HOME/.claude/notify.env"
}
ping_notify() { # <transcript> <cwd> <sid> -> the recorded body in $BODY
  rm -f "$HOME/pushed"
  PATH="$HOME/nbin:$PATH" fire "$HOOKS/notify-complete.sh" \
    "$(flush_event "$1" "$2" "$3" Stop)"
  BODY=$(cat "$HOME/pushed" 2>/dev/null)
}

# ===================================================== 1. GROWING / DIVERGED copy
step "1. setup: the live copy flushes; copy A (its byte-prefix, in the ORIGINAL dir) is then resumed there"
setup_home c2grow
SID_G="d1f60046-0000-4000-8000-000000000046"
G8=${SID_G:0:8}
mk_resumed_session "$SID_G"
LIVE_G=$LIVE A_G=$STALE LIVE_SIZE_G=$LIVE_SIZE PREFIX_G=$STALE_SIZE
fire_flush "$LIVE_G" "$RESUMED" "$SID_G" SessionEnd
wait_firing 0 60 || fail "setup: the live copy never flushed: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "$LIVE_SIZE_G" "$(cat "$ST/$SID_G.offset" 2>/dev/null)" "setup: the shared checkpoint sits at the live copy's size"
mk_prefix_copy "$LIVE_G" "$A_G" "$PREFIX_G"
mk_lines "$A_G" 5 "$ORIG" "$SID_G" a # resumed in ORIG: its OWN records past the shared prefix
A_SIZE=$(size_of "$A_G")
if [ "$A_SIZE" -gt "$PREFIX_G" ] && [ "$A_SIZE" -lt "$LIVE_SIZE_G" ]; then
  pass "fixture: A grew past the shared prefix and is still behind the checkpoint ($PREFIX_G < $A_SIZE < $LIVE_SIZE_G)"
else
  fail "fixture: A is $A_SIZE bytes -- not between the prefix $PREFIX_G and the checkpoint $LIVE_SIZE_G"
fi
if cmp -s -n "$PREFIX_G" "$A_G" "$LIVE_G" && ! cmp -s -n "$A_SIZE" "$A_G" "$LIVE_G"; then
  pass "fixture: A shares the prefix byte for byte and its tail differs from the live copy"
else
  fail "fixture: A is not <shared prefix> + <its own tail>"
fi
cp "$ST/$SID_G.offset" "$HOME/offG.before"
stub_reset

step "1. HOOK, SessionEnd at A: refused visibly -- nothing sent, no restart, state/<okey>.diverged"
N0=$(summaries)
fire_flush "$A_G" "$ORIG" "$SID_G" SessionEnd
assert_eq "0" "$RC" "the hook exits 0"
assert_eq "" "$OUT" "...and prints nothing"
await_verdict "$G8" "$N0"
assert_eq "0" "$(stub_total)" "ZERO requests: A was not sent (nor restarted)"
same_offset "$HOME/offG.before" "$ST/$SID_G.offset" "the shared checkpoint is byte-identical ($LIVE_SIZE_G)"
assert_eq "0" "$(log_count "restarting from 0 .*sid=$G8\$")" "never 'restarting from 0'"
assert_eq "1" "$(log_count "reason=stale-copy-diverged .*sid=$G8\$")" "exactly ONE reason=stale-copy-diverged line"
L=$(last_line "reason=stale-copy-diverged .*sid=$G8\$")
note "hook line: ${L#* flush }"
assert_contains "$L" "refused, nothing sent, no restart" "...it says refused, nothing sent, no restart"
assert_contains "$L" "(state/$SID_G.diverged)" "...and names the marker"
assert_not_contains "$L" "skipped" "...and never skipped (that word is the silent stale-copy's)"
assert_file_contains "$ST/$SID_G.diverged" " $A_G size=$A_SIZE checkpoint=$LIVE_SIZE_G cover=$LIVE_G" \
  "state/<okey>.diverged names A, its size, the checkpoint and the covering copy"

step "1. HOOK, PreCompact at A: the marker is rewritten, the line stays ONE (24 h), still nothing sent"
rm -f "$ST/$SID_G.diverged" # the marker is (re)written on EVERY refusing firing: prove it
fire_flush "$A_G" "$ORIG" "$SID_G" PreCompact
assert_eq "0" "$RC" "the PreCompact firing exits 0"
assert_eq "" "$OUT" "...and prints nothing"
if await_file "$ST/$SID_G.diverged" 30; then
  pass "state/<okey>.diverged was rewritten by the PreCompact firing"
else
  fail "state/<okey>.diverged was rewritten by the PreCompact firing -- no marker within 30 s"
fi
settle
settle
assert_file_contains "$ST/$SID_G.diverged" " $A_G size=$A_SIZE checkpoint=$LIVE_SIZE_G" "...naming A again"
assert_eq "1" "$(log_count "reason=stale-copy-diverged .*sid=$G8\$")" "still ONE reason=stale-copy-diverged line (one per 24 h)"
assert_eq "0" "$(stub_total)" "still ZERO requests"
same_offset "$HOME/offG.before" "$ST/$SID_G.offset" "the checkpoint is still byte-identical"
assert_eq "0" "$(log_count "restarting from 0 .*sid=$G8\$")" "still never 'restarting from 0'"

step "1. SWEEP dry run: A reads diverged (REMAIN = its whole size); the live copy reads flushed"
# The quiet window, the way case 33 satisfies it: both copies long idle.
touch -d @1700000000 "$A_G"
touch -d @1700000100 "$LIVE_G"
pin_sweep_clock "$(date +%s)"
run_bf
assert_eq "0" "$RC" "the dry run exits 0"
assert_eq "2" "$(sweep_row_count "$HOME/bf.out" "$SID_G")" "the table lists BOTH copies"
assert_eq "diverged" "$(sweep_row_state "$HOME/bf.out" "$SID_G" "$A_SIZE")" "the row for A reads diverged"
assert_eq "$A_SIZE" "$(sweep_row_remain "$HOME/bf.out" "$SID_G" "$A_SIZE")" "...REMAIN is A's whole size (its unique bytes are unknown without a full compare)"
R=$(sweep_row_reason "$HOME/bf.out" "$SID_G" "$A_SIZE")
note "reason: $R"
assert_contains "$R" "binary-rewriter-project" "...its reason names the copy that reaches the checkpoint"
assert_contains "$R" "NOT sent" "...says its records past the shared prefix were not sent"
assert_contains "$R" "a --seed cannot settle this while a copy reaches the checkpoint" "...says a --seed cannot settle it while the live copy reaches the checkpoint"
assert_contains "$R" "move this copy out of ~/.claude/projects (the marker then clears)" "...and names the owner's remedy: move the copy out"
assert_not_contains "$R" "kb-flush-backfill.sh --seed" "...never offering a --seed (the next pass would re-mark it)"
assert_eq "flushed" "$(sweep_row_state "$HOME/bf.out" "$SID_G" "$LIVE_SIZE_G")" "the live copy (size == checkpoint) reads flushed"
assert_contains "$OUT" "1 diverged copy(ies)" "the summary counts it"
assert_eq "0" "$(stub_total)" "the dry run sent nothing"

step "1. SWEEP --auto: fires nothing for the session; .sweep-last counts diverged=1 and its bytes as unflushed"
run_bf --auto
assert_eq "0" "$RC" "--auto exits 0"
assert_eq "" "$OUT" "--auto prints nothing"
settle
assert_eq "0" "$(stub_count ".session_uri==\"session:$SID_G\"")" "ZERO requests for the session"
same_offset "$HOME/offG.before" "$ST/$SID_G.offset" "its checkpoint is byte-identical"
assert_eq "1" "$(sweep_last diverged)" ".sweep-last diverged=1"
assert_eq "0" "$(sweep_last stale)" ".sweep-last stale=0 (a diverged copy is not a silent stale one)"
assert_eq "$A_SIZE" "$(sweep_last unflushed)" ".sweep-last unflushed=$A_SIZE (A's whole size; the live copy is flushed)"
assert_eq "0" "$(log_count "restarting from 0")" "no 'restarting from 0' anywhere"
unpin_sweep_clock

step "1. NOTIFY: the session reads ?/<size> diverged, never synced"
notify_setup
ping_notify "$A_G" "$ORIG" "$SID_G"
assert_eq "0" "$RC" "notify exits 0"
note "body: $BODY"
assert_contains "$BODY" "kb ?/$(human_of "$A_SIZE") diverged" "a ping for A says ?/<size> diverged"
assert_not_contains "$BODY" "synced" "...never synced"
ping_notify "$LIVE_G" "$RESUMED" "$SID_G"
note "body: $BODY"
assert_contains "$BODY" "kb ?/$(human_of "$LIVE_SIZE_G") diverged" "a ping for the live copy says diverged too (the session's capture is not what its checkpoint says)"
assert_not_contains "$BODY" "synced" "...never synced"

step "1. --seed <sid> <checkpoint>: the owner accepts the loss -- the checkpoint stays, the marker clears, said on stdout"
assert_file_exists "$ST/$SID_G.diverged" "precondition: the marker exists"
run_bf --seed "$SID_G" "$LIVE_SIZE_G"
assert_eq "0" "$RC" "--seed exits 0"
note "seed: $(printf '%s' "$OUT" | tr '\n' '|')"
assert_contains "$OUT" "seeded $G8 offset=$LIVE_SIZE_G (snapped from $LIVE_SIZE_G)" "it seeds at the checkpoint (the largest copy's line end)"
assert_contains "$OUT" "cleared: state/$SID_G.diverged (it named $A_G)" "it prints cleared: naming the marker and the copy"
same_offset "$HOME/offG.before" "$ST/$SID_G.offset" "the checkpoint is byte-identical"
assert_file_missing "$ST/$SID_G.diverged" "the marker is gone"
assert_file_missing "$ST/$SID_G.diverged.stamp" "...and its log stamp"
assert_file_contains "$LOGF" "$G8 binary-rewriter-project seed offset=$LIVE_SIZE_G (was $LIVE_SIZE_G) size=$LIVE_SIZE_G cleared: diverged" "the seed's log line says cleared: diverged"
assert_contains "$OUT" "note: $A_G is still behind the checkpoint and another copy reaches it -- the next sweep or firing marks it again; move it out of ~/.claude/projects to accept the loss" \
  "...and a note: A is still covered, so the next pass marks it again (review 2 must-fix 2)"
assert_eq "0" "$(stub_total)" "a seed sends nothing"

step "1. --auto after the seed: the covered diverged copy is marked AGAIN (the files decide, not the marker)"
pin_sweep_clock "$(date +%s)"
run_bf --auto
assert_eq "0" "$RC" "--auto exits 0"
assert_file_contains "$ST/$SID_G.diverged" " $A_G size=$A_SIZE checkpoint=$LIVE_SIZE_G" "the marker is back, naming A"
assert_eq "1" "$(sweep_last diverged)" ".sweep-last diverged=1 again"
assert_eq "0" "$(stub_total)" "--auto sent nothing"
same_offset "$HOME/offG.before" "$ST/$SID_G.offset" "the checkpoint is byte-identical"
run_bf
R=$(sweep_row_reason "$HOME/bf.out" "$SID_G" "$A_SIZE")
assert_eq "diverged" "$(sweep_row_state "$HOME/bf.out" "$SID_G" "$A_SIZE")" "the table still reads diverged for A"
assert_contains "$R" "move this copy out of ~/.claude/projects" "...its reason names moving the copy out"
assert_not_contains "$R" "kb-flush-backfill.sh --seed" "...and offers no --seed"
unpin_sweep_clock

step "1. A moved OUT of ~/.claude/projects (the owner accepts the loss): --auto clears the marker, ONE line; notify stops saying diverged"
mkdir -p "$HOME/kb-quarantine"
mv "$A_G" "$HOME/kb-quarantine/"
pin_sweep_clock "$(date +%s)"
run_bf --auto
unpin_sweep_clock
assert_eq "0" "$RC" "--auto exits 0"
assert_file_missing "$ST/$SID_G.diverged" "the marker is gone"
assert_eq "1" "$(log_count "backfill $G8 .*marker cleared: state/$SID_G\\.diverged named ")" "ONE 'backfill <sid8> ... marker cleared' line"
assert_file_contains "$LOGF" "marker cleared: state/$SID_G.diverged named $A_G, which no longer exists" "...naming the moved path"
assert_eq "0" "$(sweep_last diverged)" ".sweep-last diverged=0"
assert_eq "0" "$(stub_total)" "nothing was sent"
same_offset "$HOME/offG.before" "$ST/$SID_G.offset" "the checkpoint is byte-identical"
ping_notify "$LIVE_G" "$RESUMED" "$SID_G"
note "body: $BODY"
assert_not_contains "$BODY" "diverged" "notify for the live copy no longer says diverged"
assert_contains "$BODY" "kb $(human_of "$LIVE_SIZE_G") synced" "...it reads synced"

# ================================================ 2. LIVE COPY REMOVED / BEHIND
step "2. setup: two resumed sessions whose live copy flushed and was REMOVED; the frozen copies predate the checkpoint"
setup_home c2behind
SID_H="be41d046-0000-4000-8000-000000000046" # the HOOK rules first
SID_S="5be4d046-0000-4000-8000-000000000046" # the SWEEP rules first
H8=${SID_H:0:8}
S8=${SID_S:0:8}
mk_resumed_session "$SID_H"
LIVE_H=$LIVE STALE_H=$STALE LIVE_SIZE_H=$LIVE_SIZE STALE_SIZE_H=$STALE_SIZE
fire_flush "$LIVE_H" "$RESUMED" "$SID_H" SessionEnd
wait_firing 0 60 || fail "setup: $H8's live copy never flushed: $(tail -3 "$LOGF" | tr '\n' '|')"
mk_resumed_session "$SID_S"
LIVE_S=$LIVE STALE_S=$STALE LIVE_SIZE_S=$LIVE_SIZE STALE_SIZE_S=$STALE_SIZE
fire_flush "$LIVE_S" "$RESUMED" "$SID_S" SessionEnd
wait_firing 1 60 || fail "setup: $S8's live copy never flushed: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "$LIVE_SIZE_H" "$(cat "$ST/$SID_H.offset" 2>/dev/null)" "setup: $H8 checkpointed at its live size"
assert_eq "$LIVE_SIZE_S" "$(cat "$ST/$SID_S.offset" 2>/dev/null)" "setup: $S8 checkpointed at its live size"
mk_prefix_copy "$LIVE_H" "$STALE_H" "$STALE_SIZE_H"
mk_prefix_copy "$LIVE_S" "$STALE_S" "$STALE_SIZE_S"
cp "$LIVE_H" "$HOME/liveH.saved" # leg 3 restores it
rm -f "$LIVE_H" "$LIVE_S"
# FROZEN, explicitly: each copy's mtime an hour BEFORE its checkpoint's last
# write, read from the offset file's own mtime (never touched here).
touch -d @"$(($(mtime_of "$ST/$SID_H.offset") - 3600))" "$STALE_H"
touch -d @"$(($(mtime_of "$ST/$SID_S.offset") - 3600))" "$STALE_S"
for _s in "$SID_H" "$SID_S"; do
  assert_eq "1" "$(find "$PROJDIR" -name "$_s.jsonl" | wc -l | tr -d ' ')" "fixture: ${_s:0:8} has ONE transcript left (no copy reaches its checkpoint)"
done
if [ "$(mtime_of "$STALE_H")" -lt "$(mtime_of "$ST/$SID_H.offset")" ] && [ "$(mtime_of "$STALE_S")" -lt "$(mtime_of "$ST/$SID_S.offset")" ]; then
  pass "fixture: both frozen copies were last modified before their checkpoint's last write"
else
  fail "fixture: a frozen copy is not older than its checkpoint"
fi
cp "$ST/$SID_H.offset" "$HOME/offH.before"
cp "$ST/$SID_S.offset" "$HOME/offS.before"
stub_reset

step "2. HOOK, SessionEnd at H's frozen copy: refused -- nothing sent, no restart, state/<okey>.behind, ONE line naming --seed"
N0=$(summaries)
fire_flush "$STALE_H" "$ORIG" "$SID_H" SessionEnd
assert_eq "0" "$RC" "the hook exits 0"
assert_eq "" "$OUT" "...and prints nothing"
await_verdict "$H8" "$N0"
assert_eq "0" "$(stub_total)" "ZERO requests: the frozen copy was not re-sent"
same_offset "$HOME/offH.before" "$ST/$SID_H.offset" "the checkpoint is byte-identical ($LIVE_SIZE_H)"
assert_eq "0" "$(log_count "restarting from 0 .*sid=$H8\$")" "never 'restarting from 0'"
assert_file_contains "$ST/$SID_H.behind" " $STALE_H size=$STALE_SIZE_H checkpoint=$LIVE_SIZE_H" "state/<okey>.behind names the frozen copy"
assert_eq "1" "$(log_count "reason=behind .*sid=$H8\$")" "exactly ONE reason=behind line"
L=$(last_line "reason=behind .*sid=$H8\$")
note "hook line: ${L#* flush }"
assert_contains "$L" "refused, nothing sent, no restart" "...it says refused, nothing sent, no restart"
assert_contains "$L" "kb-flush-backfill.sh --seed $SID_H current|<bytes>" "...and names the --seed procedure"
assert_contains "$L" "the checkpoint moved after this file last changed" "...and why: the file is frozen"
N0=$(summaries)
fire_flush "$STALE_H" "$ORIG" "$SID_H" PreCompact
assert_eq "0" "$RC" "a PreCompact re-fire exits 0"
settle
settle
assert_eq "1" "$(log_count "reason=behind .*sid=$H8\$")" "still ONE reason=behind line (one per 24 h)"
assert_eq "0" "$(stub_total)" "still ZERO requests"
same_offset "$HOME/offH.before" "$ST/$SID_H.offset" "the checkpoint is still byte-identical"

step "2. SWEEP dry run: both frozen copies read behind (REMAIN 0); a dry run writes no marker"
pin_sweep_clock "$(date +%s)"
run_bf
assert_eq "0" "$RC" "the dry run exits 0"
assert_eq "behind" "$(sweep_row_state "$HOME/bf.out" "$SID_H" "$STALE_SIZE_H")" "H's frozen copy reads behind"
assert_contains "$(sweep_row_reason "$HOME/bf.out" "$SID_H" "$STALE_SIZE_H")" "state/$SID_H.behind names this file" "...because the hook's marker names it"
assert_eq "behind" "$(sweep_row_state "$HOME/bf.out" "$SID_S" "$STALE_SIZE_S")" "S's frozen copy reads behind"
R=$(sweep_row_reason "$HOME/bf.out" "$SID_S" "$STALE_SIZE_S")
note "reason: $R"
assert_contains "$R" "the file is frozen" "...because it is frozen (no marker yet)"
assert_contains "$R" "never restarted from 0" "...says never restarted from 0"
assert_contains "$R" "kb-flush-backfill.sh --seed $SID_S current" "...and names the owner's --seed decision"
assert_eq "0" "$(sweep_row_remain "$HOME/bf.out" "$SID_S" "$STALE_SIZE_S")" "...REMAIN 0 (an owner decision, not unflushed)"
assert_contains "$OUT" "2 behind the checkpoint" "the summary counts both"
assert_file_missing "$ST/$SID_S.behind" "a dry run writes no marker for S"
assert_eq "0" "$(stub_total)" "the dry run sent nothing"

step "2. SWEEP --auto: fires neither frozen copy, writes S's marker, .sweep-last behind=2 unflushed=0"
stub_reset # count only what THIS run sends
run_bf --auto
assert_eq "0" "$RC" "--auto exits 0"
assert_eq "" "$OUT" "--auto prints nothing"
settle
assert_eq "0" "$(stub_count ".session_uri==\"session:$SID_S\"")" "--auto fires nothing for S"
assert_eq "0" "$(stub_count ".session_uri==\"session:$SID_H\"")" "...nor for H"
same_offset "$HOME/offS.before" "$ST/$SID_S.offset" "S's checkpoint is byte-identical"
same_offset "$HOME/offH.before" "$ST/$SID_H.offset" "H's checkpoint is byte-identical"
assert_eq "2" "$(sweep_last behind)" ".sweep-last behind=2"
assert_eq "0" "$(sweep_last unflushed)" ".sweep-last unflushed=0 (behind is not unflushed)"
assert_file_contains "$ST/$SID_S.behind" " $STALE_S size=$STALE_SIZE_S checkpoint=$LIVE_SIZE_S" "--auto wrote S's marker naming its frozen copy"
assert_file_contains "$ST/$SID_S.behind" " frozen=$STALE_SIZE_S" "...frozen= its size (review 2 must-fix 1)"
assert_eq "0" "$(log_count "restarting from 0")" "no 'restarting from 0' anywhere"
unpin_sweep_clock

step "2. RESUMED after the live copy is gone (records appended, now NEWER than the checkpoint): the marker still refuses it"
mk_lines "$STALE_H" 3 "$ORIG" "$SID_H" a
mk_lines "$STALE_S" 3 "$ORIG" "$SID_S" a
NEW_H=$(size_of "$STALE_H")
NEW_S=$(size_of "$STALE_S")
if [ "$NEW_H" -lt "$LIVE_SIZE_H" ] && [ "$NEW_S" -lt "$LIVE_SIZE_S" ]; then
  pass "fixture: both resumed copies are still behind their checkpoint ($NEW_H < $LIVE_SIZE_H, $NEW_S < $LIVE_SIZE_S)"
else
  fail "fixture: a resumed copy reached its checkpoint"
fi
assert_ge "$(mtime_of "$STALE_H")" "$(mtime_of "$ST/$SID_H.offset")" "fixture: H's copy is no longer frozen (only the marker can refuse it now)"
assert_ge "$(mtime_of "$STALE_S")" "$(mtime_of "$ST/$SID_S.offset")" "fixture: ...nor S's"
stub_reset
N0=$(summaries)
fire_flush "$STALE_H" "$ORIG" "$SID_H" PreCompact
assert_eq "0" "$RC" "PreCompact at H's resumed copy exits 0"
await_marker "$ST/$SID_H.behind" "$NEW_H" "$N0"
N0=$(summaries)
fire_flush "$STALE_S" "$ORIG" "$SID_S" PreCompact
assert_eq "0" "$RC" "PreCompact at S's resumed copy exits 0"
await_marker "$ST/$SID_S.behind" "$NEW_S" "$N0"
assert_eq "0" "$(stub_total)" "ZERO requests for either resumed copy"
same_offset "$HOME/offH.before" "$ST/$SID_H.offset" "H's checkpoint is byte-identical"
same_offset "$HOME/offS.before" "$ST/$SID_S.offset" "S's checkpoint is byte-identical"
assert_eq "0" "$(log_count "restarting from 0")" "never 'restarting from 0'"
assert_file_contains "$ST/$SID_H.behind" " $STALE_H size=$NEW_H checkpoint=$LIVE_SIZE_H" "H's marker was rewritten with the resumed size"
assert_file_contains "$ST/$SID_S.behind" " $STALE_S size=$NEW_S checkpoint=$LIVE_SIZE_S" "S's marker too"
assert_file_contains "$ST/$SID_H.behind" " frozen=$STALE_SIZE_H" "H's rewrite KEEPS frozen= the size first marked (review 2 must-fix 1)"
assert_file_contains "$ST/$SID_S.behind" " frozen=$STALE_SIZE_S" "...and S's"
assert_eq "1" "$(log_count "reason=behind .*sid=$H8\$")" "H: still ONE reason=behind line (its 24 h stamp)"
assert_eq "1" "$(log_count "reason=behind .*sid=$S8\$")" "S: ONE reason=behind line (the sweep's marker carries no log stamp)"
assert_contains "$(last_line "reason=behind .*sid=$S8\$")" "state/$SID_S.behind names this file" "...saying the marker names the file"
assert_contains "$(last_line "reason=behind .*sid=$S8\$")" "resumed since it was marked: its records past $STALE_SIZE_S were NOT sent -- kb-flush-backfill.sh --seed $SID_S $STALE_SIZE_S sends exactly them" \
  "...and naming the exact seed, --seed <sid> <frozen> (never current)"
run_bf
assert_eq "behind" "$(sweep_row_state "$HOME/bf.out" "$SID_H" "$NEW_H")" "the table: H's resumed copy still reads behind"
assert_eq "behind" "$(sweep_row_state "$HOME/bf.out" "$SID_S" "$NEW_S")" "...and S's"
assert_eq "$((NEW_H - STALE_SIZE_H))" "$(sweep_row_remain "$HOME/bf.out" "$SID_H" "$NEW_H")" "...H's REMAIN = resumed size - frozen= (its unsent records)"
assert_not_contains "$(sweep_row_reason "$HOME/bf.out" "$SID_H" "$NEW_H")" "current (its bytes were sent" "...and its reason never offers current"

# ======================================================== 3. CLEARING the markers
step "3. HOOK: H's frozen copy removed, its live copy restored -- the next firing clears the marker, ONE line"
assert_file_exists "$ST/$SID_H.behind" "precondition: H's marker exists"
rm -f "$STALE_H"
cp "$HOME/liveH.saved" "$LIVE_H"
stub_reset
fire_flush "$LIVE_H" "$RESUMED" "$SID_H" SessionEnd
assert_eq "0" "$RC" "the firing exits 0"
wait_log "marker cleared: state/$SID_H\\.behind named .* sid=$H8\$" 30 || note "no 'marker cleared' line for $H8 within 30 s"
settle
assert_eq "1" "$(log_count "marker cleared: state/$SID_H\\.behind named .* sid=$H8\$")" "the firing logs ONE 'marker cleared' line"
assert_contains "$(last_line "marker cleared: .* sid=$H8\$")" "named $STALE_H, which no longer exists" "...naming the gone path"
assert_file_missing "$ST/$SID_H.behind" "H's marker is gone"
assert_file_missing "$ST/$SID_H.behind.stamp" "...and its log stamp"
assert_eq "0" "$(stub_total)" "nothing was sent (the restored copy is at the checkpoint)"
assert_eq "$LIVE_SIZE_H" "$(cat "$ST/$SID_H.offset" 2>/dev/null)" "H's checkpoint is unchanged"

step "3. SWEEP: S's frozen copy removed -- a dry run leaves the marker, the next --auto pass clears it with a log line"
rm -f "$STALE_S"
run_bf
assert_file_exists "$ST/$SID_S.behind" "the dry run leaves S's marker (a dry run changes no state)"
pin_sweep_clock "$(date +%s)"
run_bf --auto
assert_eq "0" "$RC" "--auto exits 0"
assert_eq "" "$OUT" "--auto prints nothing"
unpin_sweep_clock
assert_file_missing "$ST/$SID_S.behind" "--auto cleared S's marker"
assert_file_missing "$ST/$SID_S.behind.stamp" "...and its log stamp"
assert_eq "1" "$(log_count "backfill $S8 .*marker cleared: state/$SID_S\\.behind named ")" "ONE 'backfill <sid8> ... marker cleared' line"
assert_file_contains "$LOGF" "marker cleared: state/$SID_S.behind named $STALE_S, which no longer exists" "...naming the gone path"
assert_eq "$LIVE_SIZE_S" "$(cat "$ST/$SID_S.offset" 2>/dev/null)" "S's checkpoint is unchanged (clearing never touches an offset)"
assert_eq "0" "$(stub_total)" "nothing was sent"

# =========================================== 4. CONTROL: a genuine rewrite, no copy
# GREEN on every hook since wave C: one transcript, no other <sid>.jsonl, no
# marker, rewritten smaller AFTER its checkpoint's last write.
step "4. CONTROL: a SINGLE transcript rewritten smaller AFTER the checkpoint still restarts from 0"
setup_home c2rewrite
SID_C="c0de0046-0000-4000-8000-000000000046"
C8=${SID_C:0:8}
T_C="$LIVE_DIR/$SID_C.jsonl"
mk_lines "$T_C" 60 "$RESUMED" "$SID_C"
fire_flush "$T_C" "$RESUMED" "$SID_C" SessionEnd
wait_firing 0 60 || fail "setup: the transcript never flushed: $(tail -3 "$LOGF" | tr '\n' '|')"
FULL_C=$(size_of "$T_C")
assert_eq "$FULL_C" "$(cat "$ST/$SID_C.offset" 2>/dev/null)" "setup: checkpointed at its size"
head -n 25 "$T_C" >"$HOME/trunc" && cat "$HOME/trunc" >"$T_C"
touch -d @"$(($(mtime_of "$ST/$SID_C.offset") + 5))" "$T_C" # explicitly NEWER than the checkpoint
TRUNC_C=$(size_of "$T_C")
assert_eq "1" "$(find "$PROJDIR" -name "$SID_C.jsonl" | wc -l | tr -d ' ')" "fixture: no other copy of $C8 exists"
assert_file_missing "$ST/$SID_C.behind" "fixture: no behind marker"
assert_file_missing "$ST/$SID_C.diverged" "fixture: no diverged marker"
pin_sweep_clock "$(($(date +%s) + 1000))" # past the quiet window (the rewrite's mtime is in the future)
run_bf
unpin_sweep_clock
assert_eq "shrank" "$(sweep_row_state "$HOME/bf.out" "$SID_C" "$TRUNC_C")" "positive control: the sweep tables the rewrite shrank (fireable)"
stub_reset
N0=$(summaries)
fire_flush "$T_C" "$RESUMED" "$SID_C" PreCompact
wait_firing "$N0" 60 || fail "the shrank re-drain never completed: $(tail -3 "$LOGF" | tr '\n' '|')"
assert_eq "1" "$(log_count "reason=shrank .*restarting from 0 .*sid=$C8\$")" "one 'reason=shrank — restarting from 0' line"
assert_file_contains "$LOGF" "transcript shrank ($TRUNC_C < offset $FULL_C)" "...naming the size and the checkpoint"
assert_eq "0" "$(log_count 'stale copy')" "a genuine rewrite is never a stale/diverged/behind copy"
assert_eq "0" "$(stub_field ".session_uri==\"session:$SID_C\"" '.flush_offset' | head -n1)" "the re-drain starts at offset 0 (flush_offset of the first chunk)"
stub_concat ".session_uri==\"session:$SID_C\"" '.transcript' >"$HOME/ctl.got"
if cmp -s "$T_C" "$HOME/ctl.got"; then
  pass "...and re-sends the rewritten transcript byte for byte"
else
  fail "...and re-sends the rewritten transcript byte for byte -- got $(size_of "$HOME/ctl.got") of $TRUNC_C bytes"
fi
assert_eq "$TRUNC_C" "$(cat "$ST/$SID_C.offset" 2>/dev/null)" "the checkpoint follows to the new EOF"
assert_file_missing "$ST/$SID_C.behind" "no behind marker was written"
assert_file_missing "$ST/$SID_C.diverged" "no diverged marker was written"
step "4. CONTROL: rewritten to one unterminated line (newer again), the restart's 0 lands on disk"
printf '{"type":"user","half":' >"$T_C"
touch -d @"$(($(mtime_of "$ST/$SID_C.offset") + 5))" "$T_C"
HALF_C=$(size_of "$T_C")
B_C=$(stub_total)
fire_flush "$T_C" "$RESUMED" "$SID_C" PreCompact
wait_log "transcript shrank \\($HALF_C < offset $TRUNC_C\\)" 30 || fail "no shrank line for the second rewrite"
settle
settle
assert_eq "0" "$(cat "$ST/$SID_C.offset" 2>/dev/null)" "the offset file reads 0"
assert_eq "$B_C" "$(stub_total)" "nothing was sent (the only line is unterminated)"
assert_eq "0" "$(log_count 'stale copy')" "still never a stale copy"

finish

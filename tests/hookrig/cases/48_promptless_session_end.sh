#!/usr/bin/env bash
# CASE: the SessionEnd of a session that never wrote a transcript is a quiet
#       `flush skip:` line, not a `BUG:` alarm (KB 51863).
# PINS: Claude Code writes <projects root>/<slug>/<sid>.jsonl at the first
#       recorded message, but a session that ends before one -- the VS Code
#       extension's config-cache probe, a panel opened and closed, an idle panel
#       closed days later -- still fires SessionStart + SessionEnd naming the file
#       it would have used. Case 42's line (`BUG: transcript not found ...
#       (path conversion?)`) fired for every one of those: 54 of 54 BUG lines on
#       usama-server 2026-09-28..10-26, nothing lost, and the pre-brief could
#       never clear them (no transcript, so no later `ok` for that sid). Now the
#       missing-file branch logs ONE `flush skip:` line (no BUG token, no box
#       stamp) when ALL hold:
#         * the event is SessionEnd (a session with turns fires Stop first);
#         * the path is the session's OWN canonical transcript: exactly
#           $HOME/.claude/projects/<one dir>/<sid>.jsonl -- the root the
#           BACKFILL sweep scans, so where it runs a transcript that appears
#           later is still captured (under ${CLAUDE_CONFIG_DIR}/projects, which
#           the backfill sweep never scans, it stays a BUG);
#         * no firing of this session ever found its transcript: no
#           state/<sid>.* file -- every firing past the file test now leaves
#           state/<sid>.seen, even a Stop whose gate stays closed -- and no
#           state/recall-<sid>.* stamp. The daily STATE sweep retires a .seen
#           with its offset (30 days), an orphan one (no offset) only after 90
#           days with a loss note, and never one whose transcript exists.
#       Everything else still raises case 42's BUG line: Stop/PreCompact, a path
#       outside the root, one not named for the sid, a nested one, and a session
#       the hooks have seen before. And the pre-brief drops skip lines BEFORE its
#       40-line failure window, so a burst of them never hides a real failure.
#       Positive control: an existing transcript drains with neither line.
# RED on today's hooks (HEAD 73547b3): the skip assertions, the brief-burst
#       check and the SB step's `.seen` check (no breadcrumb before this
#       change) FAIL, as do the orphan-sweep steps; the other BUG-still-fires
#       steps pass on both (each ages the hourly box stamp first).
. /rig/lib.sh

new_home promptless
export KB_SWEEP_INTERVAL=0
cfg_rest
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"
ST="$HOOKS/state"
BADSTAMP="$ST/.badpath.stamp"
ROOT="$HOME/.claude/projects"
WORK="$HOME/work/proj"
mkdir -p "$WORK" "$ROOT/-home-promptless-work-proj"

event() { # <sid> <transcript> [event]
  "$RIG_JQ" -nc --arg s "$1" --arg t "$2" --arg c "$WORK" --arg e "${3:-SessionEnd}" \
    '{session_id:$s,transcript_path:$t,cwd:$c,hook_event_name:$e,reason:"other"}'
}
bugs() { log_count "BUG: transcript not found.* sid=$1"; }
skips() { log_count "flush skip: .* sid=$1"; }
age_stamp() { # let the NEXT BUG line through (case 42's hourly box stamp)
  for _f in "$ST"/.badpath*; do
    [ -e "$_f" ] || continue
    printf '0\n' >"$_f"
    touch -d @1000000000 "$_f"
  done
}
offsets() { find "$ST" -maxdepth 1 -name '*.offset' 2>/dev/null | wc -l | tr -d ' '; }

S1="5e55e0d1-1111-4111-8111-111111111111"
step "a prompt-less SessionEnd (canonical path, slug dir exists, no state): one skip line, no BUG"
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(event "$S1" "$ROOT/-home-promptless-work-proj/$S1.jsonl")"
assert_eq "0" "$RC" "the hook exits 0"
assert_eq "" "$OUT" "...and prints nothing"
settle
assert_eq "0" "$(bugs "${S1:0:8}")" "no BUG line for a prompt-less SessionEnd"
assert_eq "1" "$(skips "${S1:0:8}")" "ONE skip line for sid=${S1:0:8}"
assert_eq "1" "$(log_count "flush skip: .* ev=SessionEnd sid=${S1:0:8}")" "...naming ev=SessionEnd"
note "the line: $(grep -F 'flush skip:' "$LOGF" 2>/dev/null | head -1)"
assert_file_missing "$BADSTAMP" "the box stamp is untouched"
assert_eq "0" "$(offsets)" "no offset file was created"
assert_file_missing "$ST/$S1.project" "no pin was written"
assert_eq "0" "$(stub_total)" "nothing was submitted"

S2="5e55e0d2-2222-4222-8222-222222222222"
step "...also when the slug dir itself was never created (a first, prompt-less session in /tmp)"
fire "$HOOKS/flush-session-to-kb.sh" "$(event "$S2" "$ROOT/-tmp/$S2.jsonl")"
assert_eq "0" "$RC" "exits 0"
settle
assert_eq "0" "$(bugs "${S2:0:8}")" "...nor for a slug dir that was never created"
assert_eq "1" "$(skips "${S2:0:8}")" "ONE skip line for sid=${S2:0:8}"

step "the pre-brief raises no flush warning for skip lines"
fire "$HOOKS/kb-session-brief.sh" "$(start_event startup "$WORK")"
assert_not_contains "$OUT" "reported a recent failure" "the brief raises no flush warning"

# --- everything below must still alarm -------------------------------------------
S4="5e55e0d4-4444-4444-8444-444444444444"
step "BUG still fires: a Stop naming a missing canonical transcript (a session with turns)"
age_stamp # on the old hooks step 1 stamped the box; every BUG step must stand alone
fire "$HOOKS/flush-session-to-kb.sh" "$(event "$S4" "$ROOT/-home-promptless-work-proj/$S4.jsonl" Stop)"
assert_eq "0" "$RC" "exits 0"
wait_log "BUG: transcript not found.*sid=${S4:0:8}" 5 || true
assert_eq "1" "$(bugs "${S4:0:8}")" "a Stop still raises BUG"
assert_eq "0" "$(skips "${S4:0:8}")" "...and no skip line"

S5="5e55e0d5-5555-4555-8555-555555555555"
step "BUG still fires: a PreCompact naming a missing canonical transcript"
age_stamp
fire "$HOOKS/flush-session-to-kb.sh" "$(event "$S5" "$ROOT/-home-promptless-work-proj/$S5.jsonl" PreCompact)"
wait_log "BUG: transcript not found.*sid=${S5:0:8}" 5 || true
assert_eq "1" "$(bugs "${S5:0:8}")" "a PreCompact still raises BUG"

S6="5e55e0d6-6666-4666-8666-666666666666"
step "BUG still fires: a SessionEnd of a session the hooks HAVE seen (state/<sid>.offset)"
age_stamp
printf '123\n' >"$ST/$S6.offset"
fire "$HOOKS/flush-session-to-kb.sh" "$(event "$S6" "$ROOT/-home-promptless-work-proj/$S6.jsonl")"
wait_log "BUG: transcript not found.*sid=${S6:0:8}" 5 || true
assert_eq "1" "$(bugs "${S6:0:8}")" "earlier state makes a missing transcript a BUG"
assert_eq "0" "$(skips "${S6:0:8}")" "...and no skip line"

S7="5e55e0d7-7777-4777-8777-777777777777"
step "BUG still fires: a SessionEnd whose session had a recalled prompt (state/recall-<sid>.last)"
age_stamp
printf 'x\n' >"$ST/recall-$S7.last"
fire "$HOOKS/flush-session-to-kb.sh" "$(event "$S7" "$ROOT/-home-promptless-work-proj/$S7.jsonl")"
wait_log "BUG: transcript not found.*sid=${S7:0:8}" 5 || true
assert_eq "1" "$(bugs "${S7:0:8}")" "a recall stamp makes a missing transcript a BUG"

S3="5e55e0d3-3333-4333-8333-333333333333"
step "BUG still fires: a prompt-less SessionEnd under \${CLAUDE_CONFIG_DIR}/projects (the sweep never scans it)"
age_stamp
CFGD="$HOME/altcfg"
mkdir -p "$CFGD/projects/-home-promptless-work-proj"
CLAUDE_CONFIG_DIR="$CFGD" fire "$HOOKS/flush-session-to-kb.sh" "$(event "$S3" "$CFGD/projects/-home-promptless-work-proj/$S3.jsonl")"
assert_eq "0" "$RC" "exits 0"
wait_log "BUG: transcript not found.*sid=${S3:0:8}" 5 || true
assert_eq "1" "$(bugs "${S3:0:8}")" "a CLAUDE_CONFIG_DIR transcript path still raises BUG"
assert_eq "0" "$(skips "${S3:0:8}")" "...and no skip line"

S8="5e55e0d8-8888-4888-8888-888888888888"
step "BUG still fires: SessionEnd paths that are not the session's own canonical transcript"
age_stamp
fire "$HOOKS/flush-session-to-kb.sh" "$(event "$S8" "$HOME/elsewhere/$S8.jsonl")"
wait_log "BUG: transcript not found.*sid=${S8:0:8}" 5 || true
assert_eq "1" "$(bugs "${S8:0:8}")" "outside the projects roots -> BUG"
age_stamp
fire "$HOOKS/flush-session-to-kb.sh" "$(event "$S8" "$ROOT/-home-promptless-work-proj/$S1.jsonl")"
wait_log "BUG: transcript not found.*sid=${S8:0:8}" 5 || true
assert_eq "2" "$(bugs "${S8:0:8}")" "a file named for ANOTHER sid -> BUG"
age_stamp
fire "$HOOKS/flush-session-to-kb.sh" "$(event "$S8" "$ROOT/a/b/$S8.jsonl")"
wait_log "BUG: transcript not found.*sid=${S8:0:8}" 5 || true
assert_eq "3" "$(bugs "${S8:0:8}")" "a nested path under the root -> BUG"
age_stamp
fire "$HOOKS/flush-session-to-kb.sh" "$(event "$S8" "${ROOT}x/-home-promptless-work-proj/$S8.jsonl")"
wait_log "BUG: transcript not found.*sid=${S8:0:8}" 5 || true
assert_eq "4" "$(bugs "${S8:0:8}")" "a sibling of the root sharing its prefix -> BUG"
assert_eq "0" "$(skips "${S8:0:8}")" "...none of them is a skip"

step "POSITIVE CONTROL: an EXISTING transcript drains with neither line"
T9="$ROOT/-home-promptless-work-proj/sess-real.jsonl"
mk_transcript "$T9" 5 real
_sums=$(log_count ' (ok|partial) bytes=')
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T9" "$WORK" sess-real SessionEnd)"
assert_eq "0" "$RC" "exits 0"
if wait_firing "$_sums" 30; then pass "the existing transcript drained"; else fail "the existing transcript never drained"; fi
assert_eq "0" "$(log_count 'flush skip: .* sid=sess-rea')" "no skip line for a transcript that exists"
assert_ge "$(stub_count '.path=="/api/ingest-session" and .status==202')" "1" "...and it really submitted"

step "a burst of skip lines never pushes an uncleared failure out of the pre-brief's window"
# The BUG lines above (S3..S8) are still uncleared: no later ok for those sids.
fire "$HOOKS/kb-session-brief.sh" "$(start_event startup "$WORK")"
assert_contains "$OUT" "reported a recent failure" "precondition: the brief warns about the uncleared BUG lines"
for _n in $(seq 10 54); do # 45 prompt-less SessionEnds: more than the 40-line window
  _s="5e55e0$(printf '%02d' "$_n")-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
  fire "$HOOKS/flush-session-to-kb.sh" "$(event "$_s" "$ROOT/-home-promptless-work-proj/$_s.jsonl")"
done
settle
assert_ge "$(log_count 'flush skip: ')" "47" "45 more skip lines were written"
fire "$HOOKS/kb-session-brief.sh" "$(start_event startup "$WORK")"
assert_contains "$OUT" "reported a recent failure" "...and the brief STILL warns after them"

S9="5e55e0d9-9999-4999-8999-999999999999"
step "the probe's real sequence through the dispatcher: SessionStart then SessionEnd for one sid"
"$RIG_JQ" -nc --arg s "$S9" --arg c "$WORK" '{hook_event_name:"SessionStart",source:"startup",cwd:$c,session_id:$s}' |
  bash "$HOOKS/kb-hook-dispatch.sh" SessionStart >/dev/null 2>&1
assert_eq "0" "$(find "$ST" -maxdepth 1 -name "*$S9*" | wc -l | tr -d ' ')" "SessionStart left no state keyed by the sid"
age_stamp # so a misclassified SessionEnd's BUG line could really log
event "$S9" "$ROOT/-home-promptless-work-proj/$S9.jsonl" | bash "$HOOKS/kb-hook-dispatch.sh" SessionEnd >/dev/null 2>&1
settle
assert_eq "1" "$(skips "${S9:0:8}")" "the dispatched SessionEnd is ONE skip line"
assert_eq "0" "$(bugs "${S9:0:8}")" "...and no BUG line"

SA="5e55e0da-aaaa-4aaa-8aaa-aaaaaaaaaaab"
step "...and with a trailing slash on HOME (some sudo/container setups)"
HOME="$HOME/" fire "$HOOKS/flush-session-to-kb.sh" "$(event "$SA" "$ROOT/-home-promptless-work-proj/$SA.jsonl")"
assert_eq "0" "$RC" "exits 0"
settle
assert_eq "1" "$(skips "${SA:0:8}")" "a skip with HOME=<home>/"

SB="5e55e0db-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
step "BUG still fires: a session whose closed-gate Stop FOUND its transcript, which then vanished"
TB="$ROOT/-home-promptless-work-proj/$SB.jsonl"
mk_transcript "$TB" 5 shortsession
fire "$HOOKS/flush-session-to-kb.sh" "$(event "$SB" "$TB" Stop)"
assert_eq "0" "$RC" "the Stop exits 0"
settle
if [ -e "$ST/$SB.seen" ]; then pass "the Stop left the <sid>.seen breadcrumb"; else fail "no state/$SB.seen after a Stop that found its transcript"; fi
assert_file_missing "$ST/$SB.offset" "...and nothing else: the gate stayed closed (no offset)"
assert_file_missing "$ST/$SB.gate" "...(no .gate)"
rm -f "$TB"
age_stamp
fire "$HOOKS/flush-session-to-kb.sh" "$(event "$SB" "$TB")"
wait_log "BUG: transcript not found.*sid=${SB:0:8}" 5 || true
assert_eq "1" "$(bugs "${SB:0:8}")" "its SessionEnd is a BUG, not a skip"
assert_eq "0" "$(skips "${SB:0:8}")" "...and no skip line"

step "the daily state sweep: an ORPHAN .seen (no offset, transcript gone) goes only after 90 days, with a loss note"
aged() { touch -d "$1 days ago" "$2" 2>/dev/null || touch -t 202001010000 "$2"; }
mk_transcript "$ROOT/-home-promptless-work-proj/keepseen-1.jsonl" 3 keep
: >"$ST/goneseen-1.seen"   # 100 d, transcript gone, no offset -> retired
: >"$ST/keepseen-1.seen"   # 100 d, transcript EXISTS          -> kept
: >"$ST/youngorph-1.seen"  # 45 d, transcript gone, no offset  -> kept (inside 90 d)
: >"$ST/youngoff-1.seen"   # 100 d .seen beside a FRESH offset, transcript gone -> both kept
printf '100' >"$ST/youngoff-1.offset"
aged 100 "$ST/goneseen-1.seen"
aged 100 "$ST/keepseen-1.seen"
aged 45 "$ST/youngorph-1.seen"
aged 100 "$ST/youngoff-1.seen"
rm -f "$ST/.state-sweep.last"
TD="$ROOT/-home-promptless-work-proj/sweepdrv.jsonl"
mk_transcript "$TD" 5 drv
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TD" "$WORK" sweepdrv PreCompact)"
wait_log "state sweep: retired sid=goneseen-1" 20 || note "sweep line not yet seen"
assert_file_missing "$ST/goneseen-1.seen" "the 100-day orphan .seen was retired"
assert_eq "1" "$(log_count 'state sweep: retired sid=goneseen-1 transcript gone \(no checkpoint: the session never drained')" \
  "...with the never-drained loss note"
if [ -e "$ST/keepseen-1.seen" ]; then pass "a .seen whose transcript exists is kept"; else fail "the live session's .seen was retired"; fi
if [ -e "$ST/youngorph-1.seen" ]; then pass "a 45-day orphan .seen is kept (past the transcript retention, inside 90 d)"; else fail "a 45-day orphan .seen was retired"; fi
if [ -e "$ST/youngoff-1.seen" ] && [ -e "$ST/youngoff-1.offset" ]; then
  pass "an aged .seen beside a FRESH offset leaves the whole set alone"
else
  fail "an aged .seen beside a fresh offset was retired (the live checkpoint is gone)"
fi
if [ -e "$ST/$SB.seen" ]; then pass "a young orphan (SB, transcript gone today) is kept"; else fail "SB's fresh .seen was retired"; fi

finish

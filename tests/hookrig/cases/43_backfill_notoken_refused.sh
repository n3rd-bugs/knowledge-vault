#!/usr/bin/env bash
# CASE: a rate-limited NEEDS TOKEN refusal is `refused` in the sweep, never
#       `no-progress` (wave C.1, KB 40411 fix set item 7).
# PINS: the hook's NEEDS TOKEN line is rate-limited to one an hour per project
#       (state/.notoken-<slug>.stamp, flush-session-to-kb.sh kb_no_token_notice)
#       while the sweep runs every 30 minutes, so every OTHER --auto run fired a
#       hook that refused SILENTLY. kb-flush-backfill.sh read the empty log
#       segment as `no-progress` ("a rate-limited refusal? check ...") and walked
#       the session into STALLED with the wrong diagnosis (usama-home, KB 40411
#       defect 3). Now: no hook line + a FRESH (< 3600 s) .notoken-<slug>.stamp
#       for the transcript's pinned slug => VERDICT=refused with a DETAIL that
#       says `NEEDS TOKEN (rate-limited)` -- on the sweep's own log line and in
#       state/<okey>.sweep. Never a token VALUE anywhere.
#       Positive control: an AGED stamp lets the hook log its NEEDS TOKEN line,
#       which the sweep already calls `refused` today.
# SETUP: REST transport, NO shared KB_TOKEN, a kb-tokens.env holding only
#       another project's credential (so the hook takes the NEEDS TOKEN path,
#       not NOT CONFIGURED), no KB repo (so nothing can auto-mint); the
#       transcript is pinned `schematics<TAB>firing` like usama-home's.
# RED on today's hooks (HEAD b2b37a3) -- expected FAIL lines (steps 1 and 3):
#       "the sweep's verdict is refused -- [ schematics refused ] not found in
#       [... backfill 5c4e3a71 schematics no-progress base=0 ...]",
#       "...with DETAIL NEEDS TOKEN (rate-limited) -- ... not found",
#       "...and never no-progress -- [no-progress] unexpectedly present",
#       "state/<okey>.sweep says verdict=refused -- expected [refused], got [no-progress]".
#       The control step, the silence checks and the no-token-value checks pass
#       on both.
. /rig/lib.sh

new_home notokensweep
stub_reset
install_hooks >/dev/null
printf '# REMOTE\nKB_REST_URL=%s\n' "$STUB" | write_cfg # NO shared KB_TOKEN
BAIT="kbg_0000000000000000000000000000000000000000"
printf 'KB_TOKEN_OTHERPROJ=%s\n' "$BAIT" >"$HOME/.claude/kb-tokens.env"
chmod 600 "$HOME/.claude/kb-tokens.env"
BF="$HOOKS/kb-flush-backfill.sh"
ST="$HOOKS/state"
PROJDIR="$HOME/.claude/projects"
mkdir -p "$ST"
printf '2000-01-01T00:00:00Z\n' >"$ST/.checkpoints-trusted-since"
# NB: never KB_SWEEP_INTERVAL=0 here -- that is the sweep's OFF switch (--auto exits 0).

SLUG=schematics
SID="5c4e3a71-0000-4000-8000-000000000043"
SID8="${SID:0:8}"
CWD="$HOME/work/$SLUG" # NOT a git repo: a firing-derived pin, as on usama-home
mkdir -p "$CWD"
TDIR="$PROJDIR/$(printf '%s' "$CWD" | tr '/' '-')"
mkdir -p "$TDIR"
T="$TDIR/$SID.jsonl"
"$RIG_PY" - "$T" "$CWD" <<'EOF'
import json, sys
path, cwd = sys.argv[1], sys.argv[2]
with open(path, "w", encoding="utf-8") as fh:
    for i in range(20):
        fh.write(json.dumps({"type": "user", "timestamp": "2026-09-20T00:00:00.000Z",
                             "cwd": cwd, "i": i, "text": "schematics line %d %s" % (i, "y" * 40)}) + "\n")
EOF
touch -d @1700000000 "$T" # long outside the sweep's quiet window
printf '%s\tfiring\n' "$SLUG" >"$ST/$SID.project"
STAMP="$ST/.notoken-$SLUG.stamp"

run_auto() {
  OUT=$("$BF" --auto 2>&1)
  RC=$?
}
bf_line() { grep -F " backfill $SID8 " "$LOGF" 2>/dev/null | tail -n1; }
sweep_field() { sed -n "s/.*\\b$1=\\([^ ]*\\).*/\\1/p" "$ST/$SID.sweep" 2>/dev/null | head -n1; }
needs_token_lines() { log_count "flush $SLUG NEEDS TOKEN"; }

step "precondition: the sweep tables the pinned transcript as ready"
ROW=$("$BF" 2>&1 | grep -F " $SID " | head -n1)
assert_contains "$ROW" "$SLUG" "the dry table files it under the pinned slug"
assert_contains "$ROW" " ready" "...as ready (no checkpoint, post-stamp)"

# ------------------------------------------ 1. a FRESH stamp, no hook line
step "a fresh .notoken-$SLUG.stamp: the hook refuses in SILENCE and the sweep says refused (rate-limited)"
date +%s >"$STAMP"
run_auto
assert_eq "0" "$RC" "--auto exits 0"
assert_eq "" "$OUT" "--auto prints nothing"
assert_eq "0" "$(needs_token_lines)" "precondition: the hook logged NO NEEDS TOKEN line (rate-limited)"
L=$(bf_line)
note "sweep line: ${L#* backfill }"
assert_contains "$L" " $SLUG refused " "the sweep's verdict is refused"
assert_contains "$L" "NEEDS TOKEN (rate-limited)" "...with DETAIL NEEDS TOKEN (rate-limited)"
assert_not_contains "$L" "no-progress" "...and never no-progress"
assert_eq "refused" "$(sweep_field verdict)" "state/<okey>.sweep says verdict=refused"
assert_eq "0" "$(stub_total)" "nothing was sent"
assert_file_missing "$ST/$SID.offset" "a NEEDS TOKEN refusal writes no checkpoint"

# ------------------------------------------ 2. POSITIVE CONTROL: aged stamp
step "POSITIVE CONTROL: an aged stamp -> the hook logs NEEDS TOKEN itself -> refused"
printf '0\n' >"$STAMP"
run_auto
assert_eq "0" "$RC" "--auto exits 0"
assert_eq "1" "$(needs_token_lines)" "the hook logged its NEEDS TOKEN line"
L=$(bf_line)
note "sweep line: ${L#* backfill }"
assert_contains "$L" " $SLUG refused " "the sweep's verdict is refused"
assert_not_contains "$L" "no-progress" "...and never no-progress"
_st=$(cat "$STAMP" 2>/dev/null)
case "$_st" in '' | *[!0-9]*) fail "the hook did not re-stamp $STAMP ([$_st])" ;;
  *) assert_le "$(($(date +%s) - _st))" "60" "the hook re-stamped .notoken-$SLUG.stamp (fresh again)" ;;
esac

# ---------------------- 3. the very next run: the hook's own stamp is fresh
step "the next --auto inside the hour (the hook's OWN fresh stamp): refused (rate-limited), not no-progress"
run_auto
assert_eq "0" "$RC" "--auto exits 0"
assert_eq "1" "$(needs_token_lines)" "precondition: the hook stayed silent (still one NEEDS TOKEN line)"
L=$(bf_line)
note "sweep line: ${L#* backfill }"
assert_contains "$L" " $SLUG refused " "the sweep's verdict is refused"
assert_contains "$L" "NEEDS TOKEN (rate-limited)" "...with DETAIL NEEDS TOKEN (rate-limited)"
assert_not_contains "$L" "no-progress" "...and never no-progress"
assert_eq "refused" "$(sweep_field verdict)" "state/<okey>.sweep says verdict=refused"
assert_eq "3" "$(sweep_field fails)" "a refusal still counts toward the stall (fails=3): never abandoned, never mislabelled"

step "no token VALUE anywhere"
assert_not_contains "$(cat "$LOGF" 2>/dev/null)" "kbg_" "the flush log carries no kbg_ value"
assert_not_contains "$(cat "$ST/$SID.sweep" 2>/dev/null)" "kbg_" "the .sweep record carries no kbg_ value"

finish

#!/usr/bin/env bash
# CASE: kb-flush-backfill.sh -- the whole-box recovery sweep
# PINS: every transcript ALREADY on a box can still be captured. The flush hook
#       only ever sees the sessions that fire it, so anything it refused (8 of 22
#       flushes on 2026-07-31 had no project token) and everything that predates
#       the install is knowledge sitting on disk that the KB has never been told
#       about. That recovery was done BY HAND once, hook-firing per transcript;
#       this case pins the productised version.
# ALSO PINS the three ways the sweep could quietly do damage:
#         * DRY RUN BY DEFAULT -- a bare invocation sends NOTHING and moves no
#           offset, so "let me see what is missing" can never become a 40-session
#           bulk submission by accident.
#         * NEVER GUESSED INTO THE WRONG PROJECT -- the cwd comes from the
#           transcript's OWN records. The directory slug is ambiguous (Claude Code
#           replaces every '/' with '-', which a directory name containing a dash
#           makes irreversible), so a transcript whose cwd cannot be recovered is
#           reported SKIPPED with its reason. Misfiling one client's session under
#           another's name is the confidentiality failure 'session:' already cost.
#         * IDEMPOTENT -- the hook owns the offsets, so a second sweep sends
#           nothing. Re-running a recovery must never duplicate knowledge.
#       Finally: it is NOT a hook. install.sh installs it and registers no event.
. /rig/lib.sh

new_home backfill
stub_reset
cfg_mcp
install_hooks >/dev/null

BF="$HOOKS/kb-flush-backfill.sh"
PROJDIR="$HOME/.claude/projects"
WORK="$HOME/work"

slug_of() { printf '%s' "$1" | tr '/' '-'; } # exactly how Claude Code names the dir

mk_session() { # <project-dir-name> <session-id> <lines> [cwd]
  mkdir -p "$PROJDIR/$1"
  "$RIG_PY" - "$PROJDIR/$1/$2.jsonl" "$3" "${4:-}" <<'EOF'
import json, sys
path, n, cwd = sys.argv[1], int(sys.argv[2]), sys.argv[3]
with open(path, "w", encoding="utf-8") as fh:
    for i in range(n):
        rec = {"type": "user", "i": i, "text": "backfill line %d %s" % (i, "y" * 40)}
        if cwd:
            rec["cwd"] = cwd
        fh.write(json.dumps(rec) + "\n")
EOF
  printf '%s' "$PROJDIR/$1/$2.jsonl"
}

run_bf() { # capture the sweep's own report so the TABLE can be asserted, not just its effects
  OUT=$("$BF" "$@" 2>&1)
  RC=$?
  printf '%s\n' "$OUT" >"$HOME/bf.out"
  export RC
}
bf_row() { # <session id> -> "project session size offset remaining state"
  awk -v s="$1" '$2 == s { print $1, $2, $3, $4, $5, $6 }' "$HOME/bf.out"
}
count_ingest() { stub_count '.tool=="kb_ingest_session"'; }

# ---------------------------------------------------------------- installation
step "install.sh ships the sweep but registers NO hook event for it"
if [ -x "$BF" ]; then pass "kb-flush-backfill.sh installed at $BF"; else fail "the sweep was not installed"; fi
# `grep -c` PRINTS 0 and EXITS 1 on no match, so `|| echo 0` would emit "0\n0".
REGISTERED=$(grep -c 'kb-flush-backfill' "$SETTINGS" 2>/dev/null) || REGISTERED=0
assert_eq "0" "$REGISTERED" \
  "settings.json registers no event for it (it is owner-run, not a hook)"

step "it carries the SAME portable block as the four hook scripts"
# Case 26 diffs four copies and does not know about a fifth. Shell has no import,
# so an unchecked copy is a copy that will drift.
extract_block "$SKILL/install.sh" "$HOME/block.ref"
extract_block "$BF" "$HOME/block.bf"
if diff -u "$HOME/block.ref" "$HOME/block.bf" >"$HOME/blockdiff"; then
  pass "the block is byte-identical to install.sh's"
else
  fail "the backfill's portable block DRIFTED: $(head -c 300 "$HOME/blockdiff")"
fi

# ------------------------------------------------------------------- the fixture
# Four transcripts covering every state the sweep has to tell apart.
mkdir -p "$WORK/alpha" "$WORK/beta" "$WORK/gamma"
T_DONE=$(mk_session "$(slug_of "$WORK/alpha")" s-done 12 "$WORK/alpha")
T_PART=$(mk_session "$(slug_of "$WORK/beta")" s-partial 20 "$WORK/beta")
T_FRESH=$(mk_session "$(slug_of "$WORK/gamma")" s-fresh 15 "$WORK/gamma")
# No cwd in ANY record, and a directory slug that decodes to a path which does
# not exist on this box -> unrecoverable, and therefore skipped rather than
# guessed.
T_NOCWD=$(mk_session "-nowhere-under-this-box" s-nocwd 8 "")

SZ_DONE=$(wc -c <"$T_DONE" | tr -d ' ')
SZ_PART=$(wc -c <"$T_PART" | tr -d ' ')
SZ_FRESH=$(wc -c <"$T_FRESH" | tr -d ' ')
SZ_NOCWD=$(wc -c <"$T_NOCWD" | tr -d ' ')
REM_PART=$((SZ_PART - 300))

mkdir -p "$HOOKS/state"
printf '%s' "$SZ_DONE" >"$HOOKS/state/s-done.offset"  # fully flushed already
printf '300' >"$HOOKS/state/s-partial.offset"         # flushed up to byte 300

# Oldest first is the sweep order, so give the fixture a deterministic one.
touch -d @1700000100 "$T_PART"
touch -d @1700000200 "$T_FRESH"
touch -d @1700000300 "$T_DONE"
touch -d @1700000400 "$T_NOCWD"

# ------------------------------------------------------------------- the dry run
step "a bare invocation TABLES all four and sends nothing"
run_bf
assert_eq "0" "$RC" "the sweep exits 0"
assert_eq "alpha s-done $SZ_DONE $SZ_DONE 0 flushed" "$(bf_row s-done)" \
  "an already-flushed session has no remainder"
assert_eq "beta s-partial $SZ_PART 300 $REM_PART ready" "$(bf_row s-partial)" \
  "a partially flushed session offers only the bytes past its offset"
assert_eq "gamma s-fresh $SZ_FRESH 0 $SZ_FRESH ready" "$(bf_row s-fresh)" \
  "an untouched session offers the whole file"
assert_eq "? s-nocwd $SZ_NOCWD 0 $SZ_NOCWD skipped" "$(bf_row s-nocwd)" \
  "an unrecoverable cwd is SKIPPED, not filed under a guessed project"
assert_file_contains "$HOME/bf.out" "no cwd in the first" "and the skip states its reason"
assert_contains "$OUT" "DRY RUN" "the default run says it sent nothing"
assert_contains "$OUT" "2 with unflushed bytes ($((REM_PART + SZ_FRESH)) bytes)" "the totals add up"

settle
settle
assert_eq "0" "$(stub_total)" "a DRY RUN made no transport call at all"
assert_eq "300" "$(cat "$HOOKS/state/s-partial.offset")" "and moved no offset"

step "--project narrows the sweep to one project"
run_bf --project beta
assert_eq "beta s-partial $SZ_PART 300 $REM_PART ready" "$(bf_row s-partial)" "beta is listed"
assert_eq "" "$(bf_row s-fresh)" "gamma is not"
assert_eq "" "$(bf_row s-done)" "alpha is not"

# ---------------------------------------------------------------------- the push
step "--push flushes EXACTLY the two remainders, oldest transcript first"
stub_reset
run_bf --push
assert_eq "0" "$RC" "the sweep exits 0"
assert_contains "$OUT" "pushed=2 deferred=0 failed=0" "the summary counts what happened"
assert_eq "2" "$(count_ingest)" "two submissions, not four"
assert_eq "beta
gamma" "$(stub_field '.tool=="kb_ingest_session"' '.project')" \
  "beta (older) then gamma -- and NOTHING for alpha or the skipped one"
assert_eq "session:s-partial
session:s-fresh" "$(stub_field '.tool=="kb_ingest_session"' '.session_uri')" \
  "each carries its own session identity"

step "the bytes on the wire are the REMAINDER, not the whole transcript"
assert_eq "$REM_PART" "$(stub_field '.tool=="kb_ingest_session" and .project=="beta"' '.transcript_bytes')" \
  "beta sent $REM_PART bytes (size $SZ_PART minus offset 300)"
assert_eq "$SZ_FRESH" "$(stub_field '.tool=="kb_ingest_session" and .project=="gamma"' '.transcript_bytes')" \
  "gamma sent its whole $SZ_FRESH bytes"
tail -c +301 "$T_PART" >"$HOME/expect.beta"
stub_concat '.tool=="kb_ingest_session" and .project=="beta"' '.transcript' >"$HOME/got.beta"
if cmp -s "$HOME/expect.beta" "$HOME/got.beta"; then
  pass "beta's submission is byte-for-byte the unflushed tail"
else
  fail "beta's submission is not the unflushed tail (already-ingested bytes were resent?)"
fi

step "the checkpoints advanced -- nothing is left to send"
run_bf
assert_eq "beta s-partial $SZ_PART $SZ_PART 0 flushed" "$(bf_row s-partial)" "beta is drained"
assert_eq "gamma s-fresh $SZ_FRESH $SZ_FRESH 0 flushed" "$(bf_row s-fresh)" "gamma is drained"
assert_contains "$OUT" "0 with unflushed bytes (0 bytes)" "the box has no unflushed remainder left"

step "re-running the sweep is a NO-OP (a recovery must not duplicate knowledge)"
stub_reset
run_bf --push
assert_eq "0" "$RC" "the second sweep exits 0"
settle
settle
assert_eq "0" "$(stub_total)" "zero requests on the second --push"
assert_contains "$OUT" "pushed=0 deferred=0 failed=0" "and it says so"

step "the skipped transcript was never submitted, on either run"
assert_eq "0" "$(stub_count '.tool=="kb_ingest_session" and (.transcript|test("backfill line 0 y+")) and .project=="?"')" \
  "no submission was filed under a guessed project"
run_bf
assert_eq "? s-nocwd $SZ_NOCWD 0 $SZ_NOCWD skipped" "$(bf_row s-nocwd)" \
  "it is still reported, still skipped, still with its reason"

# --------------------------------------------------------------------- the cap
step "--limit bounds a huge first sweep"
stub_reset
mkdir -p "$WORK/delta" "$WORK/epsilon"
T_D=$(mk_session "$(slug_of "$WORK/delta")" s-delta 9 "$WORK/delta")
T_E=$(mk_session "$(slug_of "$WORK/epsilon")" s-epsilon 9 "$WORK/epsilon")
touch -d @1700000500 "$T_D"
touch -d @1700000600 "$T_E"
run_bf --push --limit 1
assert_eq "1" "$(count_ingest)" "exactly one transcript went"
assert_eq "delta" "$(stub_field '.tool=="kb_ingest_session"' '.project')" "the OLDEST one"
assert_contains "$OUT" "left for the next run by --limit 1" "and it says what it deferred"
stub_reset
run_bf --push
assert_eq "1" "$(count_ingest)" "the next run picks up exactly the remaining one"
assert_eq "epsilon" "$(stub_field '.tool=="kb_ingest_session"' '.project')" "epsilon"

step "no credential ever reaches the sweep's own output"
assert_not_contains "$(cat "$HOME/bf.out")" "$RIG_TOKEN" "the report prints no token"
LEAKED=$(grep -c "$RIG_TOKEN" "$LOGF" 2>/dev/null) || LEAKED=0
assert_eq "0" "$LEAKED" "nor does the flush log"

finish

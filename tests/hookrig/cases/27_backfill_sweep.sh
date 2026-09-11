#!/usr/bin/env bash
# CASE: kb-flush-backfill.sh -- the sweep on OFFSET ACCOUNTING (wave C item C7,
#       KB 33784; root cause KB 33783). REWRITTEN 2026-09-11: the `.backfill-done`
#       ledger is gone -- the hook's never-deleted checkpoint is the only record.
# PINS: what leaves a box under the sweep is EXACTLY the bytes past each
#       session's checkpoint, and nothing else:
#         * a SEEDED transcript with a tail (offset 300 of N) -> only the tail
#           leaves, byte for byte, under its own session identity;
#         * a transcript with NO checkpoint that the hook's own rule would NOT
#           send from 0 (here: no timestamp in its records, so undatable against
#           .checkpoints-trusted-since) is `unknown`: listed, never sent by a
#           bare --push (an older, offset-deleting hook may already have drained
#           it) -- `--push --include-unknown` is the explicit, discouraged opt-in
#           and is the positive control here. (A no-checkpoint transcript whose
#           first record post-dates the stamp is `ready` at 0 -- case 33 §1c.)
#         * a FLUSHED transcript (offset == size) sends nothing;
#         * the old ledger file is INERT: a `.backfill-done` claiming a session
#           fully accounted for changes nothing, and the sweep never rewrites it;
#         * DRY RUN BY DEFAULT -- a bare invocation sends NOTHING and moves no
#           offset; a second --push is a no-op (idempotent, the hook owns the
#           checkpoints); the hook is fed `hook_event_name=Backfill`, never
#           SessionEnd (the log line says ev=Backfill);
#         * NEVER GUESSED INTO THE WRONG PROJECT: a transcript whose cwd cannot
#           be recovered is `skipped` with its reason;
#         * --limit / --project keep working for the owner; a bad flag exits 1.
#       Finally: it is NOT a hook. install.sh installs it and registers no event.
. /rig/lib.sh

new_home backfill
stub_reset
cfg_mcp
install_hooks >/dev/null

BF="$HOOKS/kb-flush-backfill.sh"
PROJDIR="$HOME/.claude/projects"
WORK="$HOME/work"
ST="$HOOKS/state"
mkdir -p "$ST"

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
REGISTERED=$(grep -c 'kb-flush-backfill' "$SETTINGS" 2>/dev/null) || REGISTERED=0
assert_eq "0" "$REGISTERED" "settings.json registers no event for it (it is owner-run / timer-run, not a hook)"

step "it carries the SAME portable block as the four hook scripts"
extract_block "$SKILL/install.sh" "$HOME/block.ref"
extract_block "$BF" "$HOME/block.bf"
if diff -u "$HOME/block.ref" "$HOME/block.bf" >"$HOME/blockdiff"; then
  pass "the block is byte-identical to install.sh's"
else
  fail "the backfill's portable block DRIFTED: $(head -c 300 "$HOME/blockdiff")"
fi

# ------------------------------------------------------------------- the fixture
# Four transcripts covering the states the sweep has to tell apart. Every mtime
# is far in the past so the quiet window (KB_SWEEP_QUIET_SECONDS) never applies.
mkdir -p "$WORK/alpha" "$WORK/beta" "$WORK/gamma"
T_DONE=$(mk_session "$(slug_of "$WORK/alpha")" s-done 12 "$WORK/alpha")
T_PART=$(mk_session "$(slug_of "$WORK/beta")" s-partial 20 "$WORK/beta")
T_FRESH=$(mk_session "$(slug_of "$WORK/gamma")" s-fresh 15 "$WORK/gamma")
T_NOCWD=$(mk_session "-nowhere-under-this-box" s-nocwd 8 "")

SZ_DONE=$(wc -c <"$T_DONE" | tr -d ' ')
SZ_PART=$(wc -c <"$T_PART" | tr -d ' ')
SZ_FRESH=$(wc -c <"$T_FRESH" | tr -d ' ')
SZ_NOCWD=$(wc -c <"$T_NOCWD" | tr -d ' ')
REM_PART=$((SZ_PART - 300))

printf '%s' "$SZ_DONE" >"$ST/s-done.offset" # fully flushed already
printf '300' >"$ST/s-partial.offset"        # flushed up to byte 300 (a line boundary is not required of the fixture)
# The INERT ledger: claims s-partial is fully accounted for. Must change nothing.
printf 's-partial\t%s\n' "$SZ_PART" >"$ST/.backfill-done"
cp "$ST/.backfill-done" "$HOME/ledger.before"

touch -d @1700000100 "$T_PART"
touch -d @1700000200 "$T_FRESH"
touch -d @1700000300 "$T_DONE"
touch -d @1700000400 "$T_NOCWD"

# ------------------------------------------------------------------- the dry run
step "a bare invocation TABLES all four on offset accounting and sends nothing"
run_bf
assert_eq "0" "$RC" "the sweep exits 0"
assert_eq "alpha s-done $SZ_DONE $SZ_DONE 0 flushed" "$(bf_row s-done)" \
  "offset == size -> flushed, no remainder"
assert_eq "beta s-partial $SZ_PART 300 $REM_PART ready" "$(bf_row s-partial)" \
  "a seeded session offers ONLY the bytes past its checkpoint (the ledger is ignored)"
assert_eq "gamma s-fresh $SZ_FRESH - $SZ_FRESH unknown" "$(bf_row s-fresh)" \
  "no checkpoint and no timestamp (undatable against the trust stamp) -> unknown (never sent by the sweep on its own)"
assert_eq "? s-nocwd $SZ_NOCWD - $SZ_NOCWD skipped" "$(bf_row s-nocwd)" \
  "an unrecoverable cwd is SKIPPED, not filed under a guessed project"
assert_file_contains "$HOME/bf.out" "no cwd in the first" "and the skip states its reason"
assert_file_contains "$HOME/bf.out" "no checkpoint" "the unknown row explains itself"
assert_contains "$OUT" "DRY RUN" "the default run says it sent nothing"
assert_contains "$OUT" "1 with unflushed bytes ($REM_PART bytes)" "the ready total is the seeded tail only"
assert_contains "$OUT" "1 unknown ($SZ_FRESH bytes, never swept -- see --seed)" "the unknown total is reported apart, with the way out"

settle
settle
assert_eq "0" "$(stub_total)" "a DRY RUN made no transport call at all"
assert_eq "300" "$(cat "$ST/s-partial.offset")" "and moved no offset"

step "--project narrows the sweep to one project"
run_bf --project beta
assert_eq "beta s-partial $SZ_PART 300 $REM_PART ready" "$(bf_row s-partial)" "beta is listed"
assert_eq "" "$(bf_row s-fresh)" "gamma is not"
assert_eq "" "$(bf_row s-done)" "alpha is not"

# ---------------------------------------------------------------------- the push
step "--push fires EXACTLY the seeded tail; unknown and flushed send nothing"
stub_reset
run_bf --push
assert_eq "0" "$RC" "the sweep exits 0"
assert_contains "$OUT" "pushed=1 partial=0 failed=0" "the summary counts one push"
assert_eq "1" "$(count_ingest)" "ONE submission, not four"
assert_eq "beta" "$(stub_field '.tool=="kb_ingest_session"' '.project')" "beta only -- nothing for alpha, gamma or the skipped one"
assert_eq "session:s-partial" "$(stub_field '.tool=="kb_ingest_session"' '.session_uri')" "under its own session identity"
assert_eq "$REM_PART" "$(stub_field '.tool=="kb_ingest_session"' '.transcript_bytes')" \
  "beta sent $REM_PART bytes (size $SZ_PART minus offset 300)"
tail -c +301 "$T_PART" >"$HOME/expect.beta"
stub_concat '.tool=="kb_ingest_session"' '.transcript' >"$HOME/got.beta"
if cmp -s "$HOME/expect.beta" "$HOME/got.beta"; then
  pass "beta's submission is byte-for-byte the unflushed tail"
else
  fail "beta's submission is not the unflushed tail (already-ingested bytes were resent?)"
fi
assert_file_missing "$ST/s-fresh.offset" "the unknown transcript was not touched (no checkpoint appeared)"
assert_eq "$SZ_DONE" "$(cat "$ST/s-done.offset")" "the flushed transcript's checkpoint is unchanged"
assert_eq "$SZ_PART" "$(cat "$ST/s-partial.offset")" "beta's checkpoint advanced to its size"
if cmp -s "$HOME/ledger.before" "$ST/.backfill-done"; then
  pass "the inert ledger was neither read (beta still went) nor rewritten"
else
  fail "the sweep rewrote .backfill-done -- the ledger is supposed to be inert"
fi

step "the hook was fed a Backfill event, never SessionEnd"
assert_file_contains "$LOGF" "ev=Backfill sid=s-partia" "the hook's summary line carries ev=Backfill"
assert_eq "0" "$(log_count 'ev=SessionEnd')" "no SessionEnd was synthesised"
assert_file_contains "$LOGF" "backfill s-partia beta pushed base=300 size=$SZ_PART sent=$REM_PART" "the sweep's own line: sid8 project verdict base size sent"

step "the checkpoints advanced -- nothing seeded is left to send"
run_bf
assert_eq "beta s-partial $SZ_PART $SZ_PART 0 flushed" "$(bf_row s-partial)" "beta is drained"
assert_eq "gamma s-fresh $SZ_FRESH - $SZ_FRESH unknown" "$(bf_row s-fresh)" "gamma is still unknown, still listed"
assert_contains "$OUT" "0 with unflushed bytes (0 bytes)" "no seeded remainder left"

step "re-running --push is a NO-OP (a recovery must not duplicate knowledge)"
stub_reset
run_bf --push
assert_eq "0" "$RC" "the second sweep exits 0"
settle
settle
assert_eq "0" "$(stub_total)" "zero requests on the second --push"
assert_contains "$OUT" "pushed=0 partial=0 failed=0" "and it says so"

step "--push --include-unknown is the explicit opt-in: gamma goes from 0, the skipped one still does not"
stub_reset
run_bf --push --include-unknown
assert_eq "0" "$RC" "exits 0"
assert_eq "1" "$(count_ingest)" "exactly one submission (gamma)"
assert_eq "gamma" "$(stub_field '.tool=="kb_ingest_session"' '.project')" "gamma"
assert_eq "$SZ_FRESH" "$(stub_field '.tool=="kb_ingest_session"' '.transcript_bytes')" "sent whole, from 0"
assert_eq "$SZ_FRESH" "$(cat "$ST/s-fresh.offset" 2>/dev/null)" "gamma now has a checkpoint at its size"
run_bf
assert_eq "gamma s-fresh $SZ_FRESH $SZ_FRESH 0 flushed" "$(bf_row s-fresh)" "gamma reads flushed afterwards"
assert_eq "? s-nocwd $SZ_NOCWD - $SZ_NOCWD skipped" "$(bf_row s-nocwd)" "the cwd-less transcript is still skipped, still with its reason"
assert_eq "0" "$(stub_count '.tool=="kb_ingest_session" and (.transcript|test("backfill line 0 y+")) and .project=="?"')" \
  "no submission was ever filed under a guessed project"

# --------------------------------------------------------------------- the cap
step "--limit bounds a big sweep, oldest first"
stub_reset
mkdir -p "$WORK/delta" "$WORK/epsilon"
T_D=$(mk_session "$(slug_of "$WORK/delta")" s-delta 9 "$WORK/delta")
T_E=$(mk_session "$(slug_of "$WORK/epsilon")" s-epsilon 9 "$WORK/epsilon")
printf 0 >"$ST/s-delta.offset"   # seeded at 0: the owner said "nothing captured yet"
printf 0 >"$ST/s-epsilon.offset"
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

step "usage errors exit 1"
run_bf --bogus-flag
assert_eq "1" "$RC" "an unknown flag exits 1"
run_bf --limit x
assert_eq "1" "$RC" "a non-numeric --limit exits 1"

step "no credential ever reaches the sweep's own output"
assert_not_contains "$(cat "$HOME/bf.out")" "$RIG_TOKEN" "the report prints no token"
LEAKED=$(grep -c "$RIG_TOKEN" "$LOGF" 2>/dev/null) || LEAKED=0
assert_eq "0" "$LEAKED" "nor does the flush log"

finish

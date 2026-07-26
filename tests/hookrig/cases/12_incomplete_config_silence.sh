#!/usr/bin/env bash
# EXPECT: pass  (was xfail; the silent no-op was FIXED — the hook now logs NOT CONFIGURED
#               and the pre-brief surfaces it. Regression gate: this must stay green.)
# CASE: an INCOMPLETE transport config is a permanent, unlogged no-op
# PINS: the hole this rig found. install.sh's REMOTE fallback writes
#         KB_REST_URL=https://api.example.com
#         KB_TOKEN=
#       (install.sh:73-76) and tells the operator, ONCE, on stdout, to fill the
#       token in. Until they do, every flush takes the `elif` at
#       flush-session-to-kb.sh:58 -- which requires a NON-EMPTY KB_TOKEN -- falls
#       through every branch, and exits 0 having written NOTHING to
#       flush-session.log. The pre-brief hook's failure surfacing
#       (kb-session-brief.sh:106-116) greps that same log, so it stays quiet too:
#       a machine in this state captures nothing, forever, with no signal
#       anywhere. Contrast case 10, where a REACHABLE-but-broken transport is
#       always logged.
#
#       This case asserts the behaviour we WANT (a log line naming the
#       misconfiguration). It is marked xfail: the rig reports it as a known
#       defect instead of a red suite. When the hook is fixed this case starts
#       PASSING, the runner reports XPASS (a failure), and the marker gets
#       removed -- which is exactly when the KB fact should be corrected.
. /rig/lib.sh

new_home incomplete
WORK="$HOME/work/silentrepo"
mkdir -p "$WORK"
T="$WORK/transcript.jsonl"
mk_transcript "$T" 12 silent

step "install with no preconditions -> the REMOTE placeholder config"
install_hooks >/dev/null
assert_file_contains "$CFG" "KB_REST_URL=https://api.example.com" "placeholder url written"
assert_eq "KB_TOKEN=" "$(grep '^KB_TOKEN=' "$CFG")" "token left empty for the operator"

step "a flush in that state"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-silent PreCompact)"
assert_eq "0" "$RC" "hook exits 0 (correct: the session must never break)"
assert_empty_file "$OUT_FILE" "stdout empty (correct)"
settle
settle

step "WANTED: the operator learns their KB is capturing nothing"
if [ -s "$LOGF" ]; then
  pass "flush-session.log explains the misconfiguration: $(tail -1 "$LOGF")"
else
  fail "SILENT NO-OP: nothing was submitted and flush-session.log is empty/absent"
fi

step "and the next session's pre-brief warns about it"
stub_reset
fire "$HOOKS/kb-session-brief.sh" "$(start_event startup "$WORK")"
assert_contains "$OUT" "session-flush" "SessionStart surfaces the dead transport"

step "same hole with an MCP url and no bearer"
new_home incomplete_mcp
printf '# MCP\nKB_MCP_URL=%s/mcp\nKB_TOKEN=\n' "$STUB" | write_cfg
install_hooks >/dev/null
WORK="$HOME/work/silentrepo"
mkdir -p "$WORK"
mk_transcript "$WORK/t.jsonl" 12 silent
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WORK/t.jsonl" "$WORK" sess-silent2 PreCompact)"
assert_eq "0" "$RC" "hook exits 0"
settle
settle
assert_eq "0" "$(stub_total)" "confirmed: nothing is submitted with an empty bearer"
if [ -s "$LOGF" ]; then
  pass "the empty bearer is logged: $(tail -1 "$LOGF")"
else
  fail "SILENT NO-OP: empty KB_TOKEN with a valid KB_MCP_URL logs nothing"
fi

finish

#!/usr/bin/env bash
# IMAGE: nojson
# CASE: a box with NEITHER jq NOR python3 -- fail LOUD, never silent
# PINS: the two halves of the same bug class, on the one box where nothing can
#       be parsed at all.
#   1. INSTALL must fail loudly and ACTIONABLY, without sudo. The old message was
#      `need jq (apt install jq)` (install.sh:7) -- useless on a locked-down
#      machine, where apt is exactly what you do not have. The new message must
#      name the sudo-free routes (~/.local/bin, a jq beside the hooks, KB_JQ) and
#      must NOT say "apt install".
#   2. A HOOK in that state must NOT exit 0 in silence. That is the defect rig
#      case 12 caught (item 1174, the silent no-op): the machine captures nothing
#      forever, and because the SessionStart pre-brief surfaces failures by
#      grepping flush-session.log, the warning stays silent too. Both hooks must
#      now write a rate-limited "NOT CONFIGURED" line -- the exact token the
#      pre-brief's awk pattern (kb-session-brief.sh) already matches -- so the
#      NEXT session is told. The brief hook was previously the only one that
#      could never speak; it speaks now (into the log, never into stdout, which
#      would inject text into the model's context).
# ALSO PINS: the rate limit is per hook and one line an HOUR (the pattern added
#       for the no-transport case), so an unconfigured box does not spam the log
#       on every PreCompact + SessionEnd.
. /rig/lib.sh

step "precondition: no JSON tool of any kind on PATH"
for t in jq jq.exe python3 python; do
  if command -v "$t" >/dev/null 2>&1; then fail "$t is on PATH -- this image proves nothing"; else pass "no $t"; fi
done

# -------------------------------------------------------------- 1. loud install
new_home nojson_install
OUT_INST=$(install_hooks 2>&1)
RC_INST=$?
assert_ne "0" "$RC_INST" "install.sh FAILS (it cannot honestly claim to have installed)"
assert_contains "$OUT_INST" "no JSON tool" "the failure names the actual problem"
assert_contains "$OUT_INST" "~/.local/bin" "tells you where to drop a static jq (no sudo)"
assert_contains "$OUT_INST" "KB_JQ" "tells you about the KB_JQ override"
assert_contains "$OUT_INST" "$HOME/.claude/hooks/jq" "tells you the co-location path"
assert_contains "$OUT_INST" "python3" "mentions the python3 alternative"
assert_not_contains "$OUT_INST" "apt install" "NEVER the sudo-only advice again"
assert_not_contains "$OUT_INST" "apt-get" "no other package-manager advice either"

step "it stopped before touching settings.json (a half-install is worse)"
assert_file_missing "$SETTINGS" "no settings.json written"

# ------------------------------------------------------- 2. the hooks stay loud
# install.sh refused, so the hooks are exercised straight from the (read-only)
# skill mount -- exactly the state a machine is in when jq disappears from PATH
# AFTER a successful install.
step "a flush hook fired in that state: exit 0, silent stdout, LOUD log"
mkdir -p "$HOME/.claude"
printf '# MCP\nKB_MCP_URL=%s/mcp\nKB_TOKEN=%s\n' "$STUB" "$RIG_TOKEN" >"$CFG"
WORK="$HOME/work/nojsonproj"
mkdir -p "$WORK"
mk_transcript "$WORK/t.jsonl" 5 nojson
stub_reset
fire "$SKILL/flush-session-to-kb.sh" "$(flush_event "$WORK/t.jsonl" "$WORK" sess-nojson PreCompact)"
assert_eq "0" "$RC" "hook exits 0 -- compaction is never blocked"
assert_empty_file "$OUT_FILE" "nothing on stdout (hook stdout is injected context)"
if [ -s "$LOGF" ]; then
  pass "flush-session.log was written: $(tail -1 "$LOGF")"
else
  fail "SILENT DEATH: no JSON tool and flush-session.log is empty/absent"
fi
assert_file_contains "$LOGF" "NOT CONFIGURED" "uses the token the pre-brief greps for"
assert_file_contains "$LOGF" "flush NOT CONFIGURED" "tagged as the flush hook"
assert_file_contains "$LOGF" "~/.local/bin" "the log line is actionable too"
assert_file_contains "$LOGF" "KB_JQ" "and names the override"
assert_not_contains "$(cat "$LOGF")" "apt install" "no sudo-only advice in the log"
assert_eq "1" "$(wc -l <"$LOGF")" "exactly one line so far"
settle
assert_eq "0" "$(stub_total)" "nothing was submitted (it could not parse the event at all)"

step "the brief hook is no longer the silent one either (independent stamp)"
fire "$SKILL/kb-session-brief.sh" "$(start_event startup "$WORK")"
assert_eq "0" "$RC" "brief exits 0 -- a prompt is never broken"
assert_empty_file "$OUT_FILE" "brief prints nothing (no context injection)"
assert_eq "2" "$(wc -l <"$LOGF")" "the brief added its own line despite the flush stamp"
assert_file_contains "$LOGF" "brief NOT CONFIGURED" "tagged as the brief hook"

step "rate limit: further firings within the hour add no lines"
fire "$SKILL/kb-session-brief.sh" "$(start_event startup "$WORK")"
fire "$SKILL/kb-session-brief.sh" "$(start_event clear "$WORK")"
fire "$SKILL/flush-session-to-kb.sh" "$(flush_event "$WORK/t.jsonl" "$WORK" sess-nojson2 SessionEnd)"
assert_eq "2" "$(wc -l <"$LOGF")" "still one line an hour PER HOOK (no log spam)"

step "and the pre-brief's own failure surfacing reads it back"
# The warning path needs a hook that CAN parse events, so re-run the brief with a
# JSON tool available (KB_JQ, the sudo-free override) against the same log.
export KB_JQ="$RIG_JQ"
fire "$SKILL/kb-session-brief.sh" "$(start_event startup "$WORK")"
assert_contains "$OUT" "session-flush reported a recent failure" "next session is warned"
assert_contains "$OUT" "NOT CONFIGURED" "with the actual line"

finish

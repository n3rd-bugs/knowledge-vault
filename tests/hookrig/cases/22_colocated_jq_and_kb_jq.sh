#!/usr/bin/env bash
# IMAGE: nojson
# CASE: links 2 and 3 of the JSON backend chain -- a CO-LOCATED jq and $KB_JQ
# PINS: the two sudo-free ways to give a locked-down box a JSON tool when the
#       admins will never put one on PATH. This case runs on the image with NO
#       jq and NO python3, so the ONLY way anything here can work is the link
#       under test -- there is no fallback left to accidentally pass through.
#         link 2: a jq sitting next to the hooks (~/.claude/hooks/jq). The
#                 installer already did this for Windows (old install.sh:98,
#                 KB item 496: Claude Code can inherit a PATH from before jq was
#                 installed); it is now every platform's escape hatch, and the
#                 hooks look there explicitly as well as via PATH.
#         link 3: KB_JQ=/path/to/jq, the override named in the failure message
#                 case 21 pins -- so the advice we print is advice that works.
# ALSO PINS: the installer PROPAGATES whichever jq it used into the hooks dir,
#       so a KB_JQ-only install leaves a box that keeps working after the
#       operator's shell (and their KB_JQ export) is gone.
. /rig/lib.sh

step "precondition: nothing on PATH can parse JSON"
if command -v jq >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1; then
  fail "a JSON tool is on PATH -- this case would prove nothing"
else
  pass "no jq, no python3 on PATH"
fi

# ------------------------------------------------- link 2: jq beside the hooks
new_home colocated
mkdir -p "$HOOKS"
cp "$RIG_JQ" "$HOOKS/jq"
chmod +x "$HOOKS/jq"
cfg_mcp

OUT_INST=$(install_hooks 2>&1)
RC_INST=$?
assert_eq "0" "$RC_INST" "install succeeds using the jq beside the hooks"
assert_contains "$OUT_INST" "$HOOKS/jq" "installer reports the co-located jq as its backend"
assert_eq "5" "$("$HOOKS/jq" -r '.hooks | length' "$SETTINGS")" "five events registered"

step "a flush works with the co-located jq alone"
WORK="$HOME/work/colocated-proj"
mkdir -p "$WORK"
mk_transcript "$WORK/t.jsonl" 11 colo
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WORK/t.jsonl" "$WORK" sess-colo PreCompact)"
assert_eq "0" "$RC" "hook exits 0"
if wait_stub 1 25; then pass "the submission landed"; else fail "nothing reached the stub"; fi
assert_eq "colocated-proj" "$(stub_field '.tool=="kb_ingest_session"' '.project')" "correct project"
assert_eq "11" "$(stub_field '.tool=="kb_ingest_session"' '.transcript_lines')" "all lines arrived"
if wait_log ' ok bytes=' 15; then pass "success logged"; else fail "no ok line"; fi
assert_not_contains "$(cat "$LOGF")" "NOT CONFIGURED" "no false no-JSON warning was logged"

step "the brief hook resolves it too (recall is not a second-class path)"
stub_reset
fire "$HOOKS/kb-session-brief.sh" "$(prompt_event "how does the hook chunking offset behave" "$WORK")"
assert_contains "$OUT" "KB recall" "recall ran and printed hits"
assert_eq "1" "$(stub_count '.tool=="kb_search"')" "one kb_search call"

# ------------------------------------------------------- link 3: the KB_JQ env
new_home kbjq_override
mkdir -p "$HOME/tools"
cp "$RIG_JQ" "$HOME/tools/jq"
chmod +x "$HOME/tools/jq"
export KB_JQ="$HOME/tools/jq" # nothing on PATH, nothing beside the hooks
cfg_mcp

OUT_INST=$(install_hooks 2>&1)
RC_INST=$?
assert_eq "0" "$RC_INST" "install succeeds via KB_JQ"
assert_contains "$OUT_INST" "$HOME/tools/jq" "installer reports the KB_JQ binary as its backend"

step "the installer co-locates it, so the box survives losing KB_JQ"
if [ -x "$HOOKS/jq" ]; then pass "jq propagated to $HOOKS/jq"; else fail "no jq beside the hooks after install"; fi

WORK="$HOME/work/kbjq-proj"
mkdir -p "$WORK"
mk_transcript "$WORK/t.jsonl" 7 kbjq
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WORK/t.jsonl" "$WORK" sess-kbjq PreCompact)"
if wait_stub 1 25; then pass "flush submitted with KB_JQ set"; else fail "nothing reached the stub"; fi
assert_eq "kbjq-proj" "$(stub_field '.tool=="kb_ingest_session"' '.project')" "correct project"

step "and again with KB_JQ UNSET -- the co-located copy carries it"
unset KB_JQ
rm -rf "$HOME/tools" # the operator's copy is gone; only the installer's remains
rm -f "$HOME/.claude/hooks/state/"*.offset
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WORK/t.jsonl" "$WORK" sess-kbjq2 PreCompact)"
if wait_stub 1 25; then pass "flush still submits from the co-located copy"; else fail "the install did not survive losing KB_JQ"; fi

finish

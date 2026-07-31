#!/usr/bin/env bash
# CASE: a HOME and a project directory that contain SPACES
# PINS: "C:\Users\First Last\..." is the normal case on Windows, and "~/My
#       Projects/..." is common on macOS -- both mean every $VAR that holds a
#       path must be quoted in all four scripts. One unquoted expansion turns a
#       flush into a silent no-op (word splitting produces a path that does not
#       exist, and the hook's own contract is to exit 0 quietly), which is the
#       hardest failure mode to notice: item 1174's bug class again.
#       This case therefore drives the WHOLE loop -- install, PreCompact flush,
#       SessionEnd flush with an offset, per-prompt recall and the SessionStart
#       pre-brief -- through a HOME with a space and a project dir with a space,
#       plus a jq co-located under that spacey path (the co-location branch of
#       install.sh, which copies a binary between two quoted paths).
# ALSO PINS: the derived PROJECT slug of "kb hook rig" is "kb-hook-rig" (the
#       slugifier turns whitespace into a single dash), so a spacey directory
#       still produces a legal, stable project name.
. /rig/lib.sh

new_home "spacey home dir"
step "the clean-room HOME really contains a space"
case "$HOME" in *" "*) pass "HOME = [$HOME]" ;; *) fail "HOME has no space: $HOME" ;; esac

cfg_mcp
OUT_INST=$(install_hooks 2>&1)
RC_INST=$?
assert_eq "0" "$RC_INST" "install.sh survives a spacey HOME"
assert_contains "$OUT_INST" "$HOOKS/flush-session-to-kb.sh" "it reports the spacey hook path"
if [ -x "$HOOKS/flush-session-to-kb.sh" ]; then pass "hook installed under the spacey path"; else fail "hook missing"; fi
if [ -x "$HOOKS/kb-session-brief.sh" ]; then pass "brief installed"; else fail "brief missing"; fi
assert_eq "5" "$("$RIG_JQ" -r '.hooks | length' "$SETTINGS")" "five events registered"
assert_eq "$HOOKS/flush-session-to-kb.sh" "$("$RIG_JQ" -r '.hooks.PreCompact[0].hooks[0].command' "$SETTINGS")" "the registered command keeps the space, unmangled"

step "the installer co-located jq under the spacey path too"
if [ -x "$HOOKS/jq" ]; then pass "jq co-located at [$HOOKS/jq]"; else fail "no co-located jq"; fi

WORK="$HOME/work/kb hook rig"
mkdir -p "$WORK"
mk_transcript "$WORK/session transcript.jsonl" 14 spacey

step "a PreCompact flush of a spacey transcript path"
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WORK/session transcript.jsonl" "$WORK" "sess spacey" PreCompact)"
assert_eq "0" "$RC" "hook exits 0"
assert_empty_file "$OUT_FILE" "stdout empty"
if wait_stub 1 25; then pass "the submission landed"; else fail "nothing submitted -- a path was probably word-split"; fi
assert_eq "kb-hook-rig" "$(stub_field '.tool=="kb_ingest_session"' '.project')" "spacey dir -> kb-hook-rig slug"
assert_eq "14" "$(stub_field '.tool=="kb_ingest_session"' '.transcript_lines')" "all 14 lines arrived"
if wait_log ' ok bytes=' 15; then pass "logged to the spacey log path"; else fail "no ok line in $LOGF"; fi

step "the offset checkpoint file works under the spacey HOME"
OFFCOUNT=$(find "$HOME/.claude/hooks/state" -name '*.offset' | wc -l | tr -d ' ')
assert_eq "1" "$OFFCOUNT" "exactly one offset file was written"
SZ=$(wc -c <"$WORK/session transcript.jsonl" | tr -d ' ')
assert_eq "$SZ" "$(cat "$HOME/.claude/hooks/state/"*.offset)" "offset advanced to the full size"

step "a SessionEnd flush of the APPENDED bytes only (delta still works)"
mk_transcript "$HOME/more.jsonl" 6 spacey2
cat "$HOME/more.jsonl" >>"$WORK/session transcript.jsonl"
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WORK/session transcript.jsonl" "$WORK" "sess spacey" SessionEnd)"
if wait_stub 1 25; then pass "the delta submission landed"; else fail "nothing submitted"; fi
assert_eq "6" "$(stub_field '.tool=="kb_ingest_session"' '.transcript_lines')" "only the 6 NEW lines were sent"

step "per-prompt recall and the pre-brief work from a spacey cwd"
stub_reset
fire "$HOOKS/kb-session-brief.sh" "$(prompt_event "how does the chunk offset resume after a failure" "$WORK")"
assert_contains "$OUT" "KB recall · project kb-hook-rig" "recall ran with the slugified project"
assert_eq "1" "$(stub_count '.tool=="kb_search"')" "one kb_search"
stub_reset
fire "$HOOKS/kb-session-brief.sh" "$(start_event startup "$WORK")"
assert_contains "$OUT" "KB pre-brief · project kb-hook-rig" "SessionStart pre-brief ran"
assert_not_contains "$OUT" "reported a recent failure" "and it saw no failures (nothing broke quietly)"

finish

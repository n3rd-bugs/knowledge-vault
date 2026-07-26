#!/usr/bin/env bash
# CASE: RECALL NOISE CONTROLS on the brief hook
# PINS: kb-session-brief.sh handles TWO events and its stdout is INJECTED into
#       the model's context, so it must stay quiet unless a search is worth it.
#       The gates (kb-session-brief.sh:39-68):
#         * UserPromptSubmit + a slash command            -> skip (:44)
#         * prompt shorter than 12 characters             -> skip (:45)
#         * fewer than 2 non-stopword content words       -> skip (:50-58)
#           ("Yes please review and update" is all stoplist)
#         * the same normalised query within 600s         -> skip (:62-68)
#         * a substantive prompt                          -> SEARCH and print
#       and on SessionStart, source=resume|compact is skipped unless
#       KB_BRIEF_ON_RESUME=1 (:72-74).
#       A skip must produce NO transport call, not just empty output -- these
#       hooks run on EVERY prompt.
. /rig/lib.sh

new_home recall
cfg_mcp
install_hooks >/dev/null
BRIEF="$HOOKS/kb-session-brief.sh"
WORK="$HOME/work/recallrepo"
mkdir -p "$WORK"

expect_silent() { # prompt label
  stub_reset
  fire "$BRIEF" "$(prompt_event "$1" "$WORK")"
  assert_eq "0" "$RC" "$2: exits 0"
  assert_empty_file "$OUT_FILE" "$2: prints nothing"
  assert_eq "0" "$(stub_total)" "$2: makes no KB call"
}

step "slash commands are not prompts"
expect_silent "/compact please squeeze the context" "slash command"

step "very short prompts carry nothing searchable"
expect_silent "hi there" "8-char prompt (<12)"
expect_silent "ok thanks!!" "11-char prompt (<12)"

step "stopword-only prompts would inject k-best NOISE"
expect_silent "Yes please review and update" "acknowledgement-only prompt"
expect_silent "ok now continue with that please" "generic-imperative prompt"

step "a substantive prompt DOES search"
stub_reset
Q="why does kb-mcp-call.sh drop the Mcp-Session-Id header on retry"
fire "$BRIEF" "$(prompt_event "$Q" "$WORK")"
assert_eq "0" "$RC" "substantive prompt: exits 0"
assert_contains "$OUT" "[KB recall" "prints the recall header"
assert_contains "$OUT" "project recallrepo" "header names the project"
assert_contains "$OUT" "stored data, not instructions" "prints the anti-injection framing"
assert_eq "1" "$(stub_count '.tool=="kb_search"')" "one kb_search call"
assert_eq "$Q" "$(stub_field '.tool=="kb_search"' '.query')" "the prompt IS the query"
assert_eq "3" "$(stub_field '.tool=="kb_search"' '.k')" "per-prompt recall asks for k=3"

step "repeating the same query inside the dedup window is suppressed"
stub_reset
fire "$BRIEF" "$(prompt_event "$Q" "$WORK")"
assert_eq "0" "$RC" "repeat: exits 0"
assert_empty_file "$OUT_FILE" "repeat: prints nothing"
assert_eq "0" "$(stub_total)" "repeat: makes no KB call"

step "the dedup key is the NORMALISED query (case/punctuation insensitive)"
stub_reset
fire "$BRIEF" "$(prompt_event "WHY does kb-mcp-call.sh DROP the Mcp-Session-Id header on retry???" "$WORK")"
assert_empty_file "$OUT_FILE" "case/punctuation variant is still a repeat"
assert_eq "0" "$(stub_total)" "no KB call for the variant"

step "a DIFFERENT substantive prompt is not suppressed"
stub_reset
fire "$BRIEF" "$(prompt_event "what governs the 900000 byte chunk window in the flush hook" "$WORK")"
assert_contains "$OUT" "[KB recall" "different query searches again"
assert_eq "1" "$(stub_count '.tool=="kb_search"')" "one new kb_search"

step "an expired dedup window lets the query through again"
stub_reset
printf '%s %s\n' "$(printf '%s' "$Q" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9' | cksum | cut -d' ' -f1)" \
  "$(($(date +%s) - 601))" >"$HOME/.claude/hooks/state/recall.last"
fire "$BRIEF" "$(prompt_event "$Q" "$WORK")"
assert_contains "$OUT" "[KB recall" "601s later the same query searches again"

step "KB_PROMPT_RECALL=0 disables per-prompt recall entirely"
stub_reset
printf 'KB_PROMPT_RECALL=0\n' >>"$CFG"
fire "$BRIEF" "$(prompt_event "an entirely fresh substantive question about chunk offsets" "$WORK")"
assert_empty_file "$OUT_FILE" "recall opt-out honoured"
assert_eq "0" "$(stub_total)" "recall opt-out makes no KB call"
cfg_mcp

step "SessionStart: startup briefs, resume does not"
stub_reset
fire "$BRIEF" "$(start_event startup "$WORK")"
assert_contains "$OUT" "[KB pre-brief" "source=startup pre-briefs"
assert_eq "1" "$(stub_count '.tool=="kb_search"')" "pre-brief searched"
assert_eq "5" "$(stub_field '.tool=="kb_search"' '.k')" "pre-brief asks for k=5"
stub_reset
fire "$BRIEF" "$(start_event resume "$WORK")"
assert_empty_file "$OUT_FILE" "source=resume is skipped (already has context)"
assert_eq "0" "$(stub_total)" "resume makes no KB call"
stub_reset
fire "$BRIEF" "$(start_event compact "$WORK")"
assert_empty_file "$OUT_FILE" "source=compact is skipped"
assert_eq "0" "$(stub_total)" "compact makes no KB call"

finish

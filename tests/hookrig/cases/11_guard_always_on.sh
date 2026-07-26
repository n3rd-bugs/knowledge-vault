#!/usr/bin/env bash
# CASE: KB_HOOK_GUARD is checked ALWAYS, in every mode, before anything else
# PINS: the correction to kb-system item 885, which had the guard INVERTED
#       (claimed the guard was mode-specific / only consulted on the connector
#       path, i.e. that a guarded hook could still do work). Ground truth: line
#       14 of BOTH flush-session-to-kb.sh and kb-session-brief.sh is
#           [ -n "${KB_HOOK_GUARD:-}" ] && exit 0
#       -- BEFORE the config is even sourced, so it cannot depend on the config.
#       The guard is what stops the CONNECTOR path (which spawns `claude -p`,
#       flush-session-to-kb.sh:164) from recursively re-triggering the hooks.
#       Each assertion here is paired with a POSITIVE CONTROL: the same firing
#       without the guard must do the work, otherwise "no call" proves nothing.
. /rig/lib.sh

new_home guard
cfg_mcp
install_hooks >/dev/null
WORK="$HOME/work/guardrepo"
mkdir -p "$WORK"
T="$WORK/transcript.jsonl"
mk_transcript "$T" 15 guard

step "MCP mode: positive control (unguarded firing DOES call the transport)"
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-guard-a PreCompact)"
wait_stub 3 25 || fail "positive control failed: unguarded flush made no call"
assert_eq "1" "$(stub_count '.tool=="kb_ingest_session"')" "unguarded flush submits"

step "MCP mode: guarded firing does NOTHING"
stub_reset
LOGLINES=$(wc -l <"$LOGF")
fire_guarded "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-guard-b PreCompact)"
assert_eq "0" "$RC" "guarded flush exits 0"
assert_empty_file "$OUT_FILE" "guarded flush prints nothing"
settle
settle
assert_eq "0" "$(stub_total)" "guarded flush makes NO transport call"
assert_eq "$LOGLINES" "$(wc -l <"$LOGF")" "guarded flush writes no log line"
assert_file_missing "$HOME/.claude/hooks/state/sess-guard-b.offset" "guarded flush leaves no checkpoint"

step "LOCAL mode: the guard is not MCP-specific"
new_home guard_local
fake_uv
fake_kb_repo "$HOME/kbrepo"
cfg_local "$HOME/kbrepo" "$HOME/bin/uv"
install_hooks >/dev/null
WORK="$HOME/work/guardrepo"
mkdir -p "$WORK"
T="$WORK/transcript.jsonl"
mk_transcript "$T" 15 guard
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-guard-c PreCompact)"
i=0
while [ ! -f "$HOME/uv-ran" ] && [ "$i" -lt 60 ]; do
  i=$((i + 1))
  sleep 0.25
done
if [ -f "$HOME/uv-ran" ]; then
  pass "positive control: unguarded LOCAL flush invoked the CLI"
  note "cli: $(head -1 "$HOME/uv-ran")"
else
  fail "positive control failed: LOCAL flush never invoked the CLI"
fi
rm -f "$HOME/uv-ran"
fire_guarded "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" sess-guard-d PreCompact)"
assert_eq "0" "$RC" "guarded LOCAL flush exits 0"
settle
settle
assert_file_missing "$HOME/uv-ran" "guarded LOCAL flush never invoked the CLI"

step "the brief hook honours the guard on both its events"
new_home guard_brief
cfg_mcp
install_hooks >/dev/null
WORK="$HOME/work/guardrepo"
mkdir -p "$WORK"
stub_reset
Q="explain the offset checkpoint file used by the flush hook"
fire "$HOOKS/kb-session-brief.sh" "$(prompt_event "$Q" "$WORK")"
assert_contains "$OUT" "[KB recall" "positive control: unguarded recall fires"
stub_reset
fire_guarded "$HOOKS/kb-session-brief.sh" "$(prompt_event "a completely different substantive question about chunking" "$WORK")"
assert_eq "0" "$RC" "guarded recall exits 0"
assert_empty_file "$OUT_FILE" "guarded recall injects nothing"
assert_eq "0" "$(stub_total)" "guarded recall makes no KB call"
stub_reset
fire_guarded "$HOOKS/kb-session-brief.sh" "$(start_event startup "$WORK")"
assert_eq "0" "$RC" "guarded SessionStart exits 0"
assert_empty_file "$OUT_FILE" "guarded SessionStart injects nothing"
assert_eq "0" "$(stub_total)" "guarded SessionStart makes no KB call"

step "the guard is checked BEFORE the config is read (no config -> still exit 0)"
new_home guard_nocfg
mkdir -p "$HOME/.claude/hooks"
install -m 0755 "$SKILL/flush-session-to-kb.sh" "$HOME/.claude/hooks/"
install -m 0755 "$SKILL/kb-session-brief.sh" "$HOME/.claude/hooks/"
WORK="$HOME/work/guardrepo"
mkdir -p "$WORK"
mk_transcript "$WORK/t.jsonl" 5 guard
fire_guarded "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WORK/t.jsonl" "$WORK" sess-guard-e PreCompact)"
assert_eq "0" "$RC" "guard + no config -> exit 0"
KB_HOOK_GUARD_VALUE=any-nonempty-value fire_guarded "$HOOKS/kb-session-brief.sh" "$(prompt_event "$Q" "$WORK")"
assert_eq "0" "$RC" "any non-empty guard value counts"
assert_empty_file "$OUT_FILE" "any non-empty guard value suppresses output"

finish

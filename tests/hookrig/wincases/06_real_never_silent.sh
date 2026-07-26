#!/usr/bin/env bash
# CASE: on Windows too, a hook must NEVER fail silently
# PINS: the hole this rig originally found (container case 12) plus the Windows
#       amplifier. Windows is where the hooks are least observable — no
#       journald, no terminal to notice a stray message — so "exit 0 and say
#       nothing" would mean a user whose sessions are silently never captured.
#       The contract: exit 0 ALWAYS (never block Claude Code), but leave a LOUD
#       line in the flush log whenever a transport is unreachable or absent.
. "$RIG_SANDBOX/winlib.sh"

new_home neversilent
load_portable_block

step "an UNREACHABLE transport: exit 0, but log loudly"
# A REMOTE config pointing at a closed port on loopback.
cfg_rest "$RIG_TOKEN" "http://127.0.0.1:9"
install_hooks >/dev/null 2>&1
PROJ="$HOME/work/deadend"
mkdir -p "$PROJ"
mk_transcript "$PROJ/t.jsonl" 12 dead
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$(win_path "$PROJ/t.jsonl")" "$(win_path "$PROJ")" win-dead)"
assert_eq "0" "$RC" "the hook exits 0 even though the transport is dead"
if wait_log "FAILED" 45; then
  pass "the failure is recorded in the flush log"
  note "log: $(grep -m1 "FAILED" "$LOGF")"
else
  fail "SILENT FAILURE: transport was unreachable and nothing was logged"
  [ -f "$LOGF" ] && note "log had: $(tail -5 "$LOGF" | tr '\n' '|')" || note "no log file at all"
fi

step "the offset must NOT advance on a failed submission"
OFF_FILE=$(ls "$HOOKS/state/"*.offset 2>/dev/null | head -1)
if [ -n "$OFF_FILE" ]; then
  OFF=$(cat "$OFF_FILE" 2>/dev/null || echo 0)
  assert_eq "0" "$OFF" "offset stayed at 0, so the content is retried next time"
else
  pass "no offset checkpoint written for a failed submission"
fi

step "NO transport configured at all: still exit 0, still loud"
new_home notconfigured
install_hooks >/dev/null 2>&1 || true
# Strip every transport key so the config selects nothing.
if [ -f "$CFG" ]; then
  grep -v "^KB_REST_URL=\|^KB_MCP_URL=\|^KB_DIR=\|^KB_CONNECTOR=" "$CFG" >"$CFG.tmp" 2>/dev/null || true
  mv "$CFG.tmp" "$CFG" 2>/dev/null || true
fi
note "config now: $(grep -v '^#' "$CFG" 2>/dev/null | grep . | tr '\n' ' ' || echo '<empty>')"
PROJ2="$HOME/work/unconfigured"
mkdir -p "$PROJ2"
mk_transcript "$PROJ2/t.jsonl" 8 noconf
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$(win_path "$PROJ2/t.jsonl")" "$(win_path "$PROJ2")" win-noconf)"
assert_eq "0" "$RC" "the unconfigured hook exits 0"
if wait_log "NOT CONFIGURED" 30; then
  pass "the unconfigured state is announced in the log"
  note "log: $(grep -m1 "NOT CONFIGURED" "$LOGF")"
else
  fail "SILENT NO-OP: no transport configured and nothing was logged"
fi

step "the brief hook surfaces the problem to the user"
fire "$HOOKS/kb-session-brief.sh" "$(start_event startup "$(win_path "$PROJ2")")"
assert_eq "0" "$RC" "the brief hook exits 0"
if [ -n "$OUT" ]; then
  note "brief stdout: $(printf '%s' "$OUT" | head -c 220)"
  assert_contains "$OUT" "NOT CONFIGURED" "the brief tells the user capture is broken"
else
  fail "the brief said nothing while the KB was misconfigured"
fi

finish

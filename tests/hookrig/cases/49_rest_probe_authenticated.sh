#!/usr/bin/env bash
# CASE: the REST reachability probe never sends a bearer-less request.
# PINS: kb-session-brief.sh probes "$KB_REST_URL" before the REST search so a
#       dead KB fails in ~3 s, not at the search's ceiling. Sent BARE, that probe
#       was a 401 on kb-rest at every firing -- a `401 unauthorized ... GET /`
#       WARNING and one more hit on the 401-burst alert (src/kbrest/auth_alert.py,
#       20 per 10 min); on 2026-10-29 it was every such line kb-rest logged (42,
#       from three boxes). Now:
#         * UserPromptSubmit: the probe carries the bearer (stdin config, never
#           argv): ONE GET / with the credential, THEN the k=3 search; nothing
#           bearer-less reaches the server;
#         * a GATED prompt (slash command, <12 chars, stopword-only, the same
#           query inside the 600 s window) makes NO request at all: every gate
#           sits before the probe;
#         * SessionStart: the bearer-authenticated manifest fetch already proved
#           this base reachable, so there is NO probe (manifest, then the k=5
#           search); with KB_VERSION_CHECK=0 (no manifest) the authenticated
#           probe is back -- the positive control;
#         * never silent: an unreachable REST base still logs ONE `RECALL
#           UNAVAILABLE ... unreachable rc=7` line and trips the breaker, on a
#           prompt AND on a SessionStart whose manifest fetch failed;
#         * source pins: every curl naming the REST base reads `-K -`; the MCP
#           probe stays bare ON PURPOSE (a session-less request past the gate
#           registers a stateful MCP session that nothing expires);
#         * no token resolved: a SessionStart sends NOTHING -- the manifest route
#           sits behind the bearer gate, so a bearer-less fetch could only ever
#           be one more counted 401 (two helped fire the 2026-10-24 KB ALERT);
#         * an EMPTY-stdin prompt (the dispatcher still names the event as $1)
#           exits at the prompt gates instead of running the SessionStart branch.
#       Source: kb-session-brief.sh, the REST branch's probe, the SessionStart
#       version check (`_kb_reached`, the KB_TOKEN guard) and the $1 fallback.
# EXPECTED RED on the pre-fix hooks: A, C, D, F, G and H (a bearer-less GET / on
#       every firing, a probe after a 200 manifest, a bearer-less manifest fetch,
#       and an empty-stdin prompt briefed as a session start); B and E pass on
#       both -- they pin what the fix must NOT change.
. /rig/lib.sh

new_home probe
cfg_rest
install_hooks >/dev/null
BRIEF="$HOOKS/kb-session-brief.sh"
WORK="$HOME/work/proberepo"
mkdir -p "$WORK"
# Substantive (>= 12 chars, >= 2 content words) and rig-specific.
Q="rig probe fixture: stub bearer gate answers proberepo recall"

probes() { stub_count '.method=="GET" and .path=="/"'; }
bare() { stub_count '.path=="/" and ((.bearer // "") == "")'; }
paths() { stub_field '.path=="/" or .path=="/api/search"' '.path' | tr '\n' ' ' | sed 's/ $//'; }

step "A. a substantive prompt: the probe carries the bearer, then the k=3 search"
stub_reset
fire "$BRIEF" "$(prompt_event "$Q" "$WORK")"
assert_eq "0" "$RC" "prompt: exits 0"
assert_contains "$OUT" "[KB recall" "prompt: prints the recall block"
assert_eq "1" "$(probes)" "prompt: exactly one GET / (the probe)"
assert_eq "$RIG_TOKEN" "$(stub_field '.method=="GET" and .path=="/"' '.bearer')" \
  "prompt: the probe presented the bearer"
assert_eq "0" "$(bare)" "prompt: no bearer-less GET /"
assert_eq "0" "$(stub_count '.path=="/api/setup/manifest.json"')" "prompt: no manifest (SessionStart only)"
assert_eq "3" "$(stub_field '.path=="/api/search"' '.k')" "prompt: the k=3 search ran"
assert_eq "ok" "$(stub_field '.path=="/api/search"' '.auth')" "prompt: the search authenticated"
assert_eq "/ /api/search" "$(paths)" "prompt: probe first, search second"

step "B. a gated prompt makes NO request at all (every gate sits before the probe)"
gated() { # prompt label
  stub_reset
  fire "$BRIEF" "$(prompt_event "$1" "$WORK")"
  assert_eq "0" "$RC" "$2: exits 0"
  assert_empty_file "$OUT_FILE" "$2: prints nothing"
  assert_eq "0" "$(stub_total)" "$2: no request, not even the probe"
}
gated "/compact squeeze the context please" "slash command"
gated "hi there" "short prompt (<12)"
gated "Yes please review and update" "stopword-only prompt"
gated "$Q" "the same query inside the 600 s window"

step "C. SessionStart: the authenticated manifest proved the base reachable -> no probe"
stub_reset
fire "$BRIEF" "$(start_event startup "$WORK")"
assert_eq "0" "$RC" "start: exits 0"
assert_contains "$OUT" "[KB pre-brief" "start: prints the pre-brief"
assert_eq "1" "$(stub_count '.path=="/api/setup/manifest.json" and .status==200')" \
  "start: one authenticated manifest fetch answered"
assert_eq "0" "$(probes)" "start: NO probe after a 200 manifest"
assert_eq "0" "$(bare)" "start: no bearer-less GET /"
assert_eq "5" "$(stub_field '.path=="/api/search"' '.k')" "start: the k=5 brief search ran"

step "D. control: SessionStart with the version check off -> the probe runs, authenticated"
stub_reset
export KB_VERSION_CHECK=0
fire "$BRIEF" "$(start_event startup "$WORK")"
unset KB_VERSION_CHECK
assert_eq "0" "$RC" "no version check: exits 0"
assert_eq "0" "$(stub_count '.path=="/api/setup/manifest.json"')" "no version check: no manifest fetch"
assert_eq "1" "$(probes)" "no version check: the probe is back"
assert_eq "$RIG_TOKEN" "$(stub_field '.method=="GET" and .path=="/"' '.bearer')" \
  "no version check: and it carries the bearer"
assert_eq "5" "$(stub_field '.path=="/api/search"' '.k')" "no version check: then the k=5 search"

step "E. never silent: an unreachable REST base is logged and trips the breaker"
new_home probedead
cfg_rest "$RIG_TOKEN" "http://127.0.0.1:9"
install_hooks >/dev/null
BRIEF="$HOOKS/kb-session-brief.sh"
WORK="$HOME/work/proberepo"
mkdir -p "$WORK"
BRK="$HOOKS/state/.recall-breaker.stamp"
fire "$BRIEF" "$(prompt_event "$Q" "$WORK")"
assert_eq "0" "$RC" "dead KB, prompt: exits 0"
assert_empty_file "$OUT_FILE" "dead KB, prompt: prints nothing"
assert_eq "1" "$(log_count 'RECALL UNAVAILABLE: event=UserPromptSubmit transport=rest endpoint=http://127\.0\.0\.1:9 unreachable rc=7')" \
  "dead KB, prompt: ONE unreachable line"
if [ -f "$BRK" ]; then pass "dead KB, prompt: the breaker tripped"; else fail "dead KB, prompt: no breaker stamp"; fi
rm -f "$BRK"
fire "$BRIEF" "$(start_event startup "$WORK")"
assert_eq "0" "$RC" "dead KB, start: exits 0"
assert_eq "1" "$(log_count 'RECALL UNAVAILABLE: event=SessionStart transport=rest endpoint=http://127\.0\.0\.1:9 unreachable rc=7')" \
  "dead KB, start: a failed manifest never skips the probe"
if [ -f "$BRK" ]; then pass "dead KB, start: the breaker tripped"; else fail "dead KB, start: no breaker stamp"; fi

step "F. source pins: every curl at the REST base reads -K -; the MCP probe stays bare"
# One STATEMENT per line: backslash continuations are joined first, so the
# manifest fetch and the search (whose URL sits on a continuation line) are
# checked too, not only the one-line probe.
REST_CURLS=$(awk '!buf && /^[[:space:]]*#/ { next }
  { if (sub(/\\$/, "")) { buf = buf $0 " "; next } print buf $0; buf = "" }' "$BRIEF" |
  grep -v '^[[:space:]]*\(printf\|echo\) ' | grep 'curl -' | grep 'KB_REST_URL' || true)
N_REST=$(printf '%s\n' "$REST_CURLS" | grep -c . || true)
note "curl statements naming the REST base: $N_REST"
assert_ge "$N_REST" 3 "the manifest fetch, the probe and the search are all seen (not vacuous)"
assert_eq "" "$(printf '%s\n' "$REST_CURLS" | grep -v -- '-K -' || true)" \
  "no curl at the REST base runs without the stdin config"
assert_eq "1" "$(grep -cF 'curl -sS -4 -o /dev/null --max-time 3 "$KB_MCP_URL"' "$BRIEF")" \
  "the MCP probe is still the bare one (deliberately)"

step "G. no token resolved: a SessionStart sends nothing (no bearer-less manifest fetch)"
new_home probenotoken
printf '# REMOTE\nKB_REST_URL=%s\n' "$STUB" | write_cfg
install_hooks >/dev/null
BRIEF="$HOOKS/kb-session-brief.sh"
WORK="$HOME/work/proberepo"
mkdir -p "$WORK"
stub_reset
fire "$BRIEF" "$(start_event startup "$WORK")"
assert_eq "0" "$RC" "no token, start: exits 0"
assert_eq "0" "$(stub_count '.path=="/api/setup/manifest.json"')" "no token, start: no manifest fetch"
assert_eq "0" "$(stub_total)" "no token, start: no request reached the server"

step "H. an EMPTY-stdin prompt (event named only as \$1) is gated, never briefed"
new_home probeempty
cfg_rest
install_hooks >/dev/null
BRIEF="$HOOKS/kb-session-brief.sh"
mkdir -p "$HOME/work/proberepo"
stub_reset
OUT_FILE="$HOME/.stdout.empty"
(cd "$HOME/work/proberepo" && printf '' | "$BRIEF" UserPromptSubmit >"$OUT_FILE" 2>"$HOME/.stderr.empty")
RC=$?
assert_eq "0" "$RC" "empty stdin, \$1=UserPromptSubmit: exits 0"
assert_empty_file "$OUT_FILE" "empty stdin: no pre-brief injected into the prompt"
assert_eq "0" "$(stub_total)" "empty stdin: no manifest, no probe, no search"

finish

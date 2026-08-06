#!/usr/bin/env bash
# IMAGE: nojq
# CASE: a LOCKED-DOWN box with NO jq (and no sudo) -- python3 fallback
# PINS: install.sh:7 used to be `command -v jq || { echo "need jq (apt install
#       jq)"; exit 1; }`. On a machine without root that was a DEAD END: the
#       whole skill was uninstallable, and jq is used ~48 times across it. The
#       hooks now carry a JSON-tool abstraction whose backend chain is
#         1. jq on PATH   2. a jq co-located with the hooks   3. $KB_JQ
#         4. python3 (this case)
#       and every jq use in install.sh / flush-session-to-kb.sh /
#       kb-session-brief.sh / kb-mcp-call.sh goes through it.
# PINS (still true on the python backend, i.e. the fallback is not a lesser mode):
#   * item 1173 -- the mode chain LOCAL > MCP > CONNECTOR > REMOTE, including the
#     MCP auto-discovery from ~/.claude.json (the most complex jq program in the
#     skill: key-regex + url + bearer filtering) which has a python twin.
#   * item 1152 -- FOUR events are still registered (PreCompact, SessionEnd,
#     SessionStart, UserPromptSubmit), two scripts.
#   * a flush really SUBMITS on both network transports (MCP, then REST): the
#     request-body builder (jq --rawfile <-> python read) is the path a ~900 KB
#     chunk travels, so it must work with no jq anywhere.
# PINS (the equivalence that makes the fallback trustworthy): install.sh's two
#       settings.json merges -- the jq filter and the python read-modify-write --
#       must produce BYTE-IDENTICAL settings.json for identical input, preserving
#       foreign hooks, unrelated keys, unicode and key order. Asserted by running
#       the installer once per backend against the same seeded HOME and diffing.
. /rig/lib.sh

step "precondition: this image really has no jq, but does have python3"
if command -v jq >/dev/null 2>&1; then fail "jq is on PATH -- the nojq image is wrong"; else pass "no jq on PATH"; fi
if command -v python3 >/dev/null 2>&1; then pass "python3 present"; else fail "python3 missing"; fi

# ---------------------------------------------------------------- install works
new_home nojq_mcp
fake_claude_json # MCP precondition: a bearer-carrying kb server in ~/.claude.json
OUT_INST=$(install_hooks 2>&1)
RC_INST=$?
assert_eq "0" "$RC_INST" "install.sh SUCCEEDS with no jq (was: exit 1, apt install jq)"
assert_contains "$OUT_INST" "python3 fallback" "installer names the backend it chose"
assert_not_contains "$OUT_INST" "apt install" "no sudo-only advice anywhere in the output"

step "the mode chain still resolves through the python backend (item 1173)"
assert_file_contains "$CFG" "KB_MCP_URL=https://kb.example.invalid/mcp" "MCP url auto-discovered"
assert_file_contains "$CFG" "KB_TOKEN=kbfake_00000000000000000000000000" "bearer auto-discovered"

step "and LOCAL still outranks MCP on the python backend"
new_home nojq_local
fake_claude_json
fake_kb_repo "$HOME/data-server"
fake_uv
install_hooks >/dev/null
assert_file_contains "$CFG" "KB_DIR=$HOME/data-server" "LOCAL wins over MCP"

step "four events, two scripts (item 1152)"
new_home nojq_events
install_hooks >/dev/null
KEYS=$("$RIG_JQ" -r '.hooks | keys | join(",")' "$SETTINGS")
assert_eq "PreCompact,SessionEnd,SessionStart,Stop,UserPromptSubmit" "$KEYS" "exactly five event keys"
assert_eq "5" "$("$RIG_JQ" -r '[.hooks[][].hooks[0].command] | unique | length' "$SETTINGS")" "five dispatcher+event strings"
assert_eq "$HOOKS/kb-hook-dispatch.sh PreCompact" "$("$RIG_JQ" -r '.hooks.PreCompact[0].hooks[0].command' "$SETTINGS")" "PreCompact -> dispatcher"
assert_eq "startup|clear" "$("$RIG_JQ" -r '.hooks.SessionStart[0].matcher' "$SETTINGS")" "SessionStart matcher kept"

# --------------------------------------------------------------- a flush lands
step "MCP flush submits for real, with no jq on the box"
new_home nojq_flush
cfg_mcp
install_hooks >/dev/null
WORK="$HOME/work/nojqproj"
mkdir -p "$WORK"
mk_transcript "$WORK/t.jsonl" 20 nojq
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WORK/t.jsonl" "$WORK" sess-nojq PreCompact)"
assert_eq "0" "$RC" "hook exits 0"
assert_empty_file "$OUT_FILE" "stdout stays empty"
if wait_stub 1 25; then pass "a request reached the stub"; else fail "nothing was submitted"; fi
assert_eq "1" "$(stub_count '.tool=="kb_ingest_session"')" "one kb_ingest_session tools/call"
assert_eq "nojqproj" "$(stub_field '.tool=="kb_ingest_session"' '.project')" "project slug from cwd"
assert_eq "20" "$(stub_field '.tool=="kb_ingest_session"' '.transcript_lines')" "all 20 lines arrived"
if wait_log ' ok bytes=' 15; then pass "a success line was logged"; else fail "no ok line in the flush log"; fi

step "git context (code_ref anchoring) still rides along on the python backend"
# The old builder merged a pre-made GITCTX object into the body with jq; the
# portable builder appends repo/branch/commit as extra fields, still OMITTING
# empty ones entirely (never sent as ""). Only a git cwd produces them.
new_home nojq_git
cfg_mcp
install_hooks >/dev/null
WORK="$HOME/work/gitproj"
mkdir -p "$WORK"
git -C "$WORK" init -q 2>/dev/null
git -C "$WORK" config user.email rig@example.invalid
git -C "$WORK" config user.name rig
printf 'x\n' >"$WORK/f.txt"
git -C "$WORK" add f.txt
git -C "$WORK" commit -qm "rig commit" 2>/dev/null
mk_transcript "$WORK/t.jsonl" 5 git
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WORK/t.jsonl" "$WORK" sess-git PreCompact)"
if wait_stub 1 25; then pass "the submission landed"; else fail "nothing submitted"; fi
KEYS=$(stub_field '.tool=="kb_ingest_session"' '.arg_keys | join(",")')
assert_contains "$KEYS" "repo" "repo travelled with the body"
assert_contains "$KEYS" "branch" "branch travelled with the body"
assert_contains "$KEYS" "commit" "commit travelled with the body"
assert_eq "false" "$(stub_field '.tool=="kb_ingest_session"' '.is_error')" "server accepted the shape"

step "and a NON-git cwd omits them entirely (never empty strings)"
new_home nojq_nogit
cfg_mcp
install_hooks >/dev/null
WORK="$HOME/work/plainproj"
mkdir -p "$WORK"
mk_transcript "$WORK/t.jsonl" 5 nogit
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WORK/t.jsonl" "$WORK" sess-nogit PreCompact)"
if wait_stub 1 25; then pass "the submission landed"; else fail "nothing submitted"; fi
KEYS=$(stub_field '.tool=="kb_ingest_session"' '.arg_keys | join(",")')
# session_uri is always present now: it is the identity that lets the drain
# coalesce a session's chunks into ONE extraction instead of one per 900 KB
# window. The point of this assertion is unchanged — no EMPTY git keys may
# leak into the body — so the expected set grows by exactly that field.
assert_eq "kind,project,session_uri,transcript_gz" "$KEYS" "exactly the three base fields, no empty git keys"

step "REST flush submits too (the other body builder)"
new_home nojq_rest
cfg_rest
install_hooks >/dev/null
WORK="$HOME/work/restproj"
mkdir -p "$WORK"
mk_transcript "$WORK/t.jsonl" 8 rest
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WORK/t.jsonl" "$WORK" sess-rest PreCompact)"
if wait_stub 1 25; then pass "a REST request reached the stub"; else fail "nothing was submitted"; fi
assert_eq "202" "$(stub_field '.path=="/api/ingest-session"' '.status')" "server accepted it (202)"
assert_eq "8" "$(stub_field '.path=="/api/ingest-session"' '.transcript_lines')" "all 8 lines arrived"

step "REMOTE-mode recall: url-encoding + hit formatting, both backends, same output"
# The likeliest transport on a locked-down box is REMOTE, so the pre-brief's REST
# path must survive the fallback too: kb_urlencode (jq @uri vs urllib.quote) and
# the bullet formatter (jq string interpolation vs a python twin).
new_home nojq_restrecall
cfg_rest
install_hooks >/dev/null
WORK="$HOME/work/recallproj"
mkdir -p "$WORK"
stub_reset
fire "$HOOKS/kb-session-brief.sh" "$(prompt_event "chunk offset & resume behaviour after a 500" "$WORK")"
PY_OUT="$OUT"
assert_contains "$PY_OUT" "KB recall · project recallproj" "recall header printed"
assert_contains "$PY_OUT" "- [decision · kb-system · verified] REST hit 0 — snippet 0" "hits formatted as bullets"
assert_eq "chunk offset & resume behaviour after a 500" \
  "$(stub_field '.path=="/api/search"' '.query')" "the query survived url-encoding verbatim"
assert_eq "3" "$(stub_field '.path=="/api/search"' '.k')" "k=3 for per-prompt recall"

rm -f "$HOME/.claude/hooks/state/recall-"*.last # the 600s duplicate gate
export KB_JQ="$RIG_JQ"
stub_reset
fire "$HOOKS/kb-session-brief.sh" "$(prompt_event "chunk offset & resume behaviour after a 500" "$WORK")"
unset KB_JQ
assert_eq "$PY_OUT" "$OUT" "the jq backend produced byte-identical recall output"

# ------------------------------------------- the two backends must AGREE exactly
step "jq and python3 produce a BYTE-IDENTICAL settings.json"
new_home nojq_equiv
seed_settings() { # a settings.json with foreign hooks, unicode and mixed types
  mkdir -p "$HOME/.claude"
  cat >"$SETTINGS" <<'JSON'
{
  "model": "opus",
  "env": {"NOTE": "kept — ünicode, ampersand & slash /"},
  "hooks": {
    "PreCompact": [
      {"matcher": "", "hooks": [{"type": "command", "command": "/opt/other/hook.sh", "timeout": 9}]}
    ],
    "Stop": [
      {"matcher": "a|b", "hooks": [{"type": "command", "command": "beep", "timeout": 3}]}
    ]
  },
  "permissions": {"allow": ["Bash(ls:*)"], "deny": []},
  "n": 15, "flag": true, "nil": null
}
JSON
}

seed_settings
KB_JQ="$RIG_JQ" install_hooks >"$HOME/out.jq" 2>&1
assert_contains "$(cat "$HOME/out.jq")" "json:     jq (" "run 1 used the jq backend (KB_JQ override)"
cp "$SETTINGS" "$HOME/settings.jq.json"

rm -rf "$HOME/.claude" # a co-located jq from run 1 must not leak into run 2
seed_settings
install_hooks >"$HOME/out.py" 2>&1
assert_contains "$(cat "$HOME/out.py")" "python3 fallback" "run 2 used the python backend"
cp "$SETTINGS" "$HOME/settings.py.json"

if diff -u "$HOME/settings.jq.json" "$HOME/settings.py.json" >"$HOME/settings.diff"; then
  pass "settings.json is byte-identical across the two backends"
else
  fail "backends disagree: $(head -c 400 "$HOME/settings.diff")"
fi
assert_eq "$(sha256sum <"$HOME/settings.jq.json")" "$(sha256sum <"$HOME/settings.py.json")" "same sha256"

step "and that shared output preserved everything it had to"
assert_eq "opus" "$("$RIG_JQ" -r '.model' "$SETTINGS")" "unrelated top-level key survived"
assert_eq "kept — ünicode, ampersand & slash /" "$("$RIG_JQ" -r '.env.NOTE' "$SETTINGS")" "unicode survived verbatim"
assert_eq "/opt/other/hook.sh" "$("$RIG_JQ" -r '.hooks.PreCompact[0].hooks[0].command' "$SETTINGS")" "foreign PreCompact hook kept, first"
assert_eq "2" "$("$RIG_JQ" -r '.hooks.PreCompact | length' "$SETTINGS")" "ours appended, not replacing"
assert_eq "beep" "$("$RIG_JQ" -r '.hooks.Stop[0].hooks[0].command' "$SETTINGS")" "foreign event untouched"
assert_eq "null" "$("$RIG_JQ" -r '.nil' "$SETTINGS")" "null value preserved"

step "the python backend is idempotent too (item: re-running install is safe)"
install_hooks >/dev/null
install_hooks >/dev/null
assert_eq "2" "$("$RIG_JQ" -r '.hooks.PreCompact | length' "$SETTINGS")" "still exactly one KB entry beside the foreign one"
assert_eq "1" "$("$RIG_JQ" -r '.hooks.SessionStart | length' "$SETTINGS")" "SessionStart not duplicated"

finish

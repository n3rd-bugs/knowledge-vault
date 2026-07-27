#!/usr/bin/env bash
# CASE: one revocable token PER PROJECT, selected by the project being written
# PINS: a machine that works across several projects should not carry one broad
#       credential for all of them. Each project gets its own narrow, revocable
#       token in ~/.claude/kb-tokens.env as KB_TOKEN_<PROJECT>, and the flush
#       hook — which already knows the project, having slugified the cwd —
#       selects the matching one.
#
#       Three properties matter, and each is a way this could silently do the
#       wrong thing:
#         1. The RIGHT token must go with the RIGHT project. Sending project A's
#            session under project B's credential is a cross-project write that
#            the server would either accept (wrong data, wrong attribution) or
#            reject with an opaque 403.
#         2. A machine with no token for THIS project is not broken in general —
#            it just has not been given access to this one. It must say exactly
#            that, and exactly what to run, instead of failing later.
#         3. A single-token machine must keep working unchanged: KB_TOKEN stays
#            the fallback, so adding this feature cannot break an existing box.
. /rig/lib.sh

new_home projtokens
stub_reset
cfg_rest
install_hooks >/dev/null

TOKENS="$HOME/.claude/kb-tokens.env"
# alpha uses the token the stub ACCEPTS, so its session actually lands;
# beta uses a different one, which the stub records even while rejecting it —
# selection is the property under test, acceptance is the server's business.
ALPHA="$RIG_TOKEN"
BETA="kbg_beta00000000000000000000000000000000000"
cat >"$TOKENS" <<EOF
# one narrow credential per project
KB_TOKEN_ALPHA=$ALPHA
KB_TOKEN_BETA=$BETA
EOF
chmod 600 "$TOKENS"

step "the env file is owner-only (it holds credentials)"
assert_eq "600" "$(stat -c%a "$TOKENS")" "kb-tokens.env is chmod 600"

# ------------------------------------------- 1. the right token per project
step "a session in project 'alpha' is sent with ALPHA's token"
WA="$HOME/work/alpha"
mkdir -p "$WA"
mk_transcript "$WA/t.jsonl" 40 alpha
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WA/t.jsonl" "$WA" alpha-1)"
assert_eq "0" "$RC" "the hook exits 0"
wait_stub 1 60 || fail "nothing was submitted for alpha"
assert_eq "alpha" "$(stub_field '.transport=="rest"' '.project' | tail -1)" "submitted under alpha"
assert_eq "$ALPHA" "$(stub_field '.' '.bearer // empty' | tail -1)" \
  "alpha's session carried ALPHA's token"

step "a session in project 'beta' is sent with BETA's token"
WB="$HOME/work/beta"
mkdir -p "$WB"
mk_transcript "$WB/t.jsonl" 40 beta
BEFORE=$(stub_total)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WB/t.jsonl" "$WB" beta-1)"
wait_stub "$((BEFORE + 1))" 60 || fail "nothing reached the stub for beta"
assert_eq "$BETA" "$(stub_field '.' '.bearer // empty' | tail -1)" \
  "beta's session carried BETA's token, not alpha's"

# ---------------------------------- 2. a project with no token says so, loudly
step "a project with NO token is announced, with the exact command to fix it"
# The shared KB_TOKEN must be absent, or it would (correctly) be used instead.
grep -v '^KB_TOKEN=' "$CFG" >"$CFG.t" && mv "$CFG.t" "$CFG"
WG="$HOME/work/gamma"
mkdir -p "$WG"
mk_transcript "$WG/t.jsonl" 40 gamma
BEFORE=$(stub_total)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WG/t.jsonl" "$WG" gamma-1)"
assert_eq "0" "$RC" "the hook still exits 0 (never blocks Claude Code)"
settle
assert_eq "$BEFORE" "$(stub_total)" "nothing was submitted without a credential"
assert_not_contains "$(cat "$LOGF")" "gamma NOT CONFIGURED" \
  "the generic message did not bury the actionable one"
if grep -q "NEEDS TOKEN" "$LOGF"; then
  LINE=$(grep -m1 "NEEDS TOKEN" "$LOGF")
  pass "the missing token is announced"
  note "$(printf '%s' "$LINE" | head -c 200)"
  assert_contains "$LINE" "KB_TOKEN_GAMMA" "it names the exact variable to add"
  assert_contains "$LINE" "kb_project_token" "it names the tool that issues one"
  assert_contains "$LINE" "gamma" "it names the project"
  assert_contains "$LINE" "NOT captured" "it is explicit that nothing was captured"
else
  fail "a project with no token failed SILENTLY: $(tail -3 "$LOGF" | tr '\n' '|')"
fi

step "the warning is rate-limited, not once per firing"
BEFORE_N=$(grep -c "NEEDS TOKEN" "$LOGF")
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WG/t.jsonl" "$WG" gamma-2)"
settle
assert_eq "$BEFORE_N" "$(grep -c 'NEEDS TOKEN' "$LOGF")" "the warning did not repeat within the window"

step "and the next session surfaces it to the user"
fire "$HOOKS/kb-session-brief.sh" "$(start_event startup "$WG")"
assert_eq "0" "$RC" "the brief hook exits 0"
if printf '%s' "$OUT" | grep -q "NEEDS TOKEN"; then
  pass "the pre-brief lifts the warning into the session"
else
  fail "the pre-brief did not surface it"
  note "log tail: $(tail -3 "$LOGF" | cut -c1-100 | tr '\n' '|')"
fi

# ------------------------------------- 3. a single-token machine still works
step "a machine with only the shared KB_TOKEN is unaffected"
new_home sharedtoken
stub_reset
cfg_rest
install_hooks >/dev/null
WS="$HOME/work/shared"
mkdir -p "$WS"
mk_transcript "$WS/t.jsonl" 40 shared
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WS/t.jsonl" "$WS" shared-1)"
wait_stub 1 60 || fail "the single-token machine stopped working"
assert_eq "$RIG_TOKEN" "$(stub_field '.' '.bearer // empty' | tail -1)" \
  "it used the shared KB_TOKEN"
assert_not_contains "$(cat "$LOGF")" "NEEDS TOKEN" "no spurious warning without kb-tokens.env"

finish

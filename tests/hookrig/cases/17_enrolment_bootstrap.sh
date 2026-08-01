#!/usr/bin/env bash
# CASE: a CLEAN machine sets itself up from a one-time enrolment code
# PINS: the whole point of enrolment — that setup is one block, that no bearer is
#       ever pasted by hand, and that the code stops being a credential once
#       used. Everything here runs against a HOME with nothing in it: no hooks,
#       no config, no scripts. That is the state a new machine is actually in,
#       and the state every previous case skipped by calling install.sh from a
#       skill directory that was already present.
#
#       The bootstrap is exactly what kb_setup prints:
#         1. fetch the four scripts, authenticating with the CODE
#         2. run install.sh with KB_ENROL_CODE set
#         3. install.sh redeems the code for this machine's own credential,
#            writes ~/.claude/kb-flush.env at mode 600, registers the hooks
#
#       Two properties matter beyond "it works":
#         * fetching must NOT spend the code (a GET is not a redemption), or the
#           bootstrap would consume its own credential before using it;
#         * the credential must land ONLY in the config file, never on a command
#           line or in the log, where `ps` or a shared logfile would expose it.
. /rig/lib.sh

new_home bootstrap
stub_reset

# A genuinely empty starting point — assert it, so this case cannot silently
# degrade into "install.sh was already there".
step "the machine starts with nothing"
assert_file_missing "$CFG" "no kb-flush.env"
assert_file_missing "$SETTINGS" "no settings.json"
assert_file_missing "$HOOKS/flush-session-to-kb.sh" "no hooks installed"
mkdir -p "$HOME/.claude/kb-setup"
assert_eq "0" "$(ls -1 "$HOME/.claude/kb-setup" | wc -l | tr -d ' ')" "the setup dir is empty"

CODE="kbe_$(printf 'b%.0s' $(seq 1 28))"
note "enrolment code: ${CODE:0:8}… (single use)"

# ------------------------------------------------- 1. fetch, authenticating with the code
step "the four scripts download using the CODE as bearer"
cd "$HOME/.claude/kb-setup" || exit 1
for f in install.sh flush-session-to-kb.sh kb-session-brief.sh \
         kb-mcp-call.sh notify-complete.sh turn-start.sh; do
  if "$RIG_CURL" -fsS -H "Authorization: Bearer $CODE" "$STUB/api/setup/$f" -o "$f"; then
    pass "fetched $f ($(wc -c <"$f") bytes)"
  else
    fail "could not fetch $f"
  fi
done

step "what arrived is the real script, byte for byte"
for f in install.sh flush-session-to-kb.sh kb-session-brief.sh \
         kb-mcp-call.sh notify-complete.sh turn-start.sh; do
  if cmp -s "$f" "$SKILL/$f"; then
    pass "$f matches the shipped copy"
  else
    fail "$f differs from the shipped copy"
  fi
done

step "an unauthenticated fetch is refused"
if "$RIG_CURL" -fsS "$STUB/api/setup/install.sh" -o /dev/null 2>/dev/null; then
  fail "the scripts are served without any credential"
else
  pass "no credential, no scripts"
fi

# ---------------------------------------------------- 2. install, redeeming the code
step "install.sh redeems the code and configures the machine"
OUT=$(cd "$HOME/.claude/kb-setup" && KB_ENROL_CODE="$CODE" KB_REST_URL="$STUB" bash install.sh 2>&1)
RC=$?
assert_eq "0" "$RC" "install.sh exits 0"
printf '%s\n' "$OUT" | grep -Ei "enrol:|mode:|config:" | while read -r l; do note "$l"; done
assert_contains "$OUT" "enrol:" "the installer reports the enrolment"
assert_contains "$OUT" "redeemed" "the code was redeemed"

step "the credential landed in the config, at mode 600"
assert_file_contains "$CFG" "KB_REST_URL=$STUB" "the REST base was written"
assert_file_contains "$CFG" "KB_TOKEN=$RIG_TOKEN" "this machine's credential was written"
MODE=$(stat -c%a "$CFG" 2>/dev/null || stat -f%Lp "$CFG")
assert_eq "600" "$MODE" "the config is owner-only"

step "the credential never appears anywhere else"
# A token on a command line is visible to `ps`; a token in the shared hook log is
# visible to anything that reads it (and the pre-brief lifts log lines into the
# next session's context).
assert_not_contains "$OUT" "$RIG_TOKEN" "install.sh did not echo the credential"
[ -f "$LOGF" ] && assert_not_contains "$(cat "$LOGF")" "$RIG_TOKEN" "not in the hook log" \
  || pass "no hook log yet"

step "the hooks are registered for all four events"
for ev in PreCompact SessionEnd SessionStart UserPromptSubmit; do
  N=$("$RIG_JQ" -r --arg e "$ev" '[.hooks[$e][]?.hooks[]?] | length' "$SETTINGS")
  # UserPromptSubmit carries TWO after the notification hooks shipped: the KB
  # per-prompt recall AND turn-start.sh, which stamps the turn so the Stop notice
  # can report how long the run took.
  EXPECT=1; [ "$ev" = "UserPromptSubmit" ] && EXPECT=2
  assert_eq "$EXPECT" "$N" "$ev registered the expected number of times"
done

# ------------------------------------------------------ 3. the code is now spent
step "the code cannot be redeemed a second time"
CODE2_OUT=$("$RIG_CURL" -sS -X POST "$STUB/api/enrol" -H "Authorization: Bearer $CODE" \
  -H "Content-Type: application/json" -d '{}' 2>&1)
assert_not_contains "$CODE2_OUT" "$RIG_TOKEN" "a spent code no longer yields a credential"
note "second redemption: $(printf '%s' "$CODE2_OUT" | head -c 90)"

step "a second machine using the same code is left UNconfigured, and says so"
new_home bootstrap2
mkdir -p "$HOME/.claude/kb-setup"
cp "$SKILL"/*.sh "$HOME/.claude/kb-setup/"
OUT2=$(cd "$HOME/.claude/kb-setup" && KB_ENROL_CODE="$CODE" KB_REST_URL="$STUB" bash install.sh 2>&1)
RC2=$?
# An INSTALLER that could not configure the machine must fail loudly. It used to
# exit 0 and write a placeholder config (api.example.com, empty token) that read
# as a finished install and captured nothing forever (KB 1198).
assert_ne "0" "$RC2" "install.sh reports failure when enrolment is refused"
assert_contains "$OUT2" "refused this code" "the refusal names the server's own reason"
assert_contains "$OUT2" "NOT configuring" "it says the machine was left unconfigured"
assert_not_contains "$(cat "$CFG" 2>/dev/null || echo)" "api.example.com" \
  "no placeholder config was written to look like success"
if grep -q "KB_TOKEN=$RIG_TOKEN" "$CFG" 2>/dev/null; then
  fail "a spent code still configured the machine"
else
  pass "no credential was written from a spent code"
fi

# ------------------------------------------------- 4. the enrolled machine works
step "the enrolled machine can actually capture a session"
export HOME="$RIG_HOMEBASE/bootstrap"
export CFG="$HOME/.claude/kb-flush.env" HOOKS="$HOME/.claude/hooks" LOGF="$HOME/.claude/hooks/flush-session.log"
W="$HOME/work/proj"
mkdir -p "$W"
mk_transcript "$W/t.jsonl" 60 enrolled
BEFORE=$(stub_total)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$W/t.jsonl" "$W" enrolled-1)"
assert_eq "0" "$RC" "the flush hook exits 0"
wait_stub "$((BEFORE + 1))" 60 || fail "the enrolled machine submitted nothing"
assert_contains "$(stub_field '.transport=="rest"' '.project')" "proj" "it submitted under the right project"
assert_file_contains "$LOGF" "ok bytes=" "and logged a successful capture"

# --------------------------- 5. the DEFAULT enrolment shape: a project-scoped box
# Everything above enrols to a credential with no project, which install.sh writes
# to kb-flush.env as a shared KB_TOKEN. That is NOT the default path. A per-project
# enrolment code redeems to a project-scoped credential that lands in
# kb-tokens.env, and NO shared KB_TOKEN is written anywhere -- so on those boxes
# (which is most of them) every bare "$KB_TOKEN" reference is an UNBOUND VARIABLE
# under `set -u`. That killed the stale-hook check inside its own command
# substitution: silently, with the warning it exists to print never appearing.
# The same class as KB item 1193, in a second place. This section reproduces the
# shape and asserts the warning fires.
step "a per-project enrolment writes a SCOPED token and no shared KB_TOKEN"
new_home enrolproj
stub_reset
stub_enrol_project proj
mkdir -p "$HOME/.claude/kb-setup"
cp "$SKILL"/*.sh "$HOME/.claude/kb-setup/"
CODE4="kbe_$(printf 'd%.0s' $(seq 1 28))"
OUT4=$(cd "$HOME/.claude/kb-setup" && KB_ENROL_CODE="$CODE4" KB_REST_URL="$STUB" bash install.sh 2>&1)
assert_eq "0" "$?" "install.sh exits 0"
TOKENS_ENV="$HOME/.claude/kb-tokens.env"
assert_file_contains "$TOKENS_ENV" "KB_TOKEN_PROJ=" "the scoped credential landed in kb-tokens.env"
if grep -q '^KB_TOKEN=' "$CFG" 2>/dev/null; then
  fail "a shared KB_TOKEN was written -- this is no longer the default-enrolment shape"
else
  pass "no shared KB_TOKEN anywhere (the shape that made \$KB_TOKEN unbound)"
fi

step "install.sh stamped the version, so it fetched with the REDEEMED credential"
# The enrolment code is SPENT by the POST above (single use), so the old
# ${KB_TOKEN:-$KB_ENROL_CODE} fallback authenticated with a dead credential and
# left every enrolled box unversioned. A stamp here means the run carried the
# token it had just redeemed.
SERVER_V=$("$RIG_CURL" -fsS -H "Authorization: Bearer $RIG_TOKEN" \
  "$STUB/api/setup/manifest.json" | "$RIG_JQ" -r .version)
assert_ne "" "$SERVER_V" "the stub publishes a version"
assert_eq "$SERVER_V" "$(cat "$HOOKS/.setup-version" 2>/dev/null)" "the installed version was recorded"

step "and a STALE default-enrolled box is told its hooks are out of date"
printf '%s\n' "deadbeefdead" >"$HOOKS/.setup-version"   # pretend it installed an older set
WP="$HOME/work/proj"
mkdir -p "$WP"
fire "$HOOKS/kb-session-brief.sh" "$(start_event startup "$WP")"
assert_eq "0" "$RC" "the brief hook exits 0"
if printf '%s' "$OUT" | grep -q "OUT OF DATE"; then
  pass "the stale box was warned"
  note "$(printf '%s' "$OUT" | grep -m1 'OUT OF DATE' | head -c 160)"
  assert_contains "$OUT" "$SERVER_V" "it names the version the server has"
else
  fail "a stale DEFAULT-ENROLLED box was NOT warned -- the version check ran with no bearer (or died on an unbound one): $(printf '%s' "$OUT" | head -c 200)"
fi

step "the check authenticated -- an unauthenticated manifest fetch is refused"
# The stub answers 401 when no bearer arrives, so the warning above can only have
# come from a request that carried the project-scoped token.
assert_eq "0" "$(stub_count '.path=="/api/setup/manifest.json" and .status==401')" \
  "no unauthenticated manifest fetch happened"

finish

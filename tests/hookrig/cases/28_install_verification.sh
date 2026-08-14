#!/usr/bin/env bash
# CASE: THE STAMP IS EARNED, NOT ASSUMED (installer verification)
# PINS: install.sh recomputes the sha256 of every hook it installed and compares
#       against the manifest's per-file hashes before writing .setup-version
#       (review #3 P1, KB 19663). A clean install stamps; ANY corrupted byte
#       that survives the copy leaves the box UNVERSIONED with a loud per-file
#       MISMATCH line, and strips a stale stamp from an earlier run — a stamp
#       sitting over corrupted files silences the staleness banner, which is the
#       exact trust gap the verification closes.
. /rig/lib.sh

new_home verifyinst
stub_reset
mkdir -p "$HOME/.claude"
cfg_rest # an already-configured box: install.sh must keep it, verify, stamp

SERVER_V=$("$RIG_CURL" -fsS -H "Authorization: Bearer $RIG_TOKEN" \
  "$STUB/api/setup/manifest.json" | "$RIG_JQ" -r .version)
assert_ne "" "$SERVER_V" "the stub publishes a manifest version"

step "a clean install verifies every installed hook and stamps"
OUT1=$(KB_REST_URL="$STUB" KB_TOKEN="$RIG_TOKEN" bash "$SKILL/install.sh" 2>&1)
RC1=$?
assert_eq "0" "$RC1" "install.sh exits 0"
assert_eq "$SERVER_V" "$(cat "$HOOKS/.setup-version" 2>/dev/null)" "the verified version was stamped"
assert_contains "$OUT1" "verified" "the stamp line says the files were verified"
assert_not_contains "$OUT1" "MISMATCH" "no mismatch on a clean install"

step "one corrupted byte post-copy leaves the box UNVERSIONED, loudly"
# The wrapper corrupts the INSTALLED copy after install(1) has written it — the
# exact 'survives to the stamp' shape from the review finding: source intact,
# destination mangled (a partial write, or Git Bash autocrlf rewriting bytes).
cat >"$HOME/bin/install" <<'EOF'
#!/usr/bin/env bash
/usr/bin/install "$@"
rc=$?
last=""
for a in "$@"; do last="$a"; done
case "$last" in */kb-session-brief.sh) printf '\r\n' >>"$last" ;; esac
exit $rc
EOF
chmod +x "$HOME/bin/install"

OUT2=$(KB_REST_URL="$STUB" KB_TOKEN="$RIG_TOKEN" bash "$SKILL/install.sh" 2>&1)
RC2=$?
assert_eq "0" "$RC2" "the install itself still succeeds (verification gates only the stamp)"
assert_contains "$OUT2" "MISMATCH kb-session-brief.sh" "the loud line names the corrupted file"
assert_contains "$OUT2" "expected" "...with expected vs got"
assert_contains "$OUT2" "UNVERSIONED" "...and says the box stays unversioned"
assert_contains "$OUT2" "CRLF" "...and hints at the Git Bash CRLF trap"
assert_file_missing "$HOOKS/.setup-version" "the stale stamp from the clean install was stripped"

EXPECT12=$("$RIG_CURL" -fsS -H "Authorization: Bearer $RIG_TOKEN" \
  "$STUB/api/setup/manifest.json" | "$RIG_JQ" -r '.files["kb-session-brief.sh"]' | cut -c1-12)
assert_contains "$OUT2" "$EXPECT12" "the loud line carries the manifest's 12-char hash prefix"
MLINE=$(printf '%s\n' "$OUT2" | grep -m1 "MISMATCH kb-session-brief.sh")
assert_not_contains "$MLINE" "$HOOKS" "the per-file line names the FILE, not the full path"

step "removing the corruption and re-running heals the box"
rm -f "$HOME/bin/install"
OUT3=$(KB_REST_URL="$STUB" KB_TOKEN="$RIG_TOKEN" bash "$SKILL/install.sh" 2>&1)
RC3=$?
assert_eq "0" "$RC3" "install.sh exits 0"
assert_eq "$SERVER_V" "$(cat "$HOOKS/.setup-version" 2>/dev/null)" "a clean re-install stamps again"
assert_file_contains "$CFG" "KB_REST_URL=$STUB" "the existing transport config was kept throughout"

step "no manifest reachable -> verification skipped with a note, no stamp"
new_home verifynomanifest
mkdir -p "$HOME/.claude"
cfg_rest
# KB_REST_URL deliberately points at a dead port: the fetch fails, so there is
# nothing to verify against — the install must still succeed, say so in ONE
# line, and stamp nothing (the enrolment-less LOCAL shape).
OUT4=$(KB_REST_URL="http://127.0.0.1:9" KB_TOKEN="$RIG_TOKEN" bash "$SKILL/install.sh" 2>&1)
RC4=$?
assert_eq "0" "$RC4" "install.sh exits 0 without a manifest"
assert_contains "$OUT4" "no manifest available" "the skipped verification is said out loud"
assert_file_missing "$HOOKS/.setup-version" "no stamp without a manifest (unversioned is honest)"

finish

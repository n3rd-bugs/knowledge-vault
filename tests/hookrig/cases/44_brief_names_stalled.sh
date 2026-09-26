#!/usr/bin/env bash
# CASE: the SessionStart `[KB sweep]` line NAMES the stalled sessions and, for a
#       project with no token, the remedy (wave C.1, KB 40411 fix set item 8).
# PINS: kb-session-brief.sh used to print a bare count -- `N transcript(s)
#       STALLED after repeated failures -- see ~/.claude/hooks/flush-session.log`
#       -- so the owner had to dig through the log for which session, which
#       project and why (usama-home, KB 40411 defect 3). Now, on the SAME single
#       ASCII line, per state/<okey>.sweep at or past the stall threshold
#       (KB_SWEEP_STALL_RUNS, 6):
#         * `<slug> (<sid8>) <verdict>` -- slug from the session pin
#           state/<okey>.project, verdict from the .sweep record;
#         * plus `kb_project_token(project="<slug>", machine=...)` ONLY when the
#           hook left state/.notoken-<slug>.stamp -- the token's NAME inputs,
#           never a token VALUE (a kbg_ bait sits in the kb-tokens.env the
#           brief sources, and must not appear);
#         * a session below the threshold is not named.
#       The line stays ONE line and pure ASCII (KB 12885).
# RED on today's hooks (HEAD b2b37a3) -- expected FAIL lines:
#       "names schematics (5ca1ab1e) refused -- [schematics (5ca1ab1e) refused]
#       not found in [[KB sweep] 2 transcript(s) STALLED after repeated failures ...]",
#       "...with the kb_project_token remedy for schematics -- ... not found",
#       "names kiwrap (b0a710ad) failed -- ... not found".
#       The line count, the count text, ASCII, the not-named / no-remedy /
#       no-token-value negatives and the brief-body control pass on both.
. /rig/lib.sh

new_home stallnamed
stub_reset
install_hooks >/dev/null
export KB_VERSION_CHECK=0 KB_RECALL_BREAKER_SECS=0
# Per-project tokens only (no shared KB_TOKEN): `proj` (the cwd) has a working
# one so the brief really runs and searches; the bait is a real-shaped kbg_
# value for another project, in the SAME file the brief sources.
printf '# REMOTE\nKB_REST_URL=%s\n' "$STUB" | write_cfg
BAIT="kbg_0000000000000000000000000000000000000000"
printf 'KB_TOKEN_PROJ=%s\nKB_TOKEN_OTHERPROJ=%s\n' "$RIG_TOKEN" "$BAIT" >"$HOME/.claude/kb-tokens.env"
chmod 600 "$HOME/.claude/kb-tokens.env"
ST="$HOOKS/state"
mkdir -p "$ST"
WORK="$HOME/work/proj"
mkdir -p "$WORK"
NOW=$(date +%s)
ascii_ok() { if printf '%s' "$1" | LC_ALL=C grep -q '[^ -~]'; then fail "$2 -- non-ASCII byte: $1"; else pass "$2 (ASCII)"; fi; }

# The last sweep: healthy cadence, two sessions stalled -- the line exists today.
printf 'ts=%s rc=0 mode=auto ready=3 pushed=0 failed=3 deferred=0 stalled=3 unknown=0 live=0 unflushed=0 busy=0 partial=0\n' \
  "$((NOW - 600))" >"$ST/.sweep-last"
# A: stalled, refused, and the hook's NEEDS TOKEN stamp is fresh (usama-home's 64de5f29).
A="5ca1ab1e-0000-4000-8000-00000000000a"
printf 'schematics\tfiring\n' >"$ST/$A.project"
printf 'fails=6 last=%s verdict=refused\n' "$NOW" >"$ST/$A.sweep"
printf '%s\n' "$NOW" >"$ST/.notoken-schematics.stamp"
# B: stalled for a transport reason, and its project HAS a token (no stamp).
B="b0a710ad-0000-4000-8000-00000000000b"
printf 'kiwrap\tgitfirst\n' >"$ST/$B.project"
printf 'fails=9 last=%s verdict=failed\n' "$NOW" >"$ST/$B.sweep"
# D: a TRANSPORT stall (verdict=failed) beside a two-day-old .notoken stamp and
# a token that EXISTS for it -- stamps are never removed, so no remedy may print.
D="d15ab1ed-0000-4000-8000-00000000000d"
printf 'staleproj\tgitfirst\n' >"$ST/$D.project"
printf 'fails=7 last=%s verdict=failed\n' "$NOW" >"$ST/$D.sweep"
printf '%s\n' "$((NOW - 172800))" >"$ST/.notoken-staleproj.stamp"
[ -f "$HOME/.claude/kb-tokens.env" ] && printf 'KB_TOKEN_STALEPROJ=kbg_%s\n' "$(printf '0%.0s' $(seq 40))" >>"$HOME/.claude/kb-tokens.env"
# C: two no-progress runs -- below the threshold, NOT stalled.
C="c1ea4f00-0000-4000-8000-00000000000c"
printf 'quietproj\tfiring\n' >"$ST/$C.project"
printf 'fails=2 last=%s verdict=no-progress\n' "$NOW" >"$ST/$C.sweep"

step "SessionStart: ONE ASCII [KB sweep] line naming each stalled session"
fire "$HOOKS/kb-session-brief.sh" "$(start_event startup "$WORK")"
assert_eq "0" "$RC" "the brief exits 0"
assert_eq "1" "$(printf '%s\n' "$OUT" | grep -c '^\[KB sweep\]')" "exactly ONE [KB sweep] line"
SWL=$(printf '%s\n' "$OUT" | grep '^\[KB sweep\]' | head -n1)
note "the line: $SWL"
assert_contains "$SWL" "3 transcript(s) STALLED" "the stalled count is still on it"
assert_contains "$SWL" "schematics (${A:0:8}) refused" "names schematics (${A:0:8}) refused"
assert_contains "$SWL" 'kb_project_token(project="schematics", machine=' "...with the kb_project_token remedy for schematics"
assert_contains "$SWL" "kiwrap (${B:0:8}) failed" "names kiwrap (${B:0:8}) failed"
assert_not_contains "$SWL" 'kb_project_token(project="kiwrap"' "no remedy for kiwrap (no .notoken-kiwrap.stamp)"
assert_contains "$SWL" "staleproj (${D:0:8}) failed" "names staleproj (${D:0:8}) failed"
assert_not_contains "$SWL" 'kb_project_token(project="staleproj"' "no remedy for a transport stall beside a STALE stamp (verdict gate)"
assert_not_contains "$SWL" "${C:0:8}" "a session below the stall threshold is not named"
assert_not_contains "$SWL" "quietproj" "...nor its project"
ascii_ok "$SWL" "[KB sweep] line"

step "never a token VALUE"
assert_not_contains "$OUT" "kbg_" "no kbg_ value anywhere in the brief"
assert_not_contains "$OUT" "$RIG_TOKEN" "not the brief's own bearer either"

step "CONTROL: the brief read that kb-tokens.env (it searched with KB_TOKEN_PROJ)"
assert_contains "$OUT" "[KB pre-brief · project proj]" "the pre-brief body printed"
assert_ge "$(stub_count '.path=="/api/search" and .auth=="ok"')" "1" "the search used the token from the same file as the bait"

finish

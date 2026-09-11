#!/usr/bin/env bash
# CASE: what notify-complete.sh and the pre-brief say about the NEW state files
# PINS: wave C notify/brief readers of the new state files -- synced, retry#N, unseeded, sweep health, sid-aware scanner (proposal_v2 C9 / hybrid_v1 s2.6, KB 33784)
#   notify-complete.sh keeps the `kb <flushed>/<size>` segment (its consumers
#   and tests parse it) and reads the per-session state files the never-delete
#   hook now leaves behind:
#     * offset == size   -> `kb 5.2MB synced` (checkpoints are never deleted,
#                           so "drained" is now a visible fact, not a missing file)
#     * <okey>.retry     -> ` retry#N` (N = the marker's 2nd field)
#     * <okey>.unseeded  -> `kb ?/<size> unseeded` (no offset: the hook refused
#                           a from-0 send for a pre-stamp transcript)
#     * sweep health     -> ` sweep:never` / ` sweep:<age> stale` /
#                           ` sweep:STALLED n` / ` sweep:unknown n`, appended
#                           ONLY when unhealthy; a healthy box stays silent.
#   kb-session-brief.sh at SessionStart:
#     * one `[KB sweep]` line, only when actionable: `stalled` when .sweep-last
#       is older than 6 h AND .sweep.trigger is older than the last sweep
#       (nothing is triggering it), `never ran` when a timer/trigger exists but
#       no sweep completed; silent when healthy.
#     * the flush-failure scanner is SID-AWARE: a failure carrying `sid=X` is
#       cleared only by a later fully-drained ok line with the SAME sid; lines
#       without a sid fall back to the project token; the old "any later ok
#       clears anything" rule survives only for lines with neither.
#   Every published body stays pure ASCII (KB 12885).
RIG_CURL="$(command -v curl)" # the recorder below shadows `curl` on PATH
. /rig/lib.sh

new_home notifystate
stub_reset
cfg_rest
install_hooks >/dev/null
export KB_VERSION_CHECK=0 KB_RECALL_BREAKER_SECS=0

# A curl that RECORDS the ntfy body and never touches the network. Installed
# AFTER install_hooks so the installer's own manifest fetch was real.
cat >"$HOME/bin/curl" <<'EOF'
#!/usr/bin/env bash
prev=""
for a in "$@"; do [ "$prev" = "-d" ] && printf '%s\n' "$a" >>"$HOME/pushed"; prev="$a"; done
cat >/dev/null
exit 0
EOF
chmod +x "$HOME/bin/curl"
printf 'NTFY_URL=http://127.0.0.1:9\nNTFY_TOPIC=rig-topic\nNTFY_MACHINE=rigbox\n' >"$HOME/.claude/notify.env"

WORK="$HOME/work/proj"
mkdir -p "$WORK"
T="$WORK/t.jsonl"
mk_transcript "$T" 400 state
SIZE=$(wc -c <"$T")
# The same rounding kb_human uses (%.0f at KB, %.1f at MB), so the expected
# strings below are the hook's own formatting and not this case's idea of it.
HSIZE=$(awk -v s="$SIZE" 'BEGIN { if (s >= 1048576) printf "%.1fMB", s/1048576; else printf "%.0fKB", s/1024 }')
ST="$HOOKS/state"
NOTIFY="$HOOKS/notify-complete.sh"
BRIEF="$HOOKS/kb-session-brief.sh"

stop_event() { # sid
  "$RIG_JQ" -nc --arg t "$T" --arg c "$WORK" --arg s "$1" '{transcript_path:$t,cwd:$c,session_id:$s,hook_event_name:"Stop"}'
}
ping() { # sid -> the recorded body in $BODY
  rm -f "$HOME/pushed"
  fire "$NOTIFY" "$(stop_event "$1")"
  BODY=$(cat "$HOME/pushed" 2>/dev/null)
}
ascii_ok() { if printf '%s' "$1" | LC_ALL=C grep -q '[^ -~]'; then fail "$2 -- non-ASCII byte in the body: $1"; else pass "$2 (ASCII)"; fi; }

step "notify: the classic progress shape is unchanged"
printf '%s' "$((SIZE / 2))" >"$ST/s-part.offset"
ping s-part
assert_eq "0" "$RC" "notify exits 0"
case "$BODY" in *"(kb "*"/$HSIZE)"*) pass "kb <flushed>/<size> shape kept: $BODY" ;; *) fail "progress shape changed: $BODY" ;; esac
assert_not_contains "$BODY" "sweep:" "no sweep state -> no sweep suffix"

step "notify: a drained session reads synced (the offset file is not deleted any more)"
printf '%s' "$SIZE" >"$ST/s-done.offset"
ping s-done
assert_contains "$BODY" "(kb $HSIZE synced)" "offset == size -> synced: $BODY"

step "notify: the retry marker adds retry#N"
printf '%s' "$((SIZE / 2))" >"$ST/s-retry.offset"
printf '1757500000 3 502 %s\n' "$SIZE" >"$ST/s-retry.retry"
ping s-retry
assert_contains "$BODY" " retry#3)" "retry#N from the marker's 2nd field: $BODY"
ascii_ok "$BODY" "retry body"
printf '1757500000 0 lock\n' >"$ST/s-retry.retry"
ping s-retry
assert_contains "$BODY" " retry)" "a 0 count (lock-deferred, nothing failed) reads as a bare retry, never retry#0: $BODY"

step "notify: an unseeded refusal reads ?/<size> unseeded"
touch "$ST/s-unseeded.unseeded"
ping s-unseeded
assert_contains "$BODY" "(kb ?/$HSIZE unseeded)" "no offset + .unseeded -> unseeded: $BODY"

# PIN (wave C stage 3 A1/D, KB 33783): a CORRUPT checkpoint is refused by the
# hook as unseeded (never restarted from 0) and leaves <okey>.unseeded beside
# it; notify says exactly what it says for a missing checkpoint. Without the
# marker (the hook has not ruled yet) the segment is omitted, never "0/...".
step "notify: a CORRUPT offset beside an .unseeded marker reads ?/<size> unseeded; alone it is omitted"
printf 'not-a-number\n' >"$ST/s-corrupt.offset"
ping s-corrupt
assert_not_contains "$BODY" "(kb " "corrupt offset, no ruling yet -> no kb segment: $BODY"
touch "$ST/s-corrupt.unseeded"
ping s-corrupt
assert_contains "$BODY" "(kb ?/$HSIZE unseeded)" "corrupt offset + .unseeded -> unseeded: $BODY"

# PIN (wave C stage 3 D, proposal_v2 Appendix I): the marker probe reads the ONE
# spelling both hooks write -- <okey>.retry / <okey>.unseeded -- and no longer
# the retired <okey>.offset.retry guess.
step "notify: the marker probe reads <okey>.retry only, never <okey>.offset.retry"
printf '%s' "$((SIZE / 2))" >"$ST/s-spell.offset"
printf '1757500000 2 502 %s\n' "$SIZE" >"$ST/s-spell.offset.retry"
ping s-spell
assert_not_contains "$BODY" "retry" "the retired <okey>.offset.retry spelling is NOT read: $BODY"
mv "$ST/s-spell.offset.retry" "$ST/s-spell.retry"
ping s-spell
assert_contains "$BODY" " retry#2)" "<okey>.retry is: $BODY"

step "notify: sweep suffix only when unhealthy"
NOW=$(date +%s)
printf 'ts=%s rc=0 ready=0 pushed=0 failed=0 deferred=0 stalled=0 unknown=0 live=1 unflushed=0\n' "$((NOW - 600))" >"$ST/.sweep-last"
ping s-part
assert_not_contains "$BODY" "sweep:" "a 10-minute-old healthy sweep is silent"
printf 'ts=%s rc=0 ready=0 pushed=0 failed=0 deferred=0 stalled=0 unknown=0 live=1 unflushed=0\n' "$((NOW - 93600))" >"$ST/.sweep-last"
ping s-part
assert_contains "$BODY" " sweep:26h stale)" "older than 3 x KB_SWEEP_INTERVAL -> stale: $BODY"
printf 'ts=%s rc=1 ready=0 pushed=0 failed=0 deferred=0 stalled=1 unknown=6 live=1 unflushed=0\n' "$((NOW - 600))" >"$ST/.sweep-last"
ping s-part
assert_contains "$BODY" " sweep:STALLED 1)" "stalled transcripts are named: $BODY"
printf 'ts=%s rc=0 ready=0 pushed=0 failed=0 deferred=0 stalled=0 unknown=6 live=1 unflushed=0\n' "$((NOW - 600))" >"$ST/.sweep-last"
ping s-part
assert_contains "$BODY" " sweep:unknown 6)" "unseeded transcripts awaiting --seed are counted: $BODY"
printf 'ts=%s rc=0 ready=0 pushed=0 failed=0 deferred=0 stalled=0 unknown=0 live=1 unflushed=0\n' "$((NOW - 93600))" >"$ST/.sweep-last"
KB_SWEEP_INTERVAL=0 ping s-part
assert_not_contains "$BODY" "sweep:" "KB_SWEEP_INTERVAL=0 is the OFF switch: no cadence, so nothing is stale"
rm -f "$ST/.sweep-last"
touch -d '@'"$((NOW - 86400))" "$ST/.sweep.trigger"
ping s-part
assert_not_contains "$BODY" "sweep:" "no timer unit: an old trigger alone is not 'never' (piggyback-only boxes stay silent)"
mkdir -p "$HOME/.config/systemd/user"
touch "$HOME/.config/systemd/user/kb-flush-sweep.timer"
ping s-part
assert_contains "$BODY" " sweep:never)" "a rendered timer + a trigger older than 3 x the interval and no completed sweep: $BODY"
ascii_ok "$BODY" "sweep body"
rm -f "$ST/.sweep.trigger"
ping s-part
assert_contains "$BODY" " sweep:never)" "a rendered timer + no trigger at all (ExecStartPre never ran): $BODY"
touch "$ST/.sweep.trigger"
ping s-part
assert_not_contains "$BODY" "sweep:" "a rendered timer + a FRESH trigger: a sweep was asked for and may be running -> silent"
rm -f "$ST/.sweep.trigger"
KB_SWEEP_INTERVAL=0 ping s-part
assert_not_contains "$BODY" "sweep:" "KB_SWEEP_INTERVAL=0: never 'never' either"
rm -rf "$HOME/.config/systemd"

step "brief: [KB sweep] line only when actionable"
LOGF="$HOOKS/flush-session.log"
: >"$LOGF"
fire "$BRIEF" "$(start_event startup "$WORK")"
assert_not_contains "$OUT" "[KB sweep]" "no sweep state -> no line"
printf 'ts=%s rc=0 stalled=0 unknown=0\n' "$((NOW - 600))" >"$ST/.sweep-last"
fire "$BRIEF" "$(start_event startup "$WORK")"
assert_not_contains "$OUT" "[KB sweep]" "healthy 10-minute-old sweep -> silent"
printf 'ts=%s rc=0 stalled=0 unknown=0\n' "$((NOW - 26000))" >"$ST/.sweep-last"
touch -d '@'"$((NOW - 30000))" "$ST/.sweep.trigger"
fire "$BRIEF" "$(start_event startup "$WORK")"
assert_eq "1" "$(printf '%s\n' "$OUT" | grep -c '^\[KB sweep\]')" "exactly one [KB sweep] line"
assert_contains "$OUT" "[KB sweep] last run 7h ago -- stalled" "7 h old and nothing triggered since -> stalled"
touch "$ST/.sweep.trigger"
fire "$BRIEF" "$(start_event startup "$WORK")"
assert_contains "$OUT" "has not completed" "a newer trigger is a different diagnosis (asked for, not finished)"
assert_not_contains "$OUT" "stalled:" "...and is NOT called stalled"
rm -f "$ST/.sweep-last" "$ST/.sweep.trigger"
mkdir -p "$HOME/.config/systemd/user"
touch "$HOME/.config/systemd/user/kb-flush-sweep.timer"
fire "$BRIEF" "$(start_event startup "$WORK")"
assert_contains "$OUT" "[KB sweep] never ran" "timer rendered, no trigger, no completed sweep -> never ran"
touch "$ST/.sweep.trigger"
fire "$BRIEF" "$(start_event startup "$WORK")"
assert_not_contains "$OUT" "[KB sweep]" "timer rendered + a fresh trigger -> a sweep may be running -> silent"
rm -rf "$HOME/.config/systemd"
touch -d '@'"$((NOW - 86400))" "$ST/.sweep.trigger"
fire "$BRIEF" "$(start_event startup "$WORK")"
assert_not_contains "$OUT" "[KB sweep]" "no timer unit: an old trigger alone is not 'never ran'"
printf 'ts=%s rc=1 stalled=2 unknown=6\n' "$((NOW - 600))" >"$ST/.sweep-last"
fire "$BRIEF" "$(start_event startup "$WORK")"
assert_contains "$OUT" "2 transcript(s) STALLED" "stalled count surfaces"
assert_contains "$OUT" "6 unseeded transcript(s) need an owner decision" "unseeded count surfaces with the --seed action"
assert_contains "$OUT" "--seed <sid> <bytes|current>" "...naming the seed procedure"
SWL=$(printf '%s\n' "$OUT" | grep '^\[KB sweep\]')
ascii_ok "$SWL" "[KB sweep] line"
rm -f "$ST/.sweep-last" "$ST/.sweep.trigger"

step "brief: the failure scanner is sid-aware"
warns() { printf '%s' "$OUT" | grep -q 'reported a recent failure'; }
cat >"$LOGF" <<'EOF'
2026-09-11T01:00:00+00:00 flush proj chunk FAILED (502) at offset 0 — will resume ev=Stop sid=aaaaaaaa
2026-09-11T01:01:00+00:00 flush proj ok bytes=100 (offset 100/100) dup=0 ev=Stop sid=bbbbbbbb
EOF
fire "$BRIEF" "$(start_event startup "$WORK")"
if warns; then pass "another session's ok does NOT clear sid=aaaaaaaa's failure"; else fail "sid B's ok masked sid A's failure"; fi
assert_contains "$OUT" "sid=aaaaaaaa" "the surfaced line is A's"
echo '2026-09-11T01:02:00+00:00 flush proj ok bytes=100 (offset 100/100) dup=0 ev=Stop sid=aaaaaaaa' >>"$LOGF"
fire "$BRIEF" "$(start_event startup "$WORK")"
if warns; then fail "A's own later ok did not clear A's failure"; else pass "the same sid's later ok clears it"; fi
cat >"$LOGF" <<'EOF'
2026-09-11T01:00:00+00:00 flush proj chunk FAILED (502) at offset 0 — will resume
2026-09-11T01:01:00+00:00 flush other ok bytes=100 (offset 100/100)
EOF
fire "$BRIEF" "$(start_event startup "$WORK")"
if warns; then pass "no sid: another PROJECT's ok does not clear it (project fallback)"; else fail "a foreign project's ok cleared the failure"; fi
echo '2026-09-11T01:02:00+00:00 flush proj ok bytes=100 (offset 100/100)' >>"$LOGF"
fire "$BRIEF" "$(start_event startup "$WORK")"
if warns; then fail "the same project's later ok did not clear a sid-less failure"; else pass "no sid: the same project's ok clears it"; fi
cat >"$LOGF" <<'EOF'
2026-09-11T01:00:00+00:00 flush proj NOT CONFIGURED: no transport in kb-flush.env
2026-09-11T01:01:00+00:00 flush proj ok bytes=100 (offset 100/100)
EOF
fire "$BRIEF" "$(start_event startup "$WORK")"
if warns; then fail "today's rule regressed: a later ok no longer clears a sid-less failure"; else pass "the pre-wave-C rule still holds for sid-less lines"; fi
# The only line with NEITHER a sid nor a project token is the portable block's
# no-JSON-tool refusal (flush-session-to-kb.sh, `<ts> hook NOT CONFIGURED: ...`):
# it keys `*` and ANY later fully-drained ok clears it.
cat >"$LOGF" <<'EOF'
2026-09-11T01:00:00+00:00 hook NOT CONFIGURED: no JSON tool (jq or python3) on this box -- the KB hooks cannot parse Claude Code events, so this session was NOT captured.
EOF
fire "$BRIEF" "$(start_event startup "$WORK")"
if warns; then pass "a keyless (no sid, no project) failure is surfaced"; else fail "the keyless NOT CONFIGURED line was not surfaced"; fi
echo '2026-09-11T01:01:00+00:00 flush other ok bytes=100 (offset 100/100)' >>"$LOGF"
fire "$BRIEF" "$(start_event startup "$WORK")"
if warns; then fail "a keyless failure was not cleared by a later ok"; else pass "keyless: any later fully-drained ok clears it (today's rule)"; fi
cat >"$LOGF" <<'EOF'
2026-09-11T01:00:00+00:00 flush proj unseeded: refusing to send 5000 from 0 (transcript predates the checkpoint stamp) -- seed it: kb-flush-backfill.sh --seed cccccccc current|5000 ev=Stop sid=cccccccc
2026-09-11T01:01:00+00:00 flush proj ok bytes=100 (offset 100/100) dup=0 ev=Stop sid=dddddddd
EOF
fire "$BRIEF" "$(start_event startup "$WORK")"
if warns; then pass "the wave-C unseeded refusal is surfaced and not cleared by another sid's ok"; else fail "the unseeded refusal did not surface"; fi
assert_contains "$OUT" "unseeded: refusing" "the surfaced line is the refusal"
echo '2026-09-11T01:02:00+00:00 flush proj ok bytes=5000 (offset 5000/5000) dup=0 ev=Stop sid=cccccccc' >>"$LOGF"
fire "$BRIEF" "$(start_event startup "$WORK")"
if warns; then fail "the seeded session's own drained ok did not clear its refusal"; else pass "after --seed, the same sid's drained ok clears the refusal"; fi
cat >"$LOGF" <<'EOF'
2026-09-11T01:00:00+00:00 flush proj partial bytes=50 (offset 50/100) — will resume dup=0 ev=Stop sid=eeeeeeee
2026-09-11T01:01:00+00:00 flush proj ok bytes=100 (offset 100/100) dup=0 ev=Stop sid=ffffffff
EOF
fire "$BRIEF" "$(start_event startup "$WORK")"
if warns; then pass "an incomplete drain (X<Y) for E is not cleared by F's ok"; else fail "F's ok masked E's incomplete drain"; fi

finish

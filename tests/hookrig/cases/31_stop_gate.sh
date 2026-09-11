#!/usr/bin/env bash
# CASE: THE STOP GATE (C5, hybrid_v1 s2.2, proposal_v2 Appendix D, KB 33784) —
#       Stop fires on EVERY turn, so the flush hook decides from two stats and a
#       few cats, BEFORE any pin/git/token/mint work, whether a drain is due.
# PINS (flush-session-to-kb.sh kb_stop_gate + the parent; kb-hook-dispatch.sh):
#   * an idle Stop makes ZERO requests and writes NO log line (and no .project,
#     no .gate: nothing below the gate is computed on a closed one).
#   * an OPEN Stop with a cached pin makes NO git call in the parent (DO-NOT-DO
#     s7): gate -> cached pin -> token file -> detach; the git context, the
#     hook_version stamp and the log rotation are the detached child's work.
#   * a refused (unseeded, pre-stamp) session: the first open Stop refuses once
#     (the child writes .unseeded); every later Stop is CLOSED by that marker
#     (one more stat) until the owner seeds it -- no pin, no lock, no .gate.
#   * size path: (SIZE - OFFSET) >= 4,000,000 AND now - .last >= 300 s opens;
#     a second open within the 300 s interval is blocked; .last set 400 s back
#     re-opens it.
#   * age path: >= 65,536 pending bytes AND now - .last >= 1800 s opens; 1000 s
#     does not; 10 KB pending never does (the floor).
#   * .gate (epoch) is stamped by the PARENT when the gate opens; .last is written
#     by the CHILD when its drain starts (.last >= .gate after a drain).
#   * a KILLED open gate (.gate newer than .last, younger than one interval)
#     keeps the gate CLOSED for that interval; once the interval has elapsed it
#     re-opens.
#   * a retry marker at give-up (n >= 20) SUPPRESSES the age path; below give-up
#     with its back-off elapsed it opens the gate (reason retry) and the drain
#     that reaches the marker's size removes the marker.
#   * STATE/.sweep.trigger is touched on a CLOSED Stop too (at most once per
#     KB_SWEEP_INTERVAL) and ONE `kb-flush-backfill.sh --auto` sweep run is
#     detached with it (a stand-in backfill that understands --auto records the
#     run and logs nothing, so the case is green with or without C7's backfill).
#   * NO token auto-mint on Stop or SessionEnd: with no token on the owner box
#     those firings log a `no token yet: the auto-mint is deferred` line (never
#     NEEDS TOKEN / NOT CONFIGURED -- the next PreCompact mints); on PreCompact
#     the mint runs in the detached CHILD (the parent returns before the 2 s
#     fake `uv` finishes) and the drain then submits with the minted bearer.
#   * a sub-agent transcript (…/subagents/<id>.jsonl) -> nothing at all on any
#     event: no request, no log line, no state file.
#   * dispatcher map: `Stop) CHILDREN="flush-session-to-kb.sh:3 notify-complete.sh:11"`
#     (flush FIRST, 3 + 11 = 14 < the registered 15 s slot); a child killed at
#     its budget leaves ONE `rc 124` line per hour naming it, and the next child
#     still runs.
# STATE FILE NAMES (Appendix I): <okey>.offset, .last, .gate, .retry, .project.
. /rig/lib.sh

new_home stopgate
stub_reset
cfg_rest
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
ST="$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$ST/.checkpoints-trusted-since"
WORK="$HOME/work/gateproj"
mkdir -p "$WORK"
T="$WORK/t.jsonl"
NOW=$(date +%s)
stop() { fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" "$1" Stop)"; }
sums() { log_count ' (ok|partial) bytes='; }
loglines() { [ -f "$LOGF" ] && wc -l <"$LOGF" | tr -d ' ' || echo 0; }
append() { # <bytes> -- grow T by a ragged fixture of that size
  mk_big_transcript "$HOME/grow.jsonl" "$1"
  cat "$HOME/grow.jsonl" >>"$T"
}
closed() { # <label> -- a Stop that must do NOTHING
  local b l
  b=$(stub_total)
  l=$(loglines)
  stop "$SID1"
  assert_eq "0" "$RC" "$1: hook exits 0"
  settle
  settle
  assert_eq "$b" "$(stub_total)" "$1: zero requests"
  assert_eq "$l" "$(loglines)" "$1: no log line"
}
last_ago() { # <seconds> -- rewind .last by hand; a real drain always leaves .last >= .gate, so drop .gate too
  printf '%s' "$((NOW - $1))" >"$ST/$SID1.last"
  rm -f "$ST/$SID1.gate"
}
opened() { # <label> -- a Stop that must drain to EOF
  local s
  s=$(sums)
  stop "$SID1"
  assert_eq "0" "$RC" "$1: hook exits 0"
  wait_firing "$s" 120 || fail "$1: the drain never completed: $(tail -2 "$LOGF" 2>/dev/null | tr '\n' '|')"
  assert_eq "$(wc -c <"$T")" "$(cat "$ST/$SID1.offset" 2>/dev/null)" "$1: drained to EOF"
}
# The piggyback sweep (hybrid_v1 s2.5): a closed Stop touches .sweep.trigger and,
# when the installed backfill understands --auto, detaches ONE `--auto` run. A
# stand-in that understands --auto replaces kb-flush-backfill.sh in every HOME of
# this case so the trigger itself is pinned, the case stays hermetic, and the
# closed-Stop "no log line" assertions stay true whether or not the served
# backfill has learnt --auto yet (C7): it records each run and logs NOTHING (a
# real quiet run writes nothing either).
sweep_standin() {
  cat >"$HOOKS/kb-flush-backfill.sh" <<'EOF'
#!/usr/bin/env bash
# rig stand-in: understands --auto, records the run, logs nothing
printf '%s\n' "$*" >>"$HOME/sweep-ran"
exit 0
EOF
  chmod +x "$HOOKS/kb-flush-backfill.sh"
}
sweeps() { if [ -f "$HOME/sweep-ran" ]; then wc -l <"$HOME/sweep-ran" | tr -d ' '; else echo 0; fi; }
sweep_standin
SID1=s-gate

# ------------------------------------------------------------ 1. idle Stop
step "an idle Stop (2 KB, fresh session) makes zero requests and no log line"
mk_transcript "$T" 20 idle
stop "$SID1"
assert_eq "0" "$RC" "hook exits 0"
assert_empty_file "$OUT_FILE" "nothing printed into the session"
settle
settle
assert_eq "0" "$(stub_total)" "zero requests (not even a handshake)"
if [ -s "$LOGF" ]; then fail "a closed Stop wrote to the log: $(cat "$LOGF")"; else pass "no log line at all"; fi
assert_file_missing "$ST/$SID1.project" "no project pin on a closed gate (nothing below the gate ran)"
assert_file_missing "$ST/$SID1.gate" "no .gate stamp on a closed gate"
assert_file_missing "$ST/$SID1.offset" "no offset file on a closed gate"
if [ -e "$ST/.sweep.trigger" ]; then pass ".sweep.trigger touched on a CLOSED Stop"; else fail "no .sweep.trigger after a closed Stop"; fi
TRIG1=$(stat -c %Y "$ST/.sweep.trigger")
settle
assert_eq "1" "$(sweeps)" "the CLOSED Stop detached exactly one sweep run from the fresh trigger"
assert_file_contains "$HOME/sweep-ran" "--auto" "the sweep ran as kb-flush-backfill.sh --auto"

# ------------------------------------------------------------ 2. size path
step "the size path opens at 4 MB pending (never drained before)"
mk_big_transcript "$T" 4200000
SIZE2=$(wc -c <"$T")
opened "size path"
GATE=$(cat "$ST/$SID1.gate" 2>/dev/null)
LAST=$(cat "$ST/$SID1.last" 2>/dev/null)
case "$GATE" in '' | *[!0-9]*) fail ".gate is not an epoch: [$GATE]" ;; *) pass ".gate stamped by the parent (epoch $GATE)" ;; esac
case "$LAST" in '' | *[!0-9]*) fail ".last is not an epoch: [$LAST]" ;; *) pass ".last written by the child (epoch $LAST)" ;; esac
assert_ge "${LAST:-0}" "${GATE:-1}" ".last >= .gate after the drain (the child started)"
assert_ge "$LAST" "$NOW" ".last is a fresh timestamp"
assert_file_contains "$LOGF" "ok bytes=$SIZE2 (offset $SIZE2/$SIZE2) dup=0 ev=Stop sid=s-gate" "the ok line ends with dup= ev=Stop sid="
if [ -s "$ST/$SID1.project" ]; then pass "the open gate pinned the project (work happens only past the gate)"; else fail "no .project after an open gate"; fi

step "a second 4 MB delta within the 300 s interval is blocked; .last 400 s back re-opens"
append 4200000
closed "interval block (delta 4.2 MB, .last just now)"
assert_eq "$SIZE2" "$(cat "$ST/$SID1.offset")" "offset unchanged while blocked"
last_ago 400
opened "interval elapsed"

# ------------------------------------------------ 2c. no git in the Stop parent
step "an OPEN Stop with a cached pin makes NO git call in the parent (the git context is the child's)"
GP="$HOME/work/gitproj"
mkdir -p "$GP"
(cd "$GP" && git init -q && git config user.email a@b.c && git config user.name t) >/dev/null 2>&1
TG="$GP/t.jsonl"
# The first record carries the cwd -> the pin anchors to this git root
# (how=gitfirst) on the first firing and is CACHED for every later one.
printf '{"type":"user","timestamp":"%s","cwd":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" "$GP" >"$TG"
mk_big_transcript "$HOME/gitfill.jsonl" 30000
cat "$HOME/gitfill.jsonl" >>"$TG"
SUMS=$(sums)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TG" "$GP" s-git PreCompact)"
wait_firing "$SUMS" 60 || fail "the pinning PreCompact never drained: $(tail -2 "$LOGF" | tr '\n' '|')"
assert_eq "$(printf 'gitproj\tgitfirst')" "$(cat "$ST/s-git.project" 2>/dev/null)" "the pin is cached as gitproj by gitfirst"
# A `git` shim that records the CALLER'S SESSION id: the hook parent shares this
# case's session, the setsid-detached child runs in one of its own.
REAL_GIT=$(command -v git)
cat >"$HOME/bin/git" <<EOF
#!/usr/bin/env bash
printf '%s %s\n' "\$(awk '{print \$6}' /proc/self/stat)" "\$*" >>"\$HOME/git-calls"
exec "$REAL_GIT" "\$@"
EOF
chmod +x "$HOME/bin/git"
MYSID=$(awk '{print $6}' /proc/$$/stat)
mk_big_transcript "$HOME/gitgrow.jsonl" 4200000
cat "$HOME/gitgrow.jsonl" >>"$TG"
printf '%s' "$((NOW - 400))" >"$ST/s-git.last"
SUMS=$(sums)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TG" "$GP" s-git Stop)"
assert_eq "0" "$RC" "the open Stop exits 0"
wait_firing "$SUMS" 120 || fail "the open-gate drain never completed: $(tail -2 "$LOGF" | tr '\n' '|')"
assert_eq "$(wc -c <"$TG")" "$(cat "$ST/s-git.offset" 2>/dev/null)" "the child drained to EOF"
PARENT_GIT=$(grep -c "^$MYSID " "$HOME/git-calls" 2>/dev/null) || PARENT_GIT=0
CHILD_GIT=$(grep -vc "^$MYSID " "$HOME/git-calls" 2>/dev/null) || CHILD_GIT=0
note "git calls: parent session $MYSID -> ${PARENT_GIT:-0}, other sessions -> ${CHILD_GIT:-0}"
assert_eq "0" "${PARENT_GIT:-0}" "zero git calls from the parent's session (gate -> cached pin -> token file -> detach)"
assert_ge "${CHILD_GIT:-0}" "1" "the detached child computed the git context from its own session"
assert_eq "gitproj" "$(stub_field '.session_uri=="session:s-git"' '.repo' | tail -1)" "the child still put repo=gitproj on the wire"
rm -f "$HOME/bin/git"

# ------------------------------------------------------------- 3. age path
step "the age path: 64 KB pending needs 1800 s since .last; 1000 s is not enough"
append 120000
last_ago 1000
closed "age path at 1000 s"
last_ago 1900
opened "age path at 1900 s"
step "the age floor: 10 KB pending never opens, however old .last is"
append 10000
last_ago 9000
closed "below the 64 KB floor"

# ------------------------------------------------- 4. killed open gate back-off
step "a .gate newer than .last (parent killed before the detach) closes the gate for one interval"
append 120000
last_ago 1900
printf '%s' "$((NOW - 10))" >"$ST/$SID1.gate"
closed "killed gate 10 s ago"
assert_eq "$((NOW - 10))" "$(cat "$ST/$SID1.gate")" "the closed Stop did not re-stamp .gate"
printf '%s' "$((NOW - 400))" >"$ST/$SID1.gate"
opened "killed gate one interval ago"
assert_ge "$(cat "$ST/$SID1.gate")" "$NOW" "the re-opened gate re-stamped .gate"

# ------------------------------------------------------ 5. retry marker paths
step "a retry marker at give-up (n=20) suppresses the age path"
append 120000
last_ago 1900
printf '%s 20 502 %s' "$((NOW - 7200))" "$(wc -c <"$T")" >"$ST/$SID1.retry"
closed "give-up marker, 120 KB pending, .last 1900 s"
step "a retry marker below give-up with its back-off elapsed opens the gate, and the drain removes it"
printf '%s 3 502 %s' "$((NOW - 1300))" "$(wc -c <"$T")" >"$ST/$SID1.retry"   # n=3 -> back-off 1200 s
opened "retry due"
assert_file_missing "$ST/$SID1.retry" "the marker is removed once the offset reached the size it recorded"
step "a retry marker inside its back-off holds even a 4 MB delta"
append 4200000
last_ago 1900
printf '%s 1 502 %s' "$((NOW - 10))" "$(wc -c <"$T")" >"$ST/$SID1.retry"
closed "back-off hold (n=1, 10 s old)"
rm -f "$ST/$SID1.retry"
opened "the same delta once the marker is gone"

# ------------------------------------------- 5b. a refused (unseeded) session
step "an unseeded refusal closes the gate: the first open Stop refuses once, every later Stop costs nothing"
TO="$WORK/old.jsonl"
RIG_FIXTURE_TS="1999-01-01T00:00:00.000Z" mk_big_transcript "$TO" 4200000 # predates the 2000-01-01 stamp
B=$(stub_total)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TO" "$WORK" s-old Stop)"
assert_eq "0" "$RC" "hook exits 0"
wait_log "unseeded: refusing to send .* sid=s-old" 30 || fail "no unseeded refusal line: $(tail -2 "$LOGF" | tr '\n' '|')"
settle
assert_eq "$B" "$(stub_total)" "nothing was sent for the pre-stamp transcript"
if [ -e "$ST/s-old.unseeded" ]; then pass ".unseeded written by the refusing child"; else fail "no .unseeded marker"; fi
assert_file_missing "$ST/s-old.offset" "no offset for the refused session"
if [ -s "$ST/s-old.gate" ]; then pass "the FIRST Stop did open the gate (.gate stamped) -- the refusal is the child's"; else fail "the first Stop never opened the gate"; fi
printf '%s' "$((NOW - 1900))" >"$ST/s-old.last" # every size/age condition holds; only the marker can close the gate
rm -f "$ST/s-old.gate"
L=$(loglines)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TO" "$WORK" s-old Stop)"
assert_eq "0" "$RC" "hook exits 0"
settle
settle
assert_eq "$B" "$(stub_total)" "a later Stop on the refused session makes zero requests"
assert_eq "$L" "$(loglines)" "and writes no log line (closed by the .unseeded marker: one more stat)"
assert_file_missing "$ST/s-old.gate" "the gate stayed closed (no .gate stamp, no pin, no lock)"

# ------------------------------------------------------------ 6. the trigger
step ".sweep.trigger is re-touched on a closed Stop only once the interval has passed"
assert_eq "$TRIG1" "$(stat -c %Y "$ST/.sweep.trigger")" "not re-touched within KB_SWEEP_INTERVAL (mtime unchanged since step 1)"
touch -d '1 hour ago' "$ST/.sweep.trigger" 2>/dev/null || touch -t 202001010000 "$ST/.sweep.trigger"
OLD=$(stat -c %Y "$ST/.sweep.trigger")
closed "delta 0 (trigger probe)"
assert_ge "$(stat -c %Y "$ST/.sweep.trigger")" "$((OLD + 1))" "an hour-old trigger was re-touched by a CLOSED Stop"
settle
assert_eq "2" "$(sweeps)" "exactly one more sweep run: the re-touch detached it, no firing inside the interval did"

# ------------------------------------------------------ 7. mint placement
new_home stopmint
stub_reset
fake_kb_repo
# A `uv` that "mints" the rig's accepted bearer after 2 s, so a mint that runs
# in the PARENT is visible as wall time and a mint that runs at all is visible
# as $HOME/uv-ran + the bearer on the wire.
cat >"$HOME/bin/uv" <<EOF
#!/usr/bin/env bash
echo "uv \$*" >>"\$HOME/uv-ran"
sleep 2
printf '%s\n' "$RIG_TOKEN"
EOF
chmod +x "$HOME/bin/uv"
install_hooks >/dev/null
sweep_standin
mkdir -p "$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"
printf '# REMOTE\nKB_REST_URL=%s\n' "$STUB" | write_cfg # no KB_TOKEN, no kb-tokens.env: the mint path is reachable
WM="$HOME/work/mintrepo"
mkdir -p "$WM"
(cd "$WM" && git init -q && git config user.email a@b.c && git config user.name t) >/dev/null 2>&1
TM="$WM/t.jsonl"
mk_big_transcript "$TM" 4200000
step "NO mint on Stop (open gate: 4 MB, never drained) and NO mint on SessionEnd"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TM" "$WM" s-mint Stop)"
settle
settle
settle
assert_file_missing "$HOME/uv-ran" "Stop never reached the mint"
assert_eq "0" "$(stub_total)" "and nothing was submitted (no token)"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TM" "$WM" s-mint SessionEnd)"
settle
settle
settle
assert_file_missing "$HOME/uv-ran" "SessionEnd never reached the mint"
assert_not_contains "$(cat "$LOGF" 2>/dev/null)" "auto-minted" "nothing was auto-minted on Stop/SessionEnd"
assert_eq "2" "$(log_count 'flush mintrepo no token yet: the auto-mint is deferred to the next PreCompact/Backfill firing \(never on Stop/SessionEnd\) ev=(Stop|SessionEnd) sid=s-mint')" "Stop and SessionEnd each logged ONE deferred-mint line naming the event"
assert_eq "0" "$(log_count 'NEEDS TOKEN|NOT CONFIGURED|NOT CAPTURED|FAILED')" "no dropped-session token for a project the next PreCompact mints for"
step "on PreCompact the mint runs in the detached CHILD and the drain uses the minted bearer"
T0=$(date +%s%N 2>/dev/null || date +%s)
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TM" "$WM" s-mint PreCompact)"
T1=$(date +%s%N 2>/dev/null || date +%s)
assert_eq "0" "$RC" "hook exits 0"
case "$T0$T1" in *[!0-9]*) note "no ns clock" ;; *)
  MS=$(((T1 - T0) / 1000000))
  note "parent wall time ${MS} ms while the fake mint sleeps 2000 ms"
  assert_le "$MS" "1500" "the parent returned before the 2 s mint could have finished (the mint is in the child)"
  ;;
esac
wait_log "auto-minted a project token" 30 || fail "no auto-minted line: $(tail -3 "$LOGF" | tr '\n' '|')"
if [ -f "$HOME/uv-ran" ] && grep -q "run python" "$HOME/uv-ran"; then pass "the mint ran (uv run python)"; else fail "uv-ran missing: the mint never ran"; fi
assert_file_contains "$HOME/.claude/kb-tokens.env" "KB_TOKEN_MINTREPO=$RIG_TOKEN" "the minted token was stored under the project key"
wait_firing 0 120 || fail "the drain after the mint never completed"
assert_eq "$RIG_TOKEN" "$(stub_field '.transport=="rest"' '.bearer // empty' | tail -1)" "the drain submitted with the bearer minted moments earlier"
assert_eq "$(wc -c <"$TM")" "$(cat "$HOOKS/state/s-mint.offset")" "drained to EOF with the minted token"

# ------------------------------------------------------ 8. sub-agent transcripts
new_home stopsub
stub_reset
cfg_rest
install_hooks >/dev/null
sweep_standin
mkdir -p "$HOOKS/state" "$HOME/.claude/projects/-home-tester-work/sess-main/subagents"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"
TS="$HOME/.claude/projects/-home-tester-work/sess-main/subagents/agent-1.jsonl"
mk_big_transcript "$TS" 200000
step "a sub-agent transcript is ignored on every event: no request, no log line, no state"
for ev in PreCompact SessionEnd Stop; do
  fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TS" "$HOME/work" agent-1 "$ev")"
  assert_eq "0" "$RC" "$ev on a subagents path exits 0"
done
settle
settle
assert_eq "0" "$(stub_total)" "zero requests for the sub-agent transcript"
if [ -s "$LOGF" ]; then fail "a sub-agent firing wrote to the log: $(cat "$LOGF")"; else pass "no log line"; fi
assert_file_missing "$HOOKS/state/agent-1.offset" "no offset file for the sub-agent"
assert_file_missing "$HOOKS/state/agent-1.project" "no pin for the sub-agent"
# positive control: the SAME bytes outside a subagents/ dir do flush
TP="$HOME/work/plain.jsonl"
cp "$TS" "$TP"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TP" "$HOME/work" plain-1 PreCompact)"
wait_firing 0 60 || fail "positive control: the same transcript outside subagents/ did not flush"
assert_ge "$(stub_total)" "1" "positive control: outside subagents/ the bytes were submitted"

# ------------------------------------------------------ 9. the dispatcher map
step "dispatcher: Stop runs the flush FIRST under 3 s, then notify under 11 s (14 < 15)"
DISP="$HOOKS/kb-hook-dispatch.sh"
assert_file_contains "$DISP" 'Stop) CHILDREN="flush-session-to-kb.sh:3 notify-complete.sh:11"' "the Stop map line is pinned verbatim"
FB=$(grep -o 'Stop) CHILDREN="flush-session-to-kb.sh:[0-9]*' "$DISP" | grep -o '[0-9]*$')
NB=$(grep 'Stop) CHILDREN=' "$DISP" | grep -o 'notify-complete.sh:[0-9]*' | grep -o '[0-9]*$')
assert_le "$((FB + NB))" "14" "flush $FB + notify $NB fits under the registered 15 s Stop slot"
assert_eq "15" "$("$RIG_JQ" -r '.hooks.Stop[0].hooks[0].timeout' "$SETTINGS")" "the registered Stop slot is still 15 (never widened)"
step "a child killed at its budget leaves ONE rc-124 line per hour and the next child still runs"
new_home stopdisp
mkdir -p "$HOOKS" "$HOME/rec"
cp "$SKILL/kb-hook-dispatch.sh" "$HOOKS/"
printf '#!/usr/bin/env bash\nprintf "flush\\n" >>"%s/order"\nexec >/dev/null 2>&1 </dev/null\nsleep 30\n' "$HOME/rec" >"$HOOKS/flush-session-to-kb.sh"
printf '#!/usr/bin/env bash\nprintf "notify %%s\\n" "$1" >>"%s/order"\n' "$HOME/rec" >"$HOOKS/notify-complete.sh"
chmod +x "$HOOKS"/*.sh
D0=$(date +%s)
printf '{"hook_event_name":"Stop","session_id":"x"}' | bash "$HOOKS/kb-hook-dispatch.sh" Stop >/dev/null 2>&1
RCD=$?
D1=$(date +%s)
assert_eq "0" "$RCD" "the dispatcher exits 0 despite the killed child"
assert_le "$((D1 - D0))" "8" "the wedged flush cost its 3 s budget, not the slot (took $((D1 - D0)) s)"
assert_eq "flush
notify Stop" "$(cat "$HOME/rec/order")" "flush ran first, was killed, notify still ran with the event as \$1"
assert_eq "1" "$(log_count 'dispatch flush-session-to-kb.sh hit its 3s budget on Stop \(rc 124\)')" "exactly one rc-124 line naming the child"
printf '{"hook_event_name":"Stop","session_id":"x"}' | bash "$HOOKS/kb-hook-dispatch.sh" Stop >/dev/null 2>&1
assert_eq "1" "$(log_count 'rc 124')" "a second kill within the hour adds no line (rate-limited)"
for tok in FAILED "BUG:" "NOT CONFIGURED" "NOT CAPTURED" "NEEDS TOKEN"; do
  assert_not_contains "$(grep 'rc 124' "$LOGF")" "$tok" "the rc-124 line avoids the pre-brief's dropped-session token [$tok]"
done

finish

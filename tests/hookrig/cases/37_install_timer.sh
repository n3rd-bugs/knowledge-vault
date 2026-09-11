#!/usr/bin/env bash
# CASE: install.sh -- the checkpoint trust stamp and the sweep timer render
# PINS: wave C install.sh -- the trust stamp is written once and never moved; the sweep timer is gated, best-effort and byte-identical (proposal_v2 C9 / hybrid_v1 s2.5-2.6, KB 33784)
#   * install.sh writes state/.checkpoints-trusted-since (ISO-8601 UTC) ONCE,
#     BEFORE it copies any hook into place, and NEVER overwrites it -- the
#     stamp is what lets the never-delete-offsets hook tell "a genuinely new
#     transcript" from "a pre-existing one whose checkpoint an older hook
#     deleted", so moving it forward on a re-install would turn every live
#     session into an `unseeded` refusal (install.sh, the STAMP block).
#   * the systemd --user timer pair kb-flush-sweep.{service,timer} is rendered
#     from a heredoc AFTER the .setup-version stamp, only when `systemctl
#     --user is-system-running` ANSWERS (`degraded` with exit 1 counts -- the
#     owner box reports exactly that), the installed backfill understands
#     `--auto`, and the unit dir is writable; byte-identical across runs, with
#     ExecStartPre touching state/.sweep.trigger and ExecStart running
#     kb-flush-backfill.sh --auto (paths double-quoted, systemd Exec= quoting,
#     so a HOME with a space still runs); every systemctl call is `|| true`,
#     so a failing `enable` still leaves a completed install; an unwritable
#     unit dir, KB_SWEEP_TIMER=0 and KB_SWEEP_INTERVAL=0 (the OFF switch) each
#     produce a `not scheduled (<why>)`; exactly ONE `sweep:` line is printed
#     whatever happened; kb-flush.env is never edited.
#   * with NO systemctl (this container) the install completes with a
#     `not scheduled` reason and renders nothing.
# The timer-path steps (part 2) need a served kb-flush-backfill.sh that
# carries `--auto` (wave C item C7). Until it lands the installer refuses to
# schedule a unit that would exit 2 every tick, and part 2 reports itself
# skipped -- loudly, as a `--` note, never as a silent pass.
# Cases 03 (idempotent settings) and 28 (the earned stamp) are unchanged and
# still cover the rest of install.sh.
. /rig/lib.sh

UNITD_REL=".config/systemd/user"

# Fake user managers. They land in $HOME/bin, which new_home puts first on PATH.
fake_systemctl_degraded() { # answers `degraded` with exit 1, like the owner box
  cat >"$HOME/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
echo "systemctl $*" >>"$HOME/systemctl.calls"
case "$*" in *is-system-running*) echo degraded; exit 1 ;; esac
exit 0
EOF
  chmod +x "$HOME/bin/systemctl"
}
fake_systemctl_enable_fails() { # answers, but `enable --now` fails
  cat >"$HOME/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
echo "systemctl $*" >>"$HOME/systemctl.calls"
case "$*" in
  *is-system-running*) echo running; exit 0 ;;
  *enable*) echo "Failed to enable unit: Unit file kb-flush-sweep.timer does not exist." >&2; exit 1 ;;
esac
exit 0
EOF
  chmod +x "$HOME/bin/systemctl"
}
# An `install` wrapper that records, for every hook it copies, whether the
# trust stamp and the version stamp ALREADY existed at that moment -- the only
# way to assert ordering ("stamp before hooks", "timer after .setup-version")
# from outside the installer.
record_install_order() {
  cat >"$HOME/bin/install" <<'EOF'
#!/usr/bin/env bash
last=""
for a in "$@"; do last="$a"; done
st="absent"; [ -e "$HOME/.claude/hooks/state/.checkpoints-trusted-since" ] && st="present"
sv="absent"; [ -e "$HOME/.claude/hooks/.setup-version" ] && sv="present"
printf '%s trust=%s version=%s\n' "$(basename "$last")" "$st" "$sv" >>"$HOME/install.order"
exec /usr/bin/install "$@"
EOF
  chmod +x "$HOME/bin/install"
}
run_install() { # [skill-dir] -> $OUT_I (combined output), $RC_I
  OUT_I=$(KB_REST_URL="$STUB" KB_TOKEN="$RIG_TOKEN" bash "${1:-$SKILL}/install.sh" 2>&1)
  RC_I=$?
}
sweep_lines() { printf '%s\n' "$OUT_I" | grep -c '^sweep:' || true; }
sweep_line() { printf '%s\n' "$OUT_I" | grep -m1 '^sweep:'; }
completed() { # the summary block was reached and the version stamp earned
  assert_eq "0" "$RC_I" "$1: install.sh exits 0"
  assert_eq "1" "$(sweep_lines)" "$1: exactly one sweep: line"
  assert_contains "$OUT_I" "hook:     $HOOKS/flush-session-to-kb.sh" "$1: the summary block was reached"
  assert_ne "" "$(cat "$HOOKS/.setup-version" 2>/dev/null)" "$1: .setup-version stamped"
}

BF_HAS_AUTO=0
grep -q -- '--auto' "$SKILL/kb-flush-backfill.sh" && BF_HAS_AUTO=1

# ============================================ PART 1: stage-independent
# ------------------------------------------------------ (a) degraded, exit 1
step "a user manager answering 'degraded' (exit 1): install completes, stamp written"
new_home timer_degraded
stub_reset
cfg_rest
fake_systemctl_degraded
record_install_order
CFG_BEFORE=$(cat "$CFG")
UNITD="$HOME/$UNITD_REL"
STAMP="$HOOKS/state/.checkpoints-trusted-since"
run_install
completed "degraded manager"
assert_eq "$CFG_BEFORE" "$(cat "$CFG")" "kb-flush.env untouched"
if [ "$BF_HAS_AUTO" = 1 ]; then
  assert_contains "$(sweep_line)" "sweep:    scheduled" "the sweep is reported scheduled"
  assert_contains "$(sweep_line)" "degraded" "...and the line names the manager state it accepted"
else
  assert_contains "$(sweep_line)" "not scheduled (the installed kb-flush-backfill.sh has no --auto yet" "pre-C7 backfill: the timer is NOT scheduled and the reason names update.sh"
  assert_file_missing "$UNITD/kb-flush-sweep.timer" "pre-C7 backfill: no unit that would exit 2 every tick"
fi

step "the trust stamp: ISO UTC, written BEFORE the hooks"
STAMP1=$(cat "$STAMP" 2>/dev/null)
case "$STAMP1" in
  [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z) pass "stamp is ISO-8601 UTC: $STAMP1" ;;
  *) fail "stamp is not ISO-8601 UTC: [$STAMP1]" ;;
esac
ORDER=$(cat "$HOME/install.order" 2>/dev/null)
FLUSH_ORDER=$(printf '%s\n' "$ORDER" | grep -m1 '^flush-session-to-kb.sh ')
assert_contains "$FLUSH_ORDER" "trust=present" "the stamp already existed when the flush hook was copied in"
assert_contains "$OUT_I" "stamp:    $STAMP  (written" "the summary says the stamp was written"

step "a second install: stamp kept, still one sweep: line"
sleep 1.1 # a rewritten stamp would carry a different second
run_install
completed "re-install"
assert_eq "$STAMP1" "$(cat "$STAMP")" "the trust stamp was NOT overwritten"
assert_contains "$OUT_I" "stamp:    $STAMP  (kept $STAMP1" "the summary says the stamp was kept"
assert_eq "$CFG_BEFORE" "$(cat "$CFG")" "kb-flush.env still untouched"

step "a pre-existing stamp is honoured, never moved"
printf '2020-01-01T00:00:00Z\n' >"$STAMP"
run_install
assert_eq "2020-01-01T00:00:00Z" "$(cat "$STAMP")" "an older stamp survives a re-install"
assert_eq "1" "$(sweep_lines)" "still exactly one sweep: line"

step "an unreadable pre-existing stamp is kept and does not abort the install"
chmod 000 "$STAMP"
run_install
completed "unreadable stamp"
assert_contains "$OUT_I" "stamp:    $STAMP  (kept (unreadable" "the summary says so"
chmod 644 "$STAMP"
assert_eq "2020-01-01T00:00:00Z" "$(cat "$STAMP")" "...and it was not replaced"

step "KB_SWEEP_TIMER=0 opts out with a reason"
OUT_I=$(KB_REST_URL="$STUB" KB_TOKEN="$RIG_TOKEN" KB_SWEEP_TIMER=0 bash "$SKILL/install.sh" 2>&1)
assert_eq "1" "$(sweep_lines)" "one sweep: line"
assert_contains "$(sweep_line)" "not scheduled (KB_SWEEP_TIMER=0" "the opt-out is said out loud"

step "KB_SWEEP_INTERVAL=0 is the OFF switch, never a 0s cadence"
OUT_I=$(KB_REST_URL="$STUB" KB_TOKEN="$RIG_TOKEN" KB_SWEEP_INTERVAL=0 bash "$SKILL/install.sh" 2>&1)
assert_eq "1" "$(sweep_lines)" "one sweep: line"
assert_contains "$(sweep_line)" "not scheduled (KB_SWEEP_INTERVAL=0" "0 is reported as off"
if [ -f "$UNITD/kb-flush-sweep.timer" ]; then
  assert_not_contains "$(cat "$UNITD/kb-flush-sweep.timer")" "OnUnitActiveSec=0s" "no back-to-back timer was rendered"
else
  pass "no timer rendered with the sweep off"
fi

step "a backfill without --auto: the unit is refused, the install completes"
SKILL_NOAUTO="$RIG_HOMEBASE/skill-noauto"
rm -rf "$SKILL_NOAUTO"
cp -r "$SKILL" "$SKILL_NOAUTO"
printf '#!/usr/bin/env bash\nprintf "stub backfill: a sweep that predates the flag\\n" >&2\nexit 2\n' >"$SKILL_NOAUTO/kb-flush-backfill.sh"
chmod +x "$SKILL_NOAUTO/kb-flush-backfill.sh"
new_home timer_noauto
stub_reset
cfg_rest
fake_systemctl_degraded
run_install "$SKILL_NOAUTO"
assert_eq "0" "$RC_I" "install.sh exits 0"
assert_eq "1" "$(sweep_lines)" "exactly one sweep: line"
assert_contains "$(sweep_line)" "not scheduled (the installed kb-flush-backfill.sh has no --auto yet" "the reason names the missing flag"
assert_file_missing "$HOME/$UNITD_REL/kb-flush-sweep.timer" "nothing rendered for a backfill that would exit 2 every tick"
assert_not_contains "$(cat "$HOME/systemctl.calls" 2>/dev/null)" "enable" "nothing was enabled"
assert_contains "$OUT_I" "hook:     $HOOKS/flush-session-to-kb.sh" "the summary block was reached"
assert_ne "" "$(cat "$HOOKS/state/.checkpoints-trusted-since" 2>/dev/null)" "the trust stamp is written regardless"

# ------------------------------------------------------ (b) systemctl missing
step "no systemctl at all (this container): install completes, nothing rendered"
new_home timer_missing
stub_reset
cfg_rest
if command -v systemctl >/dev/null 2>&1; then
  note "systemctl unexpectedly present on PATH; parking it"
  mkdir -p "$HOME/nosys"
  for f in /usr/local/bin/* /usr/bin/* /bin/*; do
    b=$(basename "$f")
    [ "$b" = systemctl ] && continue
    [ -e "$HOME/nosys/$b" ] || ln -s "$f" "$HOME/nosys/$b" 2>/dev/null
  done
  export PATH="$HOME/bin:$HOME/nosys"
fi
run_install
completed "no systemctl"
assert_contains "$(sweep_line)" "not scheduled (no systemctl" "the reason is named"
assert_file_missing "$HOME/$UNITD_REL/kb-flush-sweep.timer" "no timer rendered without a user manager"
assert_ne "" "$(cat "$HOOKS/state/.checkpoints-trusted-since" 2>/dev/null)" "the trust stamp is written regardless"
export PATH="$HOME/bin:$RIG_BASEPATH"

# ============================================ PART 2: the timer path (needs C7)
if [ "$BF_HAS_AUTO" != 1 ]; then
  note "SKIPPED part 2 (timer render, enable-fails, unwritable dir, spacey HOME): the served kb-flush-backfill.sh has no --auto yet (wave C item C7); install.sh refuses to schedule it"
  finish
fi

# ------------------------------------------------------ (c) the render itself
step "degraded manager + --auto backfill: the unit pair is rendered and enabled"
new_home timer_render
stub_reset
cfg_rest
fake_systemctl_degraded
record_install_order
UNITD="$HOME/$UNITD_REL"
run_install
completed "render"
assert_contains "$(sweep_line)" "sweep:    scheduled" "the sweep is reported scheduled"
assert_eq "true" "$([ -f "$UNITD/kb-flush-sweep.service" ] && [ -f "$UNITD/kb-flush-sweep.timer" ] && echo true)" "both unit files rendered"
assert_file_contains "$UNITD/kb-flush-sweep.service" "ExecStart=$(command -v bash) \"$HOOKS/kb-flush-backfill.sh\" --auto" "ExecStart runs the sweep in --auto (path quoted)"
assert_file_contains "$UNITD/kb-flush-sweep.service" "ExecStartPre=$(command -v touch) \"$HOOKS/state/.sweep.trigger\"" "ExecStartPre touches the piggyback trigger first (path quoted)"
assert_not_contains "$(cat "$UNITD/kb-flush-sweep.service")" "Documentation=" "no unquotable file:// line"
assert_file_contains "$UNITD/kb-flush-sweep.service" "Type=oneshot" "oneshot service"
assert_file_contains "$UNITD/kb-flush-sweep.service" "KillMode=process" "a drain holding its lock is not killed at unit exit"
assert_file_contains "$UNITD/kb-flush-sweep.timer" "OnBootSec=10min" "boot trigger"
assert_file_contains "$UNITD/kb-flush-sweep.timer" "OnUnitActiveSec=1800s" "default cadence = KB_SWEEP_INTERVAL 1800"
assert_file_contains "$UNITD/kb-flush-sweep.timer" "Persistent=true" "missed windows fire at the next boot"
assert_file_contains "$UNITD/kb-flush-sweep.timer" "Unit=kb-flush-sweep.service" "the timer names its service"
CALLS=$(cat "$HOME/systemctl.calls" 2>/dev/null)
assert_contains "$CALLS" "--user daemon-reload" "daemon-reload was issued"
assert_contains "$CALLS" "--user enable --now kb-flush-sweep.timer" "enable --now was issued"
SVC_ORDER=$(printf '%s\n' "$(cat "$HOME/install.order")" | grep -m1 '^kb-flush-sweep.service ')
assert_contains "$SVC_ORDER" "version=present" "the timer was rendered AFTER the .setup-version stamp"
assert_eq "0" "$(ls -A "$UNITD" | grep -c 'render' || true)" "no render scratch left in the unit dir"

step "a second install: unit files byte-identical, still one sweep: line"
cp "$UNITD/kb-flush-sweep.service" "$HOME/svc.1"
cp "$UNITD/kb-flush-sweep.timer" "$HOME/tmr.1"
run_install
completed "re-render"
assert_contains "$(sweep_line)" "unit files unchanged" "the re-run says the units did not change"
if cmp -s "$HOME/svc.1" "$UNITD/kb-flush-sweep.service" && cmp -s "$HOME/tmr.1" "$UNITD/kb-flush-sweep.timer"; then
  pass "unit files byte-identical across two runs"
else
  fail "unit files changed between runs: $(diff "$HOME/svc.1" "$UNITD/kb-flush-sweep.service" | head -3 | tr '\n' '|')"
fi

step "KB_SWEEP_INTERVAL changes the cadence (and only the cadence)"
OUT_I=$(KB_REST_URL="$STUB" KB_TOKEN="$RIG_TOKEN" KB_SWEEP_INTERVAL=900 bash "$SKILL/install.sh" 2>&1)
assert_file_contains "$UNITD/kb-flush-sweep.timer" "OnUnitActiveSec=900s" "cadence follows KB_SWEEP_INTERVAL"
assert_eq "1" "$(sweep_lines)" "one sweep: line"
if cmp -s "$HOME/svc.1" "$UNITD/kb-flush-sweep.service"; then pass "the service file did not change"; else fail "the service file changed with the interval"; fi

# ------------------------------------------------------ (d) enable fails
step "systemctl answers but enable --now fails: install completes, honest line"
new_home timer_enablefail
stub_reset
cfg_rest
fake_systemctl_enable_fails
CFG_BEFORE=$(cat "$CFG")
UNITD="$HOME/$UNITD_REL"
run_install
completed "enable fails"
assert_contains "$(sweep_line)" "not scheduled" "a failed enable is NOT reported as scheduled"
assert_contains "$(sweep_line)" "enable --now kb-flush-sweep.timer failed" "...and names the failed command"
assert_eq "true" "$([ -f "$UNITD/kb-flush-sweep.timer" ] && echo true)" "the unit files were still rendered for a by-hand enable"
assert_eq "$CFG_BEFORE" "$(cat "$CFG")" "kb-flush.env untouched"
cp "$UNITD/kb-flush-sweep.service" "$HOME/svc.1"
cp "$UNITD/kb-flush-sweep.timer" "$HOME/tmr.1"
run_install
assert_eq "1" "$(sweep_lines)" "one sweep: line on the re-run"
if cmp -s "$HOME/svc.1" "$UNITD/kb-flush-sweep.service" && cmp -s "$HOME/tmr.1" "$UNITD/kb-flush-sweep.timer"; then
  pass "unit files byte-identical across two runs (enable-fails path)"
else
  fail "unit files changed between runs on the enable-fails path"
fi

# ------------------------------------------------------ (e) unwritable unit dir
step "an unwritable ~/.config/systemd: install completes, not scheduled (could not write)"
new_home timer_unwritable
stub_reset
cfg_rest
fake_systemctl_degraded
mkdir -p "$HOME/.config/systemd"
chmod 555 "$HOME/.config/systemd"
run_install
completed "unwritable unit dir"
assert_contains "$(sweep_line)" "not scheduled (could not write" "the reason is the unit dir"
assert_not_contains "$(cat "$HOME/systemctl.calls" 2>/dev/null)" "enable" "nothing was enabled"
run_install
assert_eq "1" "$(sweep_lines)" "one sweep: line on the re-run"
assert_eq "0" "$RC_I" "re-run exits 0 too"
chmod 755 "$HOME/.config/systemd"

# ------------------------------------------------------ (f) HOME with a space
step "a HOME with a space: the unit's paths are quoted (systemd Exec= quoting)"
new_home "timer with space"
stub_reset
cfg_rest
fake_systemctl_degraded
UNITD="$HOME/$UNITD_REL"
run_install
completed "spacey HOME"
assert_contains "$(sweep_line)" "sweep:    scheduled" "scheduled"
assert_file_contains "$UNITD/kb-flush-sweep.service" "ExecStart=$(command -v bash) \"$HOOKS/kb-flush-backfill.sh\" --auto" "ExecStart carries the spacey path inside double quotes"
assert_file_contains "$UNITD/kb-flush-sweep.service" "ExecStartPre=$(command -v touch) \"$HOOKS/state/.sweep.trigger\"" "ExecStartPre likewise"

finish

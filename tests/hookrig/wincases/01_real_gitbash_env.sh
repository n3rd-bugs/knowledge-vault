#!/usr/bin/env bash
# CASE: REAL Git Bash — the platform facts the container can only simulate
# PINS: the winsim image FAKES all of this (a stubbed `uname -s`, a shell-script
#       `cygpath`, a hand-written `timeout.exe`). If the simulation ever drifts
#       from the real box, this case is what notices. It asserts the platform
#       PREMISES every other Windows case builds on, so a failure here explains
#       the rest of the lane rather than leaving ten cases mysteriously red.
. "$RIG_SANDBOX/winlib.sh"

new_home realenv
load_portable_block

step "we are on a real Windows box, not the simulation"
assert_win
if kb_is_windows; then pass "kb_is_windows() true (the ONE platform switch)"; else fail "kb_is_windows() false on real Git Bash — uname is $(uname -s)"; fi

step "the tools the skill probes for, as they really are here"
note "uname   = $(uname -s)"
note "bash    = $BASH_VERSION"
note "EXEPATH = ${EXEPATH:-unset}"
for t in cygpath setsid perl curl jq python python3; do
  p=$(command -v "$t" 2>/dev/null || true)
  note "$t = ${p:-MISSING}"
done

# Git Bash has no setsid; that is WHY kb_detach_cmd exists. Assert the premise
# rather than assume it, so a future Git Bash that ships setsid is noticed.
step "detach: setsid is absent on Git Bash, so nohup must be chosen"
if command -v setsid >/dev/null 2>&1; then
  note "setsid EXISTS on this box (unusual for Git Bash)"
  assert_eq "setsid" "$(kb_detach_cmd)" "kb_detach_cmd prefers setsid when present"
else
  pass "no setsid on PATH (expected on Git Bash)"
  assert_eq "nohup" "$(kb_detach_cmd)" "kb_detach_cmd falls back to nohup"
fi

step "the skill's files arrived with LF endings (a CRLF checkout breaks bash)"
for f in install.sh flush-session-to-kb.sh kb-session-brief.sh kb-mcp-call.sh; do
  if grep -qU $'\r' "$SKILL/$f" 2>/dev/null; then
    fail "$f contains CR — Git Bash would fail with \$'\\r': command not found"
  else
    pass "$f is LF-clean"
  fi
done

finish

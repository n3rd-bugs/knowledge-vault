#!/usr/bin/env bash
# IMAGE: winsim
# CASE: WINDOWS with NO cygpath on PATH
# PINS: the skill's Windows path handling used to be `cygpath "$T" 2>/dev/null ||
#       printf '%s' "$T"` -- i.e. if cygpath was missing the hook carried on with
#       a raw C:\Users\... string, which no POSIX test/basename can resolve, so
#       the flush became a silent no-op (item 1174's bug class, on the platform
#       item 1153 says we support). cygpath is NOT guaranteed: it ships with Git
#       Bash/MSYS but a stripped bash-on-Windows (busybox-w32, WSL interop
#       shells, a PATH trimmed by an admin policy) may not have it.
#       kb_winpath must therefore fall back to the pure-shell mapping Git Bash
#       itself uses -- C:\Users\First Last\p -> /c/Users/First Last/p -- and the
#       flush must keep working. install.sh has the mirror-image problem: with no
#       `cygpath -w` it must still register something Windows can spawn
#       (kb_nativepath: $EXEPATH-derived, else a bare bash.exe resolved through
#       the Windows PATH) and NEVER a bare .sh (item 1152 / 496).
# ALSO PINS: the conversion is exercised directly as a unit, from the SHARED
#       block extracted out of the installed hook -- upper and lower drive
#       letters, forward- and backslash forms, spaces, and the pass-through of
#       an already-POSIX path.
. /rig/lib.sh

new_home nocygpath
fake_uname_windows
record_nohup

step "the box looks like Git Bash, but has no cygpath"
assert_eq "MINGW64_NT-10.0-22631" "$(uname -s)" "uname -s reports MINGW"
if command -v cygpath >/dev/null 2>&1; then fail "cygpath is on PATH -- this case proves nothing"; else pass "no cygpath"; fi

cfg_mcp
install_hooks >/dev/null

step "install still registers an exe+args command Windows can spawn"
CMD=$("$RIG_JQ" -r '.hooks.PreCompact[0].hooks[0].command' "$SETTINGS")
ARG0=$("$RIG_JQ" -r '.hooks.PreCompact[0].hooks[0].args[0]' "$SETTINGS")
assert_eq "bash.exe" "$CMD" "falls back to a bare bash.exe (resolved through the Windows PATH)"
assert_eq "$HOOKS/kb-hook-dispatch.sh" "$ARG0" "the dispatcher travels as args[0]"
case "$CMD" in *.sh) fail "registered a bare .sh -- unspawnable on Windows" ;; *) pass "not a bare .sh" ;; esac
for ev in SessionEnd SessionStart UserPromptSubmit; do
  assert_eq "bash.exe" "$("$RIG_JQ" -r --arg e "$ev" '.hooks[$e][0].hooks[0].command' "$SETTINGS")" "$ev too"
done

step "and with Git Bash's EXEPATH set, it derives the real bash.exe"
new_home nocygpath_exepath
fake_uname_windows
export EXEPATH='C:\Program Files\Git'
cfg_mcp
install_hooks >/dev/null
assert_eq 'C:\Program Files\Git\usr\bin\bash.exe' \
  "$("$RIG_JQ" -r '.hooks.PreCompact[0].hooks[0].command' "$SETTINGS")" "EXEPATH-derived native bash.exe"
unset EXEPATH

# ------------------------------------------------------------- the flush itself
new_home nocygpath_flush
fake_uname_windows
record_nohup
cfg_mcp
install_hooks >/dev/null

step "a C:\\ transcript path still resolves through the pure-shell fallback"
WIN_DIR='C:\Users\First Last\winproj'
WIN_T='C:\Users\First Last\winproj\t.jsonl'
mkdir -p '/c/Users/First Last/winproj'
mk_transcript '/c/Users/First Last/winproj/t.jsonl' 13 nocyg
stub_reset
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$WIN_T" "$WIN_DIR" sess-nocyg PreCompact)"
assert_eq "0" "$RC" "hook exits 0"
if wait_stub 1 25; then pass "the flush submitted with no cygpath anywhere"; else fail "SILENT NO-OP: the C:\\ path never resolved"; fi
assert_eq "winproj" "$(stub_field '.tool=="kb_ingest_session"' '.project')" "cwd converted, project = winproj"
assert_eq "13" "$(stub_field '.tool=="kb_ingest_session"' '.transcript_lines')" "whole transcript arrived"
assert_contains "$(cat "$HOME/detach.calls")" "nohup" "still detached with nohup (no setsid on Git Bash)"

step "a lowercase drive letter and a forward-slash native path work too"
mkdir -p '/d/data/proj two' 2>/dev/null || true
if [ -d '/d/data/proj two' ]; then
  mk_transcript '/d/data/proj two/t.jsonl' 4 nocyg2
  stub_reset
  fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event 'd:/data/proj two/t.jsonl' 'd:/data/proj two' sess-nocyg2 PreCompact)"
  if wait_stub 1 25; then pass "d:/ forward-slash form resolved"; else fail "d:/ form did not resolve"; fi
  assert_eq "proj-two" "$(stub_field '.tool=="kb_ingest_session"' '.project')" "slug from the converted spacey dir"
else
  note "skipped: /d is not writable in this image"
fi

# -------------------------------------------------------- the helper as a unit
step "kb_winpath, extracted from the INSTALLED hook, converts correctly"
extract_block "$HOOKS/flush-session-to-kb.sh" "$HOME/block.sh"
if [ -s "$HOME/block.sh" ]; then pass "the shared portable block extracted cleanly"; else fail "no block found in the installed hook"; fi
# shellcheck disable=SC1090
. "$HOME/block.sh"
assert_eq "/c/Users/First Last/proj" "$(kb_winpath 'C:\Users\First Last\proj')" "C:\\ + space -> /c/..."
assert_eq "/c/Users/x" "$(kb_winpath 'C:/Users/x')" "forward-slash native form"
assert_eq "/d/tmp/a b" "$(kb_winpath 'D:\tmp\a b')" "uppercase drive letter is lowercased"
assert_eq "/d/tmp/a" "$(kb_winpath 'd:/tmp/a')" "already-lowercase drive letter"
assert_eq "/home/tester/x" "$(kb_winpath '/home/tester/x')" "a POSIX path passes through untouched"
assert_eq "" "$(kb_winpath '')" "empty input stays empty"
assert_eq "relative/path" "$(kb_winpath 'relative/path')" "a relative path passes through"
if kb_is_windows; then pass "kb_is_windows agrees with the faked uname"; else fail "kb_is_windows said no on MINGW"; fi
assert_eq "bash.exe" "$(kb_nativepath /usr/bin/bash)" "kb_nativepath falls back to the bare exe name"
assert_eq "nohup" "$(kb_detach_cmd)" "kb_detach_cmd picks nohup with no setsid"

finish

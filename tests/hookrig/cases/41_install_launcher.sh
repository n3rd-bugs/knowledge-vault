#!/usr/bin/env bash
# IMAGE: winsim
# CASE: WHICH bash.exe install.sh registers on Windows (wave C.1, KB 40411 fix
#       set item 6; install.sh:578 at HEAD b2b37a3).
# PINS: install.sh used to register `cygpath -w "$(command -v bash)"`, i.e. the
#       raw MSYS `<git root>\usr\bin\bash.exe`. Claude Code spawns that as a
#       NON-login shell on the bare WINDOWS PATH -- no /usr/bin, no /mingw64/bin
#       -- which is where every usama-home hook garbled its transcript path and
#       exited in silence (cases 40/42). Git's LAUNCHER, `<git root>\bin\bash.exe`,
#       sets MSYSTEM and puts the MSYS dirs on PATH before bash starts. So:
#         * the root is `cygpath -w /`, else Git Bash's $EXEPATH;
#         * `<root>\bin\bash.exe` is registered WHEN IT EXISTS (tested through
#           its drive form, /c/Program Files/Git/bin/bash.exe here);
#         * otherwise today's value, unchanged: cygpath -> `...\usr\bin\bash.exe`,
#           no cygpath + EXEPATH -> `$EXEPATH\usr\bin\bash.exe` (case 25);
#         * the launcher is registered for ALL FIVE events, the dispatcher stays
#           args[0], and a Linux box never registers a bash.exe at all.
# MODEL: the rig's fake_cygpath maps `/` to `C:\Program Files\Git\` and
#       `/usr/bin/bash` to `C:\Program Files\Git\usr\bin\bash.exe`, and its -u
#       maps C:\ -> /c/; the winsim image's /c is writable, so a fake launcher
#       at /c/Program Files/Git/bin/bash.exe IS "<git root>\bin\bash.exe".
#       /c is container-global: every no-launcher install runs BEFORE the fake
#       launcher is created.
# RED on today's hooks (HEAD b2b37a3) -- expected FAIL lines (cygpath and
#       EXEPATH variants alike, once per event):
#       "<ev> registers Git's launcher ...\bin\bash.exe -- got [C:\Program Files\Git\usr\bin\bash.exe]",
#       "the registered launcher is exactly C:\Program Files\Git\bin\bash.exe -- expected [...\bin\bash.exe], got [...\usr\bin\bash.exe]".
#       The no-launcher installs, args[0] and the Linux control pass on both.
. /rig/lib.sh

LAUNCH_DIR='/c/Program Files/Git/bin'
LAUNCH_POSIX="$LAUNCH_DIR/bash.exe"
LAUNCH_WIN='C:\Program Files\Git\bin\bash.exe'
RAW_WIN='C:\Program Files\Git\usr\bin\bash.exe'
EVENTS="PreCompact SessionEnd SessionStart UserPromptSubmit Stop"
cmd_of() { "$RIG_JQ" -r --arg e "$1" '.hooks[$e][0].hooks[0].command // ""' "$SETTINGS"; }
arg0_of() { "$RIG_JQ" -r --arg e "$1" '.hooks[$e][0].hooks[0].args[0] // ""' "$SETTINGS"; }
expect_launcher() { # <label>
  local ev c
  for ev in $EVENTS; do
    c=$(cmd_of "$ev")
    case "$c" in
      *'\usr\bin\bash.exe') fail "$ev registers Git's launcher ...\\bin\\bash.exe ($1) -- got [$c]" ;;
      *'\bin\bash.exe') pass "$ev registers Git's launcher ...\\bin\\bash.exe ($1)" ;;
      *) fail "$ev registers Git's launcher ...\\bin\\bash.exe ($1) -- got [$c]" ;;
    esac
    assert_eq "$HOOKS/kb-hook-dispatch.sh" "$(arg0_of "$ev")" "$ev args[0] is still the dispatcher"
  done
  assert_eq "$LAUNCH_WIN" "$(cmd_of PreCompact)" "the registered launcher is exactly $LAUNCH_WIN ($1)"
}

step "precondition: no launcher exists yet"
assert_file_missing "$LAUNCH_POSIX" "no $LAUNCH_POSIX before the fake is created"

# ------------------------------------------- A. cygpath, no launcher -> today
step "cygpath present, NO <root>\\bin\\bash.exe: today's raw usr\\bin\\bash.exe"
new_home launcher_cyg_none
fake_uname_windows
fake_cygpath
cfg_mcp
install_hooks >/dev/null
for ev in $EVENTS; do
  assert_eq "$RAW_WIN" "$(cmd_of "$ev")" "$ev keeps today's value without a launcher"
done

# ----------------------------------- B. no cygpath, EXEPATH, no launcher -> today
step "no cygpath, EXEPATH set, NO launcher: today's EXEPATH-derived usr\\bin\\bash.exe"
new_home launcher_exe_none
fake_uname_windows
export EXEPATH='C:\Program Files\Git'
cfg_mcp
install_hooks >/dev/null
assert_eq "$RAW_WIN" "$(cmd_of PreCompact)" "EXEPATH without a launcher keeps today's value"
unset EXEPATH

# ------------------------------------------------------ the fake Git launcher
mkdir -p "$LAUNCH_DIR"
printf '#!/bin/sh\n# rig stand-in for Git for Windows bin\\bash.exe\nexit 0\n' >"$LAUNCH_POSIX"
chmod +x "$LAUNCH_POSIX"
if [ -x "$LAUNCH_POSIX" ]; then pass "fake launcher created at $LAUNCH_POSIX"; else fail "could not create $LAUNCH_POSIX"; fi

# -------------------------------------------- C. cygpath + launcher -> launcher
step "cygpath present AND <root>\\bin\\bash.exe exists: Git's launcher is registered"
new_home launcher_cyg
fake_uname_windows
fake_cygpath
cfg_mcp
install_hooks >/dev/null
expect_launcher "root from cygpath -w /"

# --------------------------------- D. no cygpath, EXEPATH + launcher -> launcher
step "no cygpath, EXEPATH set AND the launcher exists: Git's launcher is registered"
new_home launcher_exe
fake_uname_windows
export EXEPATH='C:\Program Files\Git'
cfg_mcp
install_hooks >/dev/null
expect_launcher "root from EXEPATH"
unset EXEPATH

# -------------------------------------------------- E. Linux control
step "CONTROL: a Linux box never registers a bash.exe, launcher or not"
new_home launcher_linux
cfg_mcp
install_hooks >/dev/null
assert_eq "$HOOKS/kb-hook-dispatch.sh PreCompact" "$(cmd_of PreCompact)" "Linux keeps the POSIX dispatcher command"

rm -rf '/c/Program Files'
finish

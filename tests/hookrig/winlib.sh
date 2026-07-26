#!/usr/bin/env bash
# winlib.sh — the Windows lane's thin shim over the SHARED lib.sh.
#
# The point of the Windows lane is that it runs the SAME assertions as the
# container lane against a REAL Git Bash, so this file must add environment
# only — never a second copy of any helper. Anything asserted here that is not
# genuinely Windows-specific belongs in lib.sh instead.
#
# What is genuinely different on a real box:
#   * PATH must keep the inherited Windows entries, because jq/python/curl are
#     .exe files resolved through them. The container's fixed PATH would hide
#     the very tools whose real-Windows behaviour we came here to test.
#   * HOME lives under the sandbox in %TEMP%, never the real user profile.
#   * The rig's own JSON/HTTP tools are the box's real ones.

# Keep the inherited PATH as the base so new_home() layers $HOME/bin on top of
# a PATH where jq.exe / python.exe / curl.exe still resolve.
export RIG_BASEPATH="${RIG_BASEPATH:-$PATH}"

# The rig's own tools (never the code under test's discovery path).
export RIG_JQ="${RIG_JQ:-$(command -v jq || echo jq)}"
export RIG_PY="${RIG_PY:-$(command -v python || command -v python3 || echo python)}"
export RIG_CURL="${RIG_CURL:-$(command -v curl || echo curl)}"

. "$RIG_LIB"

# ---------------------------------------------------------------- assertions
# Windows-only helpers. Everything else comes from lib.sh.

assert_win() { # label -- we really are on Git Bash, not a simulation
  case "$(uname -s)" in
    MINGW* | MSYS* | CYGWIN*) pass "${1:-running on real Git Bash ($(uname -s))}" ;;
    *) fail "${1:-not Git Bash}: uname -s = $(uname -s)" ;;
  esac
}

# The native Windows form of the sandbox, for building C:\-style fixtures.
win_path() { # posix -> native, via the box's own cygpath
  cygpath -w "$1" 2>/dev/null || printf '%s' "$1"
}

# Source the portable block from a skill script into THIS shell so its helper
# functions (kb_timeout, kb_json_probe, kb_winpath, ...) can be called directly.
load_portable_block() { # [script-name]
  local src="${1:-flush-session-to-kb.sh}"
  extract_block "$SKILL/$src" "$HOME/.block.sh"
  # shellcheck disable=SC1090
  . "$HOME/.block.sh"
}

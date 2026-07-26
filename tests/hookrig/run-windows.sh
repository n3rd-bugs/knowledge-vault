#!/usr/bin/env bash
# run-windows.sh — the hook rig's REAL-Windows lane, driven over WinRM.
#
# `make hooktest` proves the hooks in a Linux container, including a `winsim`
# variant that FAKES Windows. This lane runs the same skill, and the same
# assertion library, on an actual Windows box with actual Git Bash — the only
# place where the genuine System32 timeout.exe, real cygpath/EXEPATH, real
# .exe JSON tools and real CRLF risk exist.
#
# It is NOT part of `make hooktest`: it needs a reachable Windows host and
# credentials, so it is opt-in like the connector lane.
#
# Usage:
#   bash tests/hookrig/run-windows.sh              # all windows cases
#   bash tests/hookrig/run-windows.sh timeout json # only matching cases
#   bash tests/hookrig/run-windows.sh --keep       # leave the remote sandbox
#
# Credentials come from the repo-root `.windows` (gitignored):
#   WIN_IP=...  WIN_USER=...  WIN_PASSWORD=...
# They are never printed, never passed on a remote command line, and the run
# aborts if the host's real %USERPROFILE%\.claude changes (host tripwire).
set -euo pipefail

RIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$RIG_DIR/../.." && pwd)"

red() { printf '\033[31m%s\033[0m\n' "$*"; }

[ -f "$REPO_DIR/.windows" ] || {
  red "no $REPO_DIR/.windows — the Windows lane needs WIN_IP/WIN_USER/WIN_PASSWORD"
  exit 2
}

# pywinrm lives in the system python here, not the uv venv (the driver is a test
# harness, not shipped code, so it stays out of the project's dependency set).
PY="${KB_WINRIG_PYTHON:-python3}"
"$PY" -c 'import winrm' 2>/dev/null || {
  red "pywinrm is not available to $PY"
  echo "install it with:  sudo apt install python3-winrm   (or: pipx/venv + pip install pywinrm)"
  exit 2
}

exec "$PY" -u "$RIG_DIR/win_driver.py" "$@"

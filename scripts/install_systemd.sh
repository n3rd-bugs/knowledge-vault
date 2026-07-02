#!/usr/bin/env bash
# Install/refresh the Knowledge Vault systemd user units (PLAN.md §9).
# Phase 2 ships kb-mcp; kb-worker and kb-consolidate join in Phases 3/4.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
mkdir -p "${UNIT_DIR}"

install -m 644 "${REPO_DIR}"/systemd/kb-mcp.service "${UNIT_DIR}/"

systemctl --user daemon-reload
systemctl --user enable --now kb-mcp.service
# refresh semantics: a unit that was already running restarts onto new code/config
systemctl --user try-restart kb-mcp.service || true

# Keep user services alive after logout (required once per machine).
loginctl enable-linger "${USER}" || true

systemctl --user --no-pager status kb-mcp.service || true
echo "installed: kb-mcp.service (logs: journalctl --user -u kb-mcp -f)"

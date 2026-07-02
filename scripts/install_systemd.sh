#!/usr/bin/env bash
# Install/refresh the Knowledge Vault systemd user units (PLAN.md §9).
# Phase 2 ships kb-mcp; Phase 3 adds kb-worker; Phase 4 adds kb-consolidate.{service,timer}.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
mkdir -p "${UNIT_DIR}"

install -m 644 "${REPO_DIR}"/systemd/kb-mcp.service "${UNIT_DIR}/"
install -m 644 "${REPO_DIR}"/systemd/kb-worker.service "${UNIT_DIR}/"
install -m 644 "${REPO_DIR}"/systemd/kb-consolidate.service "${UNIT_DIR}/"
install -m 644 "${REPO_DIR}"/systemd/kb-consolidate.timer "${UNIT_DIR}/"

systemctl --user daemon-reload
systemctl --user enable --now kb-mcp.service
systemctl --user enable --now kb-worker.service
# The consolidation is timer-driven: enable the TIMER (not the oneshot service).
systemctl --user enable --now kb-consolidate.timer
# refresh semantics: a unit that was already running restarts onto new code/config
systemctl --user try-restart kb-mcp.service || true
systemctl --user try-restart kb-worker.service || true

# Keep user services alive after logout (required once per machine).
loginctl enable-linger "${USER}" || true

systemctl --user --no-pager status kb-mcp.service || true
systemctl --user --no-pager status kb-worker.service || true
systemctl --user --no-pager list-timers kb-consolidate.timer || true
echo "installed: kb-mcp.service, kb-worker.service, kb-consolidate.{service,timer}"
echo "logs: journalctl --user -u kb-mcp -f | -u kb-worker -f | -u kb-consolidate -f"

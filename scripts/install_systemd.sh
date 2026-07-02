#!/usr/bin/env bash
# Install/refresh the Knowledge Vault systemd user units (PLAN.md §9).
# kb-mcp (Ph2), kb-worker (Ph3), kb-consolidate.{service,timer} (Ph4),
# kb-restore-test.{service,timer} (Ph5, installed but NOT auto-enabled).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
mkdir -p "${UNIT_DIR}"

install -m 644 "${REPO_DIR}"/systemd/kb-mcp.service "${UNIT_DIR}/"
install -m 644 "${REPO_DIR}"/systemd/kb-worker.service "${UNIT_DIR}/"
install -m 644 "${REPO_DIR}"/systemd/kb-consolidate.service "${UNIT_DIR}/"
install -m 644 "${REPO_DIR}"/systemd/kb-consolidate.timer "${UNIT_DIR}/"
install -m 644 "${REPO_DIR}"/systemd/kb-restore-test.service "${UNIT_DIR}/"
install -m 644 "${REPO_DIR}"/systemd/kb-restore-test.timer "${UNIT_DIR}/"

systemctl --user daemon-reload
systemctl --user enable --now kb-mcp.service
systemctl --user enable --now kb-worker.service
# The consolidation is timer-driven: enable the TIMER (not the oneshot service).
systemctl --user enable --now kb-consolidate.timer
# kb-restore-test is intentionally left DISABLED (it spins a throwaway container);
# enable manually: systemctl --user enable --now kb-restore-test.timer
# refresh semantics: a unit that was already running restarts onto new code/config
systemctl --user try-restart kb-mcp.service || true
systemctl --user try-restart kb-worker.service || true

# Keep user services alive after logout (required once per machine).
loginctl enable-linger "${USER}" || true

systemctl --user --no-pager status kb-mcp.service || true
systemctl --user --no-pager status kb-worker.service || true
systemctl --user --no-pager list-timers kb-consolidate.timer || true
echo "installed: kb-mcp, kb-worker, kb-consolidate.{service,timer}, kb-restore-test.{service,timer}"
echo "  (kb-restore-test.timer installed but not enabled — enable manually if wanted)"
echo "logs: journalctl --user -u kb-mcp -f | -u kb-worker -f | -u kb-consolidate -f"

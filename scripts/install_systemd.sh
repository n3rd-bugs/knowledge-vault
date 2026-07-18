#!/usr/bin/env bash
# Install/refresh the Knowledge Vault systemd user units (PLAN.md §9).
# kb-mcp (Ph2), kb-worker (Ph3), kb-consolidate.{service,timer} (Ph4),
# kb-restore-test.{service,timer} + kb-cloudflared-refresh.{service,timer}
# + kb-backup.{service,timer} (§6d) (installed but NOT auto-enabled).
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
install -m 644 "${REPO_DIR}"/systemd/kb-cloudflared-refresh.service "${UNIT_DIR}/"
install -m 644 "${REPO_DIR}"/systemd/kb-cloudflared-refresh.timer "${UNIT_DIR}/"
install -m 644 "${REPO_DIR}"/systemd/kb-backup.service "${UNIT_DIR}/"
install -m 644 "${REPO_DIR}"/systemd/kb-backup.timer "${UNIT_DIR}/"

systemctl --user daemon-reload
systemctl --user enable --now kb-mcp.service
systemctl --user enable --now kb-worker.service
# The consolidation is timer-driven: enable the TIMER (not the oneshot service).
systemctl --user enable --now kb-consolidate.timer
# kb-restore-test is intentionally left DISABLED (it spins a throwaway container);
# enable manually: systemctl --user enable --now kb-restore-test.timer
# kb-cloudflared-refresh is also left DISABLED (it re-pulls + recreates the public
# edge); enable manually: systemctl --user enable --now kb-cloudflared-refresh.timer
# kb-backup is also left DISABLED until the owner reviews the schedule (and, for
# offsite, configures rclone + age in .env); enable manually:
#   systemctl --user enable --now kb-backup.timer
# refresh semantics: a unit that was already running restarts onto new code/config
systemctl --user try-restart kb-mcp.service || true
systemctl --user try-restart kb-worker.service || true

# Keep user services alive after logout (required once per machine).
loginctl enable-linger "${USER}" || true

systemctl --user --no-pager status kb-mcp.service || true
systemctl --user --no-pager status kb-worker.service || true
systemctl --user --no-pager list-timers kb-consolidate.timer || true
echo "installed: kb-mcp, kb-worker, kb-consolidate.{service,timer}, kb-restore-test.{service,timer}, kb-cloudflared-refresh.{service,timer}, kb-backup.{service,timer}"
echo "  (kb-restore-test.timer + kb-cloudflared-refresh.timer + kb-backup.timer installed but not enabled — enable manually if wanted)"
echo "logs: journalctl --user -u kb-mcp -f | -u kb-worker -f | -u kb-consolidate -f"

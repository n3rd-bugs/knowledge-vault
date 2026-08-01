#!/usr/bin/env bash
# Install/refresh the Knowledge Vault systemd user units (PLAN.md §9).
# kb-mcp (Ph2), kb-worker (Ph3), kb-consolidate.{service,timer} (Ph4),
# kb-restore-test.{service,timer} + kb-cloudflared-refresh.{service,timer}
# + kb-backup.{service,timer} (§6d) + kb-consolidate-boot.timer (catch-up)
# + kb-notify-fail@.service (the OnFailure notifier every unit hands off to).
#
# EVERY unit file in systemd/ is installed by the loop below — deliberately, not
# by an enumerated list. A hand-maintained list is how kb-consolidate-boot.timer
# came to exist only on the live machine, one `install_systemd.sh` away from
# being deleted (tests/test_unit_files.py asserts the loop covers them all).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
mkdir -p "${UNIT_DIR}"

for unit in "${REPO_DIR}"/systemd/*.service "${REPO_DIR}"/systemd/*.timer; do
  [[ -e "${unit}" ]] || continue
  install -m 644 "${unit}" "${UNIT_DIR}/"
done
# The failure notifier is executed from the repo (like scripts/backup.sh), so it
# only needs to be executable — but a lost +x turns every failure alert into a
# second failure, so assert it here rather than trusting the checkout.
chmod 755 "${REPO_DIR}/systemd/kb-notify-fail.sh"

# --- tiktoken cache: persistent, and warm before the first run ----------------
# tiktoken's default cache lives under /tmp (tmpfs), so every boot wipes it and
# the first chunker call re-downloads the BPE. A post-power-cut catch-up did that
# with no network yet and hard-failed. kb-consolidate.service pins
# TIKTOKEN_CACHE_DIR here; seed it from the vendored copy so the first run after
# an install never needs the network either.
TIKTOKEN_DIR="${HOME}/kb/cache/tiktoken"
mkdir -p "${TIKTOKEN_DIR}"
if compgen -G "${REPO_DIR}/vendor/tiktoken/*" > /dev/null; then
  cp -n "${REPO_DIR}"/vendor/tiktoken/* "${TIKTOKEN_DIR}/" 2>/dev/null || true
fi

systemctl --user daemon-reload
systemctl --user enable --now kb-mcp.service
systemctl --user enable --now kb-worker.service
# The consolidation is timer-driven: enable the TIMER (not the oneshot service).
systemctl --user enable --now kb-consolidate.timer
# Catch-up after an unclean shutdown: Persistent=true only covers a window that
# was MISSED, not a run that started and was killed 32 minutes in (2026-07-30).
# Enabled WITHOUT --now on purpose: OnBootSec=10min is already in the past on a
# box that has been up for a day, so `--now` would start a 4.5–7h consolidation
# the moment someone runs this installer. It arms itself at the next boot, which
# is the only moment it is meant to fire.
systemctl --user enable kb-consolidate-boot.timer
# kb-backup is now ENABLED by default. It used to be left off "until the owner
# reviews the schedule", which in practice meant the only backup was
# kb-consolidate's ExecStartPost — conditional on a 4.5–7h run completing, so an
# interrupted night produced nothing at all (29 + 30 July have no dump). The
# local dump needs no configuration; the offsite leg stays opt-in via .env.
systemctl --user enable --now kb-backup.timer
# kb-restore-test is intentionally left DISABLED (it spins a throwaway container);
# enable manually: systemctl --user enable --now kb-restore-test.timer
# kb-cloudflared-refresh is also left DISABLED (it re-pulls + recreates the public
# edge); enable manually: systemctl --user enable --now kb-cloudflared-refresh.timer
# kb-notify-fail@ is a TEMPLATE pulled in by OnFailure= — never enabled directly.
# refresh semantics: a unit that was already running restarts onto new code/config
systemctl --user try-restart kb-mcp.service || true
systemctl --user try-restart kb-worker.service || true

# Keep user services alive after logout (required once per machine).
loginctl enable-linger "${USER}" || true

systemctl --user --no-pager status kb-mcp.service || true
systemctl --user --no-pager status kb-worker.service || true
systemctl --user --no-pager list-timers 'kb-*' || true
echo "installed: every unit in ${REPO_DIR}/systemd (services + timers)"
echo "  enabled: kb-mcp, kb-worker, kb-consolidate.timer, kb-consolidate-boot.timer, kb-backup.timer"
echo "  installed but not enabled: kb-restore-test.timer, kb-cloudflared-refresh.timer"
echo "  kb-notify-fail@.service is a template invoked via OnFailure= (never enabled directly)"
echo "  tiktoken cache: ${TIKTOKEN_DIR} (pre-warmed from vendor/tiktoken)"
echo "logs: journalctl --user -u kb-mcp -f | -u kb-worker -f | -u kb-consolidate -f"

.PHONY: up down migrate deploy test hooktest hooktest-win lint eval mcp worker consolidate backup restore-test seed reembed sync-assets help

help:
	@echo "targets: up down migrate deploy test hooktest hooktest-win lint eval mcp worker consolidate backup restore-test seed reembed sync-assets"

up:
	docker compose --env-file .env up -d
	@echo "waiting for kb-db to be healthy..."
	@for i in $$(seq 1 40); do \
	  s=$$(docker inspect -f '{{.State.Health.Status}}' kb-db 2>/dev/null || echo none); \
	  if [ "$$s" = healthy ]; then echo "kb-db healthy"; exit 0; fi; \
	  sleep 1; \
	done; echo "kb-db did not become healthy in time"; exit 1

down:
	docker compose --env-file .env down

migrate:
	uv run python -m kbcore.migrate

test:
	uv run pytest

# Hermetic containerised rig for the Claude Code session hooks (tests/hookrig).
# Talks only to a stub it starts on an --internal docker network; never touches
# the live KB, the live DB, or the host's ~/.claude. No `uv`/DB needed.
# Build + restart the KB services. Uses the LEGACY builder deliberately.
#
# This host runs AdGuard Home on *:53, so /etc/resolv.conf is `nameserver
# 127.0.0.1` (KB item 859, sungrow). BuildKit copies that verbatim into the
# build container's network namespace, where nothing is listening, and every
# image pull dies with `lookup ghcr.io on 127.0.0.1: i/o timeout`. The legacy
# builder works because Docker substitutes a public resolver for a loopback
# nameserver — the same fallback KB 859 documents for normal containers.
#
# The permanent fix is an explicit `"dns"` in /etc/docker/daemon.json (point it
# at the LAN address AdGuard listens on, so builds keep ad-blocking), but that
# needs a dockerd restart, which bounces every container on this box.
deploy:
	docker pull ghcr.io/astral-sh/uv:latest
	DOCKER_BUILDKIT=0 docker compose build kb-mcp kb-rest
	docker compose up -d kb-mcp kb-mcp-public kb-rest
	@echo "deployed — verify: curl -o /dev/null -w '%{http_code}\\n' https://api.<host>/api/stats  (expect 401)"

hooktest:
	bash tests/hookrig/run.sh

# The REAL-Windows lane: the same skill and the same assertion library, run on
# an actual Windows box over WinRM (Git Bash, genuine System32 timeout.exe, real
# cygpath/EXEPATH, .exe JSON tools, CRLF risk). Opt-in: needs a reachable host
# and a gitignored .windows with WIN_IP/WIN_USER/WIN_PASSWORD. Sandboxed under
# the remote TEMP dir with a tripwire on the host's real ~/.claude.
hooktest-win:
	bash tests/hookrig/run-windows.sh

lint:
	uv run ruff check --fix .
	uv run ruff format .

eval:
	uv run python eval/run_eval.py

mcp:
	uv run python -m kbmcp.server

worker:
	uv run python -m kbworker.watcher

consolidate:
	uv run python -m kbworker.consolidate

seed:
	uv run kb seed

reembed:
	uv run kb reembed

# Re-sync the shipped hook assets from their canonical home. contrib/ is the
# source of truth; src/kbmcp/setup_assets/ is package data the container ships.
# The test_setup_assets_match_contrib drift test is the tripwire.
sync-assets:
	cp contrib/claude/kb-session-flush/* src/kbmcp/setup_assets/

backup:
	bash scripts/backup.sh

restore-test:
	bash scripts/restore_test.sh

.PHONY: up down migrate test hooktest lint eval mcp worker consolidate backup restore-test seed reembed sync-assets help

help:
	@echo "targets: up down migrate test hooktest lint eval mcp worker consolidate backup restore-test seed reembed sync-assets"

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
hooktest:
	bash tests/hookrig/run.sh

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

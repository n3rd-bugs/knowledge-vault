# CLAUDE.md — Knowledge Vault repo conventions

You are implementing the system specified in `PLAN.md` (v2.1 — primary use case: multidomain software development). `ADVERSARIAL_REVIEW.md` (Rounds 1–3) explains why contested decisions were settled the way they were — **read it before proposing design changes; do not re-litigate settled verdicts (V1–V8) or violate the nine invariants in PLAN.md §1.**

## Workflow
- Execute exactly one phase per session, in order. Never start phase N+1 while phase N acceptance criteria are red.
- End every phase by running its AC and pasting raw command output. No "should work" — show it working.
- Commit per phase: `phase-N: <summary>`. Small, reviewable diffs. Ask before any destructive operation (drop table, delete data, overwrite `.env`).
- If PLAN.md is ambiguous, choose the simplest interpretation consistent with the invariants and note the choice in the commit message.

## Stack & commands
- Python 3.11+, `uv` for env/deps (`uv sync`, `uv run ...`). Lint: `ruff check --fix` + `ruff format`. Tests: `pytest -q`.
- `make up | migrate | test | lint | eval | mcp | worker | consolidate | backup | restore-test | seed | reembed` — keep these working at all times.
- DB access only through `kbcore.db` (psycopg3, transactions). No ORM. Parameterized SQL only — never f-string values into queries.
- Config only via `kbcore.config` (pydantic-settings reading `.env`). No hardcoded paths, ports, model names, or secrets anywhere.

## Hard rules (mirror of PLAN.md invariants)
1. `items.body` is immutable after insert. Corrections go through `supersede()`. Consolidation creates new `summary` items only.
2. All writes via `kbcore` service functions, each in a transaction. MCP/worker/CLI call kbcore, never SQL.
3. Ingest must be idempotent — prove it with a test whenever you touch the ingest path.
4. Automatic extractions: `trust='unverified'`, quote-grounded (verbatim-substring validated) or rejected.
5. Automation never deletes rows; it flips `status`. Migrations after 001 are additive only.
6. Network: bind `KB_BIND` only; bearer check on every MCP request; never disable auth "temporarily".
7. `TIMESTAMPTZ`/UTC everywhere. `datetime.now(timezone.utc)`, never naive datetimes.
8. GPU courtesy: embed calls set short keep-alive; the 14B extractor is invoked only from `consolidate.py` or explicit user-invoked runs (`kb ingest-session`, `kb deep-search`). Residency: `KB_LLM_KEEP_ALIVE=-1` keeps it loaded (owner decision 2026-07 — idle residency is power-cheap; set 0 to reclaim VRAM).
9. **The KB is not a code index.** Never bulk-ingest source trees; the repo scanner reads docs/ADR/README files only. Knowledge about code carries a `code_ref` anchor ({repo, path, lines, commit, symbol}) instead of the code itself.

## Code style
- Type hints on all public functions; `pydantic` models in `kbcore/models.py` for tool/CLI I/O shapes.
- Errors: raise domain exceptions in kbcore; MCP layer converts to clean tool errors (message + no stack traces to clients). Log full traces to journald/stderr.
- Every new behavior lands with at least one test. Prefer real-DB tests against the compose Postgres (test schema `kb_test`, created/dropped by fixture) over mocks; mock only Ollama.
- Keep files under ~300 lines; split modules rather than growing gods.

## Secrets & data hygiene
- Never commit `.env`, dumps, `~/kb/raw` contents, or anything under `backups/`. Keep `.gitignore` current.
- Never print `KB_TOKEN` or `KB_DB_PASSWORD` in logs or AC output — show `***`.
- When writing tests/seeds, use fake tokens shaped like real ones (e.g. `ghp_` + zeros) to exercise the scrubber.

## Using the KB while building it (dogfood, from Phase 2 on)
- Before non-trivial changes: `kb_search` for prior decisions in project `kb-system` — and globally when debugging ("have we hit this anywhere?").
- After each phase: `kb_save(project="kb-system", type="decision", ...)` for every non-obvious choice, `type="gotcha"` for pitfalls discovered — always with namespaced tags (`lang:python`, `tool:systemd`, `protocol:http`, …) and a `code_ref` pointing at the relevant file/lines.
- If a phase reverses an earlier choice, use `kb_supersede` with a reason — never save a silently contradicting note. Link fixes to bugs with the `fixes` relation.

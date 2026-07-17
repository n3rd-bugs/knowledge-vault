# KB improvements for development + Claude integration

*Synthesized 2026-07-17 from a three-finder / three-judge adversarial review of the repo at `/home/usama/data-server` (Knowledge Vault, PLAN.md v2.1). All file:line citations verified against the working tree at commit `0999e29`. Note: the finder catalog reached the judges truncated mid-item [8]; all three judges scored items [0]–[8] only, and item [8]'s full intent was reconstructed from judge summaries and re-verified against the code.*

## 1. Framing

The KB's job is to make coding agents measurably better: surface the exact prior decision, gotcha, or bug→fix pair before an agent re-derives it or repeats it. The foundations are genuinely strong — immutable typed items with append-only supersession, hybrid RRF search with scope walls, a typed relation graph, hardened session-flush/pre-brief hooks, and a nightly eval sentinel. The leverage is concentrated in three places: (a) **ranking arithmetic** — the flat +0.05 bonuses are ~1.5x the maximum possible raw RRF score, so the documented "always pass `boost_project`" pattern categorically buries cross-project answers, which is the KB's core promise; (b) **the automatic Claude touchpoints** — the flush and recall hooks are the highest-frequency paths in and out of the KB, yet the flush never passes git context (so auto-captured items get no `code_ref`) and the recall path drops ranking affordances depending on transport; (c) **the evidence loop** — the eval is 25 substring cases that never exercise `boost_project`, `expand`, or `as_of`, so the two most consequential ranking knobs are changed blind. Every improvement below is additive, standalone, and local-LLM-only.

## 2. Top improvements — ranked

Judge consensus was unusually tight: all three ranked the boost rescale #1 (9/9/9) and keyword-leg relaxation #2 (8.3/8.5/8.7). Disagreements resolved explicitly: **Judge 2 placed graph-expansion-default and the anchor line (8 each) above the flush-hook git context (7.5)**; Judges 1 and 3 put git context third (8/8.5). Majority plus an effort tiebreak (the git-context fix is shell-only, and the MCP tool already accepts the parameters) settles git context at #3. **[1] eval growth vs [3] expansion default** was a near-tie on averages (7.83 vs 7.63); eval growth ranks higher because it is the measurement substrate every other change needs to prove itself. Items 6, 8, 11, 13 were surfaced by judges as coverage gaps, not scored by finders; they are slotted by judge emphasis (the relevance floor was independently headlined by two of three judges).

| Rank | Improvement | Area | Value/Effort | One-line why |
|---|---|---|---|---|
| 1 | Rank-preserving `boost_project` + `EXACT_ID_BONUS` rescale | both | high/low | Flat +0.05 exceeds max raw RRF (~0.0328, admitted at `search.py:247-250`); the recommended usage pattern buries cross-project rank-1 answers |
| 2 | Keyword-leg OR-fallback + identifier tokens as a third candidate leg | dev | high/medium | Pasted errors and NL queries AND-fail the unstemmed FTS leg; identifier bonus can't rescue items that never enter the pool |
| 3 | Flush hook passes git repo/branch/commit; recall hook transport parity | claude | high/low | `kb_ingest_session` already accepts repo/branch/commit; the hooks just don't send them — auto-captured items get no `code_ref`, and MCP-mode recall silently drops `boost_project` |
| 4 | Grow the golden eval: `expect_item_id`/`forbid_item_id`/per-case kwargs + access_log mining | dev | high/medium | Substring-in-any-top-5 can stay green while the useful item ranks nowhere; `boost_project`/`expand`/`as_of` are entirely untested |
| 5 | Graph expansion ON by default (1 hop, optionally `fixes`/`supersedes` only) | both | medium/low | Bug→fix ride-along — the headline dev scenario — is fully built, hardened, and inert (`KB_GRAPH_EXPAND_HOPS=0`) |
| 6 | Relevance floor / "KB doesn't know" abstention marker *(judge addition)* | both | high/medium | `search()` always returns k plausible hits and the recall hook injects them with zero score threshold — context pollution as unverified corpus grows |
| 7 | Synthetic anchor line: make `code_ref` path/symbol + tags searchable text | dev | medium/medium | Invariant 9's anchor is retrieval-invisible; a query naming the file or symbol can't match it |
| 8 | Session capture includes Edit/Bash tool summaries *(judge addition)* | claude | medium/medium | `_message_text` drops all tool_use/tool_result blocks (`kbworker/sessions.py:104-110`) — the actual diff/command/error never reaches the 14B extractor |
| 9 | Query-aware, breadcrumb-free snippets (`ts_headline`, strip chunk breadcrumb) | dev | medium/medium | Up to a third of the 240-char snippet restates the title; the actionable sentence is often deeper in the chunk |
| 10 | Capped usage prior from access_log (search-then-`kb_get` evidence) | both | medium/medium | Real usage signal exists (`search.py:364-373`) but sizing must stay below a rank-1 RRF increment (learn from #1) |
| 11 | `kb_feedback` / `used_item_id` lightweight signal *(judge addition)* | both | medium/medium | Snippets often suffice, so `kb_get`-after-search under-labels; the strongest eval/training signal is structurally uncollectable today |
| 12 | Heuristic query-type router (bare-symbol / error-paste / NL leg weighting) | dev | low/medium | Real but mostly subsumed by #2; revisit only if eval shows residual query-class failures |
| 13 | `code_ref` drift doctor (re-resolve path/lines/commit after refactors) *(judge addition)* | dev | low/medium | Anchors rot silently; worth a doctor check once #3 makes anchors ubiquitous |

## 3. Top 5 in detail

### 3.1 Rank-preserving `boost_project` and `EXACT_ID_BONUS` — serves BOTH themes

**Current state.** RRF fusion scores are `sum(1/(60+rank))` per leg (`src/kbcore/search.py:25` `RRF_K=60`, `:230`), so the maximum possible raw score is `2/61 ≈ 0.0328` (rank-1 in both legs). `_apply_bonuses` then adds a flat `BOOST = 0.05` to every candidate from the boosted project (`search.py:27`, `:261-266`) and a flat `EXACT_ID_BONUS = 0.05` per matched identifier token, up to `MAX_ID_TOKENS = 8` (`search.py:36`, `:41`, `:267-281` — i.e. up to +0.40 stacked). The code's own comment concedes the problem: bonuses "each exceed the max raw RRF (~0.0328)" (`search.py:247-250`) — the clamp built on that observation protects only expansion neighbors, not direct hits. Meanwhile every agent-facing surface tells callers to always pass `boost_project`: the `kb_search` docstring promises local work "floats without hiding other projects'" answers (`src/kbmcp/server.py:301-302`), the `brief_me` prompt instructs it (`src/kbmcp/prompts.py:53-55`), and the LOCAL-mode recall hook hardcodes `--boost-project "$PROJECT"` (`src/kbmcp/setup_assets/kb-session-brief.sh:143-144`).

**Gap.** A rank-40 tail hit from the boosted project (RRF ~0.01 + 0.05 = 0.06) outranks a rank-1 cross-project exact answer (0.0328) — categorically, every time. The KB's stated point ("cross-domain reuse is the point", `search.py` module intent) is arithmetically defeated by its own recommended calling convention. Same scale defect for identifier stacking: 8 token matches yield +0.40, ~12x the max raw signal.

**Proposal.** Make both bonuses rank-preserving: multiplicative (`score *= 1.25` for boosted project) or additive but sub-rank-1-sized (e.g. `+0.008`, about half a rank-1 single-leg increment `1/61 - 1/62`). Cap `EXACT_ID_BONUS` total stacking at ~1 rank-1 increment. Ties still break toward the local project; strong-vs-weak never flips.

**First step.** Change the two constants and the application arithmetic in `_apply_bonuses` (`search.py:236-289`); re-verify the expansion clamp at `:282-288` still holds (it clamps relative to post-bonus seed scores, so it survives a rescale, but assert it in a test).

**Acceptance criteria.**
- New golden cases (needs WP-D's per-case kwargs, or a minimal inline pytest first): with `boost_project="projA"` set, a foreign project's exact-match item still ranks above a projA near-miss; a projA tie still wins.
- `make eval` hit@5/MRR do not regress on the existing 25 cases.
- A unit test asserting `BOOST_effect < 1/(RRF_K+1)` (or the multiplicative equivalent) so the scale bug cannot silently return.

### 3.2 Keyword-leg relaxation + identifier candidate generation — serves DEVELOPMENT

**Current state.** The keyword leg is `websearch_to_tsquery('simple', query)` — all terms ANDed, nothing stemmed (`src/kbcore/search.py:196-210`; `migrations/001_init.sql:53` generates `tsv` with `'simple'`). The module header admits one extra common word ("drv_spi_write gotcha") "AND-fails the keyword leg" (`search.py:32-33`). `EXACT_ID_BONUS` was the patch, but it only re-scores ids already in the fused pool (`search.py:267-281` operates on `ids = list(scores)`).

**Gap.** The canonical mid-task agent queries — a pasted multi-line error string, or "systemd service keeps restarting how to debug" — AND-fail (any non-matching token kills the leg) and get no stemming ("restarting" never matches "restart"). Retrieval then rides the vector leg alone, where a 300-token error dump dilutes the embedding. If the target item also misses the vector top-40 (`CANDIDATES=40`, `search.py:26`), the identifier bonus cannot rescue it: there is no identifier-driven candidate *generation*.

**Proposal.** Two additive steps, same scope predicate, no schema change: (a) inside `_keyword_leg`, when the AND tsquery returns fewer than ~5 rows, rerun with an OR tsquery over the query's tokens (still `ts_rank_cd`-ordered, same `WHERE` wall); (b) promote `_identifier_tokens` from post-hoc bonus to a third RRF leg — a word-boundary regex match (the same `\m..\M` pattern, `search.py:270`) over chunks for up to `MAX_ID_TOKENS`, feeding `_rrf_fuse` alongside the vector and keyword legs.

**First step.** Add the OR-fallback in `_keyword_leg` behind a row-count check, plus a golden case with a verbatim pasted error string and one with a stemming-sensitive phrasing; measure before/after.

**Acceptance criteria.**
- A golden case whose query is a multi-line pasted error hits its target item in top-5; same for a morphological-variant phrasing.
- Bare-symbol lookup where the symbol appears only in one item's body ranks that item top-3 even when the vector leg misses it.
- `make eval` non-regression on all prior cases; the OR-fallback fires only when AND returns <5 rows (unit-tested).

### 3.3 Git context into auto-capture + recall transport parity — serves CLAUDE INTEGRATION

**Current state.** The plumbing already exists end to end *except the last inch*: `kb_ingest_session` accepts `repo`, `branch`, `commit` (`src/kbmcp/tools_phase5.py:123-131` — docstring: "Pass repo/branch/commit to anchor the knowledge to code"), and `kbworker/sessions.py` stamps a `code_ref` from them (`_code_ref`, `sessions.py:169-180`; threaded at `:385`) plus `meta["branch"]` (`:267-268`). But the flush hook builds its JSON args from project + transcript only (`src/kbmcp/setup_assets/flush-session-to-kb.sh:139-163`, all four `jq -n` variants: `--arg p "$PROJECT" --rawfile t ...`). Separately, the recall/brief hook's MCP transport sends only `{query, k}` (`kb-session-brief.sh:152`) while the LOCAL transport passes `--boost-project "$PROJECT"` (`:143-144`); the prompt-as-query is cut to 240 chars (`:109`), decapitating pasted errors.

**Gap.** Every auto-captured session item — the KB's highest-volume ingestion path — lands with no `code_ref`, so session-derived bug/fix knowledge can't anchor to code (Invariant 9's whole design), and entity derivation gets no repo. And recall quality silently differs by transport: MCP-connected machines never get the project boost, so the identical prompt returns different rankings depending on install mode — uninstrumented and eval-invisible.

**Proposal.** Shell-only: in `flush-session-to-kb.sh`, derive `REPO=$(git -C "$CWD" remote get-url origin 2>/dev/null || basename "$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null)")`, `BRANCH=$(git -C "$CWD" rev-parse --abbrev-ref HEAD)`, `COMMIT=$(git -C "$CWD" rev-parse HEAD)` and add them to all four `jq -n` arg builders (and the LOCAL CLI invocation). In `kb-session-brief.sh:152`, add `boost_project: $PROJECT` to the MCP `ARGS` (the `kb_search` tool accepts it, `server.py:291`). Optionally raise the query cap at `:109` for error pastes once 3.2 lands.

**First step.** Patch the two setup-asset scripts *and their installed copies' source of truth* (also mirror `contrib/claude/kb-session-flush/kb-session-brief.sh`), then re-run the skill's install/smoke test.

**Acceptance criteria.**
- A flushed test session produces queue rows whose eventual items carry `meta.code_ref` with repo + commit (verify via `kb_get` / review queue in `kb_test`).
- MCP-mode and LOCAL-mode recall produce the same ranking for the same prompt in the same CWD.
- Both hooks stay within their registered budgets (recall 15s / brief 30s, per `kb-session-brief.sh:85-88`); git calls are `2>/dev/null`-guarded so a non-git CWD degrades to today's behavior.

### 3.4 Eval that can catch ranking regressions — serves DEVELOPMENT (underpins 3.1, 3.2, 3.5)

**Current state.** `eval/golden.yaml` holds 25 hand-written cases; a case passes if ANY `expect_substr` appears anywhere in title+snippet+FULL BODY of ANY top-5 hit (`src/kbcore/eval_harness.py:44-59`; K=5 at `:24`). `evaluate()` calls `search(query, k=K, tool=None)` only (`:53`) — never `boost_project`, `expand`, or `as_of`. Meanwhile `access_log` records every real search per hit (`search.py:364-373`; schema `migrations/001_init.sql:87-91`) and every explicit `kb_get` fetch — a natural implicit-relevance label nobody consumes. Nothing measures precision of what the recall hook injects (Judge 2's gap: recall-only metrics, `eval_harness.py:61-63`).

**Gap.** The eval can stay green while the actually-useful item ranks nowhere ("modbus" merely mentioned in any top-5 body passes). No expected-item-id assertions, no negative cases (a superseded decision must NOT surface), no `as_of` cases, and the two most consequential knobs — `boost_project` and `expand` — are exercised zero times, so 3.1 and 3.5 would otherwise ship unmeasured.

**Proposal.** (1) Extend the golden schema additively: optional `expect_item_id`, `expect_rank_le`, `forbid_item_id`, and per-case `kwargs` (`boost_project`, `expand`, `as_of`) — `evaluate()` is a 30-line loop. Keep `expect_substr` working. (2) Add `kb eval mine`: join `access_log` search rows to subsequent `kb_get` rows within a short window, emit candidate `(query, item_id)` golden cases for owner approval. (3) Log a per-run count of recall-hook injections vs. subsequent same-session `kb_get`s of injected items as a crude precision proxy. All local-only, no LLM.

**First step.** Add `expect_item_id` + per-case kwargs support to `eval_harness.evaluate()` (backward compatible), then convert the identifier-heavy cases to id-based expectations and add the 3.1/3.5 golden cases that need kwargs.

**Acceptance criteria.**
- `make eval` runs old-format and new-format cases side by side; a deliberately mis-ranked fixture case fails.
- At least one `forbid_item_id` case (superseded item must not appear top-5) and one `as_of` case pass.
- `kb eval mine` emits at least one plausible candidate pair from a seeded `kb_test` access_log without writing to golden.yaml itself.

### 3.5 Graph expansion on by default — serves BOTH themes

**Current state.** WP2 expansion is fully implemented and hardened — seed-relative floor, per-level scope walls, decay clamp strictly below the vouching seed (`src/kbcore/search_expand.py:47` `expand_fused`, `src/kbcore/graph.py:188` `expand_items_scored`, clamp re-asserted post-bonus at `search.py:282-288`) — but shipped OFF: `kb_graph_expand_hops: int = 0` (`src/kbcore/config.py:96`, mirrored in `.env.example`). Only an explicit `expand=true` on `kb_search` activates it (`server.py:294`, `:305-308`). The `fixes` relation exists for exactly the ride-along scenario (`migrations/001_init.sql:76-78`, DATA-10).

**Gap.** The headline scenario — agent searches an error, hits the BUG item, and the FIX item linked via `rel='fixes'` rides along — does not happen on a plain `kb_search`. The claude.ai connector and both hooks issue plain calls, so the relation graph the system works hard to populate contributes nothing to default retrieval. The eval never runs with expansion on, so there's no evidence loop either way.

**Proposal.** Ship `KB_GRAPH_EXPAND_HOPS=1` as default (worst case, given the clamps, is a few extra low-ranked rows). Optionally restrict default expansion to high-signal relations (`fixes`, `supersedes`) by threading a rel filter into `_neighbors_batch` (`graph.py:127`). Pair with 3.4's golden cases so the flip is measured, not vibes.

**First step.** Flip the default in `config.py:96` + `.env.example`, add 2 bug→fix ride-along golden cases, compare `make eval` before/after.

**Acceptance criteria.**
- Golden case: query matching a bug item surfaces its `fixes`-linked fix item in top-5 with no `expand` param.
- No regression on the existing 25 cases; expanded neighbors always rank below their vouching seed (existing invariant test extended to run with the new default).
- p95 search latency delta measured and acceptable (expansion adds one scored BFS per search).

## 4. Quick wins (low-effort, do first)

- **Rescale `BOOST` in `src/kbcore/search.py:27` + `_apply_bonuses`** — the one-constant core of 3.1; the golden proof can follow with WP-D.
- **Add `boost_project` to the MCP recall args** — one `jq` change at `kb-session-brief.sh:152` restores transport parity (mirror into `contrib/claude/kb-session-flush/`).
- **Pass `repo`/`branch`/`commit` in `flush-session-to-kb.sh`** — the tool (`tools_phase5.py:123-131`) and worker (`sessions.py:169-180`) already accept and use them; only the `jq -n` builders at `:139-163` need the three `--arg`s.
- **Flip `KB_GRAPH_EXPAND_HOPS=1`** (`config.py:96` + `.env.example`) once the two ride-along golden cases exist.
- **Strip the leading breadcrumb from snippets** — chunks start with `"{title} › {heading}"` (`chunk.py:112-113`) and `render_hit` prints the title on the previous line then truncates to 240 chars (`render.py:32`, `:46-47`); dropping a prefix that equals the breadcrumb reclaims up to a third of the snippet budget for actual content.

## 5. Sub-agent execution guide

Four work packages. Each prompt below is fully self-contained (assumes no prior conversation, survives a session/model switch). Common guardrails baked into each: code + tests only; all DB activity via the pytest fixtures' `kb_test` schema, never the live `public` schema; no `git commit`, no migrations, no docker/systemctl, no deploys, never read or print `.env`/`KB_TOKEN`/`KB_DB_PASSWORD` — the main agent reviews, commits, and deploys.

### WP-A — Rank-preserving search bonuses

```
You are working in /home/usama/data-server, a Python 3.12 + uv + Postgres/pgvector
knowledge-base repo (read CLAUDE.md first; obey its invariants). TASK: make the
search-ranking bonuses rank-preserving instead of categorical.

Context: src/kbcore/search.py fuses a vector and a keyword leg with RRF
(RRF_K=60, line 25; max possible raw score = 2/61 ≈ 0.0328). _apply_bonuses
(lines 236-289) then adds a flat BOOST=0.05 (line 27) per boosted-project
candidate and a flat EXACT_ID_BONUS=0.05 (line 36) per matched identifier token
(up to MAX_ID_TOKENS=8, i.e. +0.40). The comment at lines 247-250 admits each
bonus exceeds the max raw RRF — so any boosted-project tail hit outranks every
cross-project rank-1 hit. That defeats the tool's contract (src/kbmcp/server.py
lines 301-302 promises local work "floats without hiding other projects'").

Do:
1. Rework _apply_bonuses so the project boost is multiplicative (score *= ~1.25)
   or additive but < 1/(RRF_K+1); cap total EXACT_ID_BONUS stacking at ~one
   rank-1 RRF increment. Keep the expansion-neighbor clamp (lines 282-288)
   correct — it must still guarantee a pure neighbor ranks strictly below its
   vouching seed AFTER the new arithmetic.
2. Add pytest tests (real-DB style, kb_test schema via existing fixtures in
   tests/; mock only Ollama embeds as the existing tests do): (a) with
   boost_project set, a foreign-project item at raw rank 1 still beats a
   boosted-project item at raw rank ~10; (b) an exact tie breaks toward the
   boosted project; (c) a guard test asserting the boost effect on a rank-1
   score is smaller than one rank-1 RRF increment; (d) the neighbor clamp holds.
3. Update the stale comment block at search.py:243-253 and the module docstring
   line 6 ("+0.05 boost_project bonus") to describe the new scheme.

Guardrails: code+tests only; kb_test schema only (never public); no git commit,
no migrations, no docker/systemctl, no .env reads, never print secrets. Run:
uv run pytest -q tests/ (search-related tests at minimum), uv run ruff check
--fix && uv run ruff format, and `make eval` if the environment allows — paste
raw output. Return: a summary of the new arithmetic, files changed with line
refs, and full test/eval output. Do NOT commit — the main agent reviews.
```

### WP-B — Keyword-leg OR-fallback + identifier candidate leg

```
You are working in /home/usama/data-server, a Python 3.12 + uv + Postgres/pgvector
knowledge-base repo (read CLAUDE.md first; obey its invariants). TASK: stop
agent-typical queries (pasted error strings, natural-language questions) from
zeroing out keyword retrieval, and let identifier tokens GENERATE candidates.

Context: src/kbcore/search.py _keyword_leg (lines 196-210) uses
websearch_to_tsquery('simple', query): all terms ANDed, unstemmed (tsv is
'simple', migrations/001_init.sql:53). One non-matching token kills the whole
leg (module docstring, lines 32-33). The identifier bonus in _apply_bonuses
(lines 267-281) only re-scores ids already retrieved by the two legs — items
outside the vector top-40 (CANDIDATES=40, line 26) are unreachable.
_identifier_tokens and the \m..\M word-boundary pattern (line 270) exist.

Do:
1. In _keyword_leg: if the AND tsquery returns < 5 rows, rerun with an OR
   tsquery over the query's tokens (same scope WHERE wall, same
   ts_rank_cd ordering, same CANDIDATES limit). Parameterized SQL only.
2. Add a third candidate leg: for up to MAX_ID_TOKENS identifier tokens from
   the query, word-boundary-match chunks (reuse the \m..\M pattern) under the
   same scope predicate, rank by match count then recency, and feed the results
   into _rrf_fuse as an additional leg (extend _rrf_fuse to accept N legs).
3. Tests (kb_test schema fixtures, mock Ollama embeds): (a) OR-fallback fires
   only when AND yields <5 rows; (b) a multi-line pasted error string retrieves
   its target item; (c) a bare symbol present only in one item's body surfaces
   that item even when the mocked vector leg omits it; (d) scope walls hold for
   the new leg (a private-project item never leaks).
4. Keep search.py under control per CLAUDE.md (~300-line guidance) — split a
   helper module if needed.

Guardrails: code+tests only; kb_test schema only; no git commit, no migrations,
no docker/systemctl, no .env reads, never print secrets. Run uv run pytest -q,
ruff check --fix + ruff format, and `make eval` if available — paste raw
output. Return: summary, changed files with line refs, test/eval output. Do
NOT commit — the main agent reviews.
```

### WP-C — Hook git context + transport parity (shell only)

```
You are working in /home/usama/data-server, a knowledge-base repo whose Claude
Code integration ships two POSIX-shell hooks in src/kbmcp/setup_assets/ (also
mirrored at contrib/claude/kb-session-flush/). TASK: thread git context into
session capture and fix a transport inconsistency in recall. Shell changes
only — no Python, no DB.

Context A: flush-session-to-kb.sh submits session transcripts via the
kb_ingest_session MCP tool / local CLI, but its jq arg builders (lines 139-163)
pass only project + transcript. The tool already accepts repo/branch/commit
(src/kbmcp/tools_phase5.py:123-131) and kbworker/sessions.py stamps a code_ref
from them (_code_ref, lines 169-180) — the context is simply never sent.
Context B: kb-session-brief.sh LOCAL mode passes --boost-project "$PROJECT"
(lines 143-144) but MCP mode builds ARGS as only {query, k} (line 152), so
recall ranking silently differs by transport.

Do:
1. In flush-session-to-kb.sh: derive, guarded with 2>/dev/null fallbacks so a
   non-git CWD degrades silently: REPO (origin URL, else toplevel basename),
   BRANCH (rev-parse --abbrev-ref HEAD), COMMIT (rev-parse HEAD). Add them to
   ALL transport variants (each jq -n builder and any local CLI invocation).
   Empty values must be omitted from the JSON, not sent as "".
2. In kb-session-brief.sh line 152: add boost_project: $PROJECT to the MCP
   ARGS (kb_search accepts it — src/kbmcp/server.py:291).
3. Mirror both edits into contrib/claude/kb-session-flush/ copies. Check
   src/kbmcp/setup_assets/install.sh for any checksum/manifest that must match.
4. Verify with shellcheck (or bash -n if unavailable) and a dry-run: feed each
   script a synthetic hook-input JSON on stdin with KB_* env unset so every
   network path is skipped, and show the constructed ARGS via a temporary
   set -x or echo (remove before finishing). Confirm budgets: the git calls are
   local and must not add meaningful latency (hook budgets: recall 15s,
   brief 30s — see kb-session-brief.sh lines 85-88).

Guardrails: shell files only; no git commit, no installs into ~/.claude, no
network calls, no .env reads, never print KB_TOKEN. Return: summary, exact
diff hunks, and the dry-run output showing the new args JSON. Do NOT commit —
the main agent reviews and reinstalls hooks.
```

### WP-D — Eval v2: id-based assertions, kwargs cases, access_log mining

```
You are working in /home/usama/data-server, a Python 3.12 + uv + Postgres/pgvector
knowledge-base repo (read CLAUDE.md first). TASK: make the retrieval eval able
to catch ranking regressions, and mine real usage into candidate golden cases.

Context: eval/golden.yaml has 25 cases; src/kbcore/eval_harness.py evaluate()
(lines 36-64) marks a case a hit if ANY expect_substr appears in
title+snippet+FULL BODY of ANY top-5 result, and always calls
search(query, k=K, tool=None) — never boost_project/expand/as_of, so those
knobs are untested. access_log (migrations/001_init.sql:87-91) already stores
(item_id, tool, query, ts) for every search hit (search.py:364-373) and every
kb_get. The harness is shared by make eval (eval/run_eval.py) and the nightly
sentinel (src/kbworker/eval_sentinel.py) — keep both callers working.

Do:
1. Extend the golden schema ADDITIVELY in eval_harness.py: optional
   expect_item_id, expect_rank_le, forbid_item_id, and a per-case kwargs dict
   (boost_project, expand, as_of) forwarded to search(). expect_substr keeps
   working unchanged; keep tool=None (eval reads are not usage — see the
   comment at lines 48-52, do not regress it).
2. A case with forbid_item_id FAILS if that id appears in top-K. Report which
   criterion matched/failed per case in the returned results structure without
   breaking run_eval.py or eval_sentinel.py output handling (update both).
3. Add a CLI command `kb eval mine` (src/kbcli/main.py, follow the style of
   search_cmd at line 139): join access_log rows where tool LIKE '%kb_search%'
   to later kb_get rows for the same item within a configurable window
   (default 15 min), emit candidate YAML case stubs to stdout for owner
   approval. Read-only SQL via kbcore.db; parameterized only. It must NOT
   write to eval/golden.yaml.
4. Tests (kb_test schema fixtures, mock Ollama): seeded items + access_log
   rows proving (a) expect_item_id pass/fail, (b) forbid_item_id fail,
   (c) kwargs forwarding (boost_project reaches search), (d) mine emits the
   expected (query, item_id) candidate and ignores un-followed searches.
5. Add 4-6 new golden.yaml cases exercising the new fields against items the
   existing seed data provides (inspect eval/golden.yaml and make seed's
   fixtures first; if live-only items are needed, provide the cases in a
   separate proposed file eval/golden.proposed.yaml instead and say so).

Guardrails: code+tests only; kb_test schema only (never public — a prior
sub-agent truncated live tables; do not repeat this); no git commit, no
migrations, no docker/systemctl, no .env reads, never print secrets. Run
uv run pytest -q and ruff check --fix + ruff format; paste raw output.
Return: summary, changed files with line refs, test output, and the sample
`kb eval mine` output from the test fixture. Do NOT commit.
```

## 6. Open questions for the owner

1. **Boost semantics**: multiplicative (`*1.25`, scale-free) or additive sub-increment (`+0.008`, easier to reason about vs RRF)? WP-A defaults to multiplicative unless you object.
2. **Expansion default scope**: full 1-hop over all relations, or restricted to `fixes`/`supersedes` at default (with `expand=true` unlocking everything)? Restricted is safer for snippet real estate but needs the rel-filter threading into `graph.py:_neighbors_batch`.
3. **Relevance floor placement** (rank 6): enforce abstention in `kbcore.search` (affects every caller) or only in the recall hook's rendering (safer, hook-local)? And is a calibrated threshold worth a small labeled pass over access_log first?
4. **Tool-block capture** (rank 8): including Edit/Bash summaries in `assemble_transcript` grows extractor input and secret-scrub surface — acceptable for the 14B's context budget, and should tool_result stderr be capped per block?
5. **Recall query cap**: after WP-B lands, raise the 240-char prompt cut (`kb-session-brief.sh:109`) so pasted errors survive — to what (1-2 KB)? It rides inside a 15s hook budget.
6. **Feedback signal** (rank 11): is a `used_item_id` optional param on `kb_search`/`kb_get` (zero new tools) acceptable to Claude-side ergonomics, or would you rather a distinct `kb_feedback` tool the prompts instruct agents to call after a hit resolves their task?
7. **Eval case provenance**: should `kb eval mine` candidates auto-expire (queries drift as projects evolve), or is golden.yaml owner-curated forever?

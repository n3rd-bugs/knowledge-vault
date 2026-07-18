# Adversarial Review — Knowledge Vault Plan

**Method:** Three rounds by five specialist agents. Round 1: independent attack on the v1 design. Round 2: cross-examination of contested calls, moderated verdicts. Round 3: delta review after the primary use case was narrowed to **multidomain software development**. Every finding below has a resolution already incorporated into `PLAN.md` (v2.1). Residual risks that cannot be engineered away are listed at the end.

**Panel:**

| Agent | Role | Attack surface |
|---|---|---|
| A1 | Systems Architect | Coherence, coupling, interfaces, migration paths |
| A2 | Security / Red-team | Exposure, auth, data poisoning, secrets, client/NDA data |
| A3 | Data & Retrieval (ML) | Schema, chunking, embeddings, ranking, eval, multilingual |
| A4 | SRE / Ops | Backups, concurrency, GPU contention, failure modes, growth |
| A5 | Pragmatist | Scope, YAGNI, time-to-first-value, maintenance burden |

---

## Round 1 — Findings

Severity: **H** = would cause data loss, security incident, or dead project. **M** = degrades quality/trust over months. **L** = polish.

| ID | Sev | Agent | Finding (attack on v1) | Resolution in v2 |
|---|---|---|---|---|
| SEC-1 | H | A2 | v1 bound the MCP HTTP server on `0.0.0.0` with no auth. Anyone on the LAN (or worse, if the router forwards) can read/write the entire knowledge base, including client project details. | Bind `127.0.0.1` (or Tailscale interface only). Static bearer token required on every request, checked in middleware. Remote access = Tailscale or SSH tunnel. Public HTTPS exposure is explicitly out of scope until Phase 6 (§10). |
| SEC-2 | H | A2 | Session transcripts contain API keys, Upwork client credentials, `.env` contents pasted during debugging. v1 ingested transcripts verbatim → secrets become permanently searchable and get echoed into future model contexts. | `scrub.py` runs on every ingest: regex patterns for AWS/GitHub/OpenAI/Anthropic keys, private key blocks, `password=`, JWTs. Matches are replaced with `[REDACTED:type]` and the item is flagged `meta.redactions>0`. (§5.3) |
| SEC-3 | M | A2 | Memory poisoning: an auto-extracted "decision" (hallucinated or from a malicious pasted document) gets stored, then retrieved and treated as ground truth by future sessions — the KB becomes a persistent prompt-injection vector. | Three defenses: (1) trust tiers — `manual` saves are `verified`, all automatic extraction is `unverified` and visibly badged in search results; (2) extraction must be quote-grounded — the LLM must return a verbatim source quote, validated as a substring, or the item is rejected; (3) MCP search results are wrapped with a provenance header instructing the consumer to treat content as data, not instructions. (§5.4, §6.2) |
| SEC-4 | M | A2 | Backups of client/NDA material sat world-readable in `~/kb/backups`. | Backup script sets `umask 077`, output `chmod 600`. Optional `age` encryption hook for offsite copies. (§9) |
| SEC-5 | M | A2 | v1 promised "connect from claude.ai web." Web custom connectors require a public HTTPS endpoint — that means exposing the KB to the internet, which contradicts SEC-1. | Scoped: primary clients are Claude Code + Claude Desktop (both reach the server over Tailscale/loopback). Public exposure via Cloudflare Tunnel + OAuth is a documented Phase 6 option, not default. (§10) |
| DATA-1 | H | A3 | No dedupe. The session watcher re-parses growing `.jsonl` files; the inbox re-fires on file touch. Within a week the KB is 60% duplicates and ranking is garbage. | `sources` has `UNIQUE(kind, uri, content_hash)`; ingest is idempotent upsert. Session parser checkpoints byte offsets per transcript file. Chunk-level `text_hash` blocks re-embedding identical text. (§4, §7.2) |
| DATA-2 | H | A3 | Append-only forever. Decisions get reversed (you switched a regulator, changed an exam format) but v1 had no way to mark knowledge wrong — the KB confidently serves stale answers, which is worse than no KB. | First-class supersession: `kb_supersede(old_id, ...)` links old→new, sets `status='superseded'`. Search defaults to `status='active'`; `include_superseded=true` recovers history. This is also the answer to "recollect old data": nothing is deleted, it's demoted and traceable. (§6.1) |
| DATA-3 | M | A3 | Embedding model is a schema-level commitment v1 didn't treat as one. Swap models later → mixed vector spaces → silent retrieval corruption. | `embed_model` stored per chunk. Search filters to the current model's vectors. `kb reembed` command queues re-embedding when the configured model changes. (§4, §7.4) |
| DATA-4 | M | A3 | Fixed 500-token chunking splits code fences and tables mid-structure — exactly the "intricate details" this system exists to preserve. | Structure-aware chunker: split on markdown headings first, keep code fences and tables atomic (oversize allowed), then pack paragraphs to 300–500 tokens with 15% overlap. (§5.2) |
| DATA-5 | M | A3 | No retrieval eval. "Refine over time" is meaningless if you can't measure whether a change to chunking/model/ranking helped or hurt. | `eval/golden.yaml` (real queries + expected-content substrings) + `make eval` reporting hit@5 and MRR. Gate: hit@5 ≥ 0.8 before Phase 5 sign-off. Seeded with 10 queries in Phase 1, grown during use. (§8) |
| DATA-6 | M | A3 | Postgres `to_tsvector('english')` stems English; your Urdu materials (Imla, Tafheem docs) become FTS-invisible, and keyword search silently fails for half your content. | FTS uses the `'simple'` config (language-neutral tokenizer); semantic recall for Urdu is carried by BGE-M3, which is explicitly multilingual (this drove the embedder choice — see Verdict V3). (§4) |
| DATA-7 | L | A3 | Mixed local timestamps → wrong timelines. | All timestamps `TIMESTAMPTZ`, stored UTC, rendered local only at display. |
| ARCH-1 | M | A1/A4 | v1 used SQLite. Three processes write (MCP server, inbox watcher, nightly consolidator); WAL single-writer + busy timeouts is workable but fragile, and FTS5+sqlite-vec hybrid is DIY. | **Verdict V1 (below): Postgres 16 + pgvector via docker compose.** |
| ARCH-2 | M | A1 | LLM entity/decision extraction will hallucinate; v1 trusted it. | Merged into SEC-3 resolution: quote-grounding + unverified tier + `kb review` CLI to promote/reject. |
| ARCH-3 | M | A1 | v1's consolidation *rewrote* items when merging/summarizing. An LLM edit-in-place destroys the ground truth it summarizes. | **Invariant #1: raw items are immutable.** Consolidation only creates new `type='summary'` items linked `derived_from` → sources, and flips status fields. Never edits `body` of ingested items. (§7.3) |
| ARCH-4 | L | A1 | "Any AI model can use it" — MCP covers Claude/Cursor/most agents in 2026, but not everything. | MCP is the v1 interface; a thin REST facade over the same `kbcore` service layer is Phase 6. Core library is transport-agnostic by construction. |
| ARCH-5 | M | A1 | Concurrent writes: consolidator merging a cluster while the MCP server supersedes an item in it → torn state. | All writes go through `kbcore` in transactions; consolidation takes `pg_advisory_lock(42)` for its run; supersede/link are single-transaction. (§7.3) |
| OPS-1 | H | A4 | The 4080 already runs your daytime LLM workloads. v1 loaded a 14B extractor resident → VRAM contention kills both. | Embeddings (BGE-M3, ~1–2 GB) load on demand with `OLLAMA_KEEP_ALIVE=2m`. The 14B extractor runs **only** in the nightly consolidation window and unloads after. `KB_EMBED_DEVICE=cpu` env escape hatch. (§3, §7.3) |
| OPS-2 | H | A4 | Untested backups are decorative. | `scripts/restore_test.sh`: restores latest dump into a throwaway container, asserts row counts, monthly via timer. Restore drill is a Phase 5 acceptance criterion. (§9) |
| OPS-3 | M | A4 | v1 ran processes in tmux. | systemd user units with `Restart=on-failure`, `MemoryMax`, journald logging; timer for consolidation. (§9) |
| OPS-4 | L | A4 | Unbounded raw-transcript retention fills the disk quietly. | Raw originals stored zstd-compressed under `~/kb/raw/`; weekly disk watermark check in worker logs a warning >80%. |
| PRAG-1 | H | A5 | v1 built graph + consolidation + watcher + UI before first use. Classic never-ships. | Re-phased: **Phases 0–2 = usable end-to-end (save + hybrid search over MCP) in one working day.** Graph/trace, consolidation, eval hardening follow. UI/REST demoted to optional Phase 6. |
| PRAG-2 | M | A5 | v1 hand-rolled a FastAPI embedding microservice. | Deleted. Ollama's `/api/embed` is the embedder. One fewer service. |
| PRAG-3 | L | A5 | Vague paths/naming. | Fixed conventions: everything under `~/kb/`, repo at `~/kb/repo`, DB port `127.0.0.1:5433`, MCP port `8321`. |

---

## Round 2 — Contested calls and verdicts

**V1 — SQLite vs Postgres** (A5 argued SQLite: zero-ops, file backup, scale is tiny). A4 countered: three writer processes, and the consolidator does multi-statement merges where advisory locks and real transactions matter; pgvector+tsvector hybrid is battle-tested vs a DIY FTS5+sqlite-vec fusion. Compose makes Postgres a one-liner and `pg_dump` is as scriptable as file copy. **Verdict: Postgres + pgvector.** A5's concern honored by capping ops surface: one container, one volume, no tuning.

**V2 — Knowledge graph now or later?** A1 wanted entities/relations from day one ("trace design decisions" is the stated goal). A5: entity extraction is the least reliable component; shipping it first poisons trust in the whole system. **Verdict: schema for entities/relations lands in migration 001 (cheap, avoids a migration later), but automatic extraction and `kb_trace` ship in Phase 5, after search is proven.** Manual `kb_link` is available from Phase 2 so real relations accumulate early.

**V3 — Embedding model.** Candidates: `nomic-embed-text` (fast, English-leaning), `mxbai-embed-large`, `bge-m3`. A3's deciding argument: the corpus is bilingual (English + Urdu teaching materials, plus code). BGE-M3 is trained multilingual, 1024-dim, runs trivially on the 4080, and Ollama serves it directly. **Verdict: `bge-m3`,** with DATA-3's re-embed machinery making this reversible.

**V4 — Trust of automatic chat extraction.** A2 wanted a hard review gate: nothing auto-ingested is searchable until human-approved. A5: a gate you must clear daily is a gate you abandon in week two, and then the collector is dead weight. **Verdict: store immediately as `unverified` (searchable, badged, ranked slightly lower); `kb review` promotes/rejects in batches whenever convenient.** Quote-grounding (SEC-3) keeps the unverified tier honest.

**V5 — How the "collector agent" attaches to chat.** A1 proposed a resident daemon tailing all transcripts. A2: too invasive across client projects (NDA risk); A4: fragile parsing of a moving format. **Verdict: two consented paths only** — (a) the model itself calls `kb_save` during sessions (instructed via `CLAUDE.md`), and (b) a Claude Code `Stop`/`SessionEnd` hook runs `kb ingest-session <transcript>` **only in projects where the hook is installed** (opt-in per project). No global surveillance of `~/.claude`.

---

## Round 3 — v2.1 revision review (software-development pivot)

Trigger: primary use case narrowed to **multidomain software development** (embedded firmware, web/tooling, devops, ML workflows). Panel re-convened on the deltas only; Rounds 1–2 resolutions stand unchanged.

| ID | Sev | Agent | Finding (attack on the pivot) | Resolution in v2.1 |
|---|---|---|---|---|
| ARCH-6 | H | A1/A5 | A dev-focused KB invites "just index the whole repo." That duplicates what git/grep/LSP/Claude Code already do, drowns retrieval in boilerplate, and balloons the DB — the KB's value is curated knowledge, not a worse code search. | **Invariant 9:** the KB stores knowledge *about* code — decisions, gotchas, snippets, specs, runbooks — anchored to code coordinates. The repo scanner (§7.5) ingests git-tracked docs/ADR/README/CHANGELOG only, with a test asserting zero source files ingested (Phase 3 AC). |
| DATA-8 | M | A3 | A flat `project` string can't answer the highest-value multidomain query: "every SPI/I2C/regulator gotcha across *all* projects." Cross-project reuse was the point and v2 couldn't express it. | `projects` registry with `domain` + repo mapping; **namespaced tags** (`lang:`, `hw:`, `protocol:`, `framework:`, `tool:`, `os:`); `kb_search` gains `domain`/`tags` filters, defaults to global scope with a `boost_project` bonus so current work floats without hiding other projects. |
| DATA-9 | M | A3/A1 | Dev knowledge detached from code coordinates rots: "the fix" without *where it landed* is a riddle six months later. | `code_ref` anchors (`{repo, path, lines, commit, symbol}`) on any item, rendered in every result; the session hook auto-captures repo/branch/commit at Stop time; reposcan stamps commit on ingested docs. |
| DATA-10 | L | A3 | Bug↔fix pairing had no first-class semantics; "have I seen this before?" needed graph help, not just similarity. | Relations gain `fixes` and `alternative_to`; `kb_similar(item_id)` added (Phase 5) — AC requires the seeded bug's fix in top-3. |
| PRAG-4 | L | A5 | Mandating formal ADR structure would kill the capture habit within a week. | Soft template only: `kb adr` CLI scaffolds Context/Decision/Alternatives/Consequences; `kb_save` docstring nudges; schema stays free-text. |
| OPS-5 | L | A4 | Reposcan re-ingesting on every doc edit could churn duplicates daily. | Content-hash dedupe (DATA-1) makes unchanged files no-ops; edited docs land as new items and nightly dedupe/timeline handles the evolution — consistent with Invariant 1. |

**Round 3 verdicts:**

**V6 — The KB is not a code index.** Unanimous; promoted to Invariant 9 rather than a guideline, because it is the scope decision most likely to be violated casually mid-build.

**V7 — Code-specific embedder?** A3 floated code-tuned models (e.g., nomic-embed-code) for snippet recall. Verdict: **stay on `bge-m3`** — the corpus is dominated by natural language *about* code, and the multilingual requirement (Urdu materials) still binds. The golden set now carries code/identifier queries, so the eval — not vibes — triggers any revisit; DATA-3's per-chunk `embed_model` + re-embed queue makes a swap cheap. Note: the `'simple'` FTS config chosen for Urdu (DATA-6) also happens to preserve identifiers like `TPS55288` un-stemmed — the keyword leg carries exact-symbol lookups regardless of embedder.

**V8 — Tree-sitter chunking for source files?** Deferred. Under Invariant 9, raw source arrives only as curated snippets via inbox; a heuristic top-level-definition splitter suffices. Tree-sitter is listed in Phase 6, gated on eval evidence of code-chunk misses.

---

## Residual risks (accepted, monitored)

1. **Bearer token is static.** Acceptable single-user on a private network; rotate via `.env` + restart. Revisit if ever exposed publicly (Phase 6 requires OAuth).
2. **Quote-grounding limits but doesn't eliminate extraction error** — a real quote can still be mis-titled. Mitigated by the unverified tier + periodic review; measured by rejection rate in consolidation reports.
3. **Ranking quality is corpus-dependent.** RRF weights and archive-decay knobs will need tuning; the eval harness exists precisely so tuning is measurable, not vibes.
4. **Single node, single disk.** Nightly dumps + optional encrypted offsite (rclone) is the recovery story; HA is deliberately out of scope.

## Sign-off checklist (all ✅ in PLAN.md v2.1)

- [x] Scope boundary enforced: repo scanner cannot ingest source trees (tested)
- [x] Cross-domain retrieval expressible (domain filter + namespaced tags) and demonstrated in AC
- [x] Knowledge → code traceability via `code_ref` anchors, auto-captured by the session hook

- [x] No unauthenticated network surface
- [x] Idempotent ingest; duplicate-safe under re-runs
- [x] Knowledge can be corrected (supersede) without loss (immutable raw)
- [x] Multilingual retrieval path (Urdu) verified in Phase 1 AC
- [x] GPU usage bounded and off-peak
- [x] Backups exist **and restore is tested**
- [x] First usable milestone ≤ 1 day of Claude Code sessions
- [x] Every phase has machine-checkable acceptance criteria

---

## Amendment A11-1 — Invariant 11 carve-out: owner-only authz routes (2026-07-18)

**Decision (owner):** Invariant 11 is amended from "the web UI is read-only by construction" to
"the web UI performs **no knowledge mutation**". The ONLY mutating web routes permitted are the
OWNER-ONLY authorization-config actions `POST /admin/grant` / `POST /admin/revoke`, which call
`kbcore.principals.grant()`/`ungrant()` with `rw=false` hardwired.

**Why this does not weaken what Inv. 11 protects.** Inv. 11 exists to guarantee that a web
session — the most exposed, most phishable surface — can never alter *knowledge* (items, sources,
relations, review state, immutability/supersession semantics). The carve-out touches only
`principal_grants`, an authorization-config table: no item row is ever written from kbweb, and the
knowledge-write import test still covers every kbweb module. A **grantee** session gains nothing —
non-owners receive the byte-identical 404 an unknown path yields (no admin-existence oracle) and
never see an admin link. A hijacked **owner** cookie gains no authority it did not already have:
the owner's `/setup` page already renders the master bearer, which transitively allows every REST
write — so owner-session compromise was already total, and the new routes add CSRF-token +
same-origin defenses that `/setup` never needed. Scope is frozen: share grant/revoke only —
principal lifecycle (create/revoke/rotate) stays CLI-only, and any future mutating web route
re-opens this review.

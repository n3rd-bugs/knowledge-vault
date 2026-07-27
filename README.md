# Knowledge Vault

A local-first, self-refining knowledge base for multidomain software work. It captures decisions,
gotchas, snippets, specs, runbooks, and bug/fix pairs; serves recall over **MCP + REST + a web UI**
with hybrid semantic+keyword search; runs entirely on **local models** (Ollama); refines itself
nightly (dedupe, summaries, contradiction detection with evidence weighing, auto-resolution); and
**never loses history** — corrections supersede, nothing is deleted. Design and rationale live in
[PLAN.md](PLAN.md) and [ADVERSARIAL_REVIEW.md](ADVERSARIAL_REVIEW.md).

**Stack:** Postgres 16 + pgvector · Ollama (`bge-m3` embeddings, a 14B-class extractor such as
`qwen3:14b`) · FastMCP (streamable-HTTP MCP) · FastAPI (REST) · Starlette+Jinja (web UI, no build
step) · Docker Compose for the HTTP tier, systemd for the GPU/filesystem workers.

**Surfaces** (hostnames below are placeholders — substitute your own):

| Surface | URL | Auth |
|---|---|---|
| MCP (public) | `https://kb.example.com/mcp` | OAuth (claude.ai) **or** bearer |
| MCP (local) | `http://127.0.0.1:8321/mcp` | bearer |
| REST API | `https://api.example.com/api` | bearer |
| Web UI | `https://web.example.com` | OAuth sign-in (allow-list) or bearer form |

Publish the public surfaces however you like — a reverse proxy or an outbound tunnel both work, and
a tunnel needs no inbound port. **Kill switch:** stop whatever fronts the public hostnames and all
external exposure is gone instantly (the data is untouched; local/loopback access is unaffected).

**Auth:** one bearer token (`KB_TOKEN` in `.env`, never committed) works on every surface; the
public MCP additionally speaks OAuth for claude.ai. Rotate by editing `.env` and restarting the HTTP
services. Get the token with `grep '^KB_TOKEN=' .env`.

> ⚠️ **A public endpoint serves the entire KB — including any client/NDA projects — behind this one
> token.** Anyone with the token has full access. See §11 for the security model and the recommended
> per-project public allow-list.

---

## 1. Claude Code (CLI / IDE)

```bash
# local, on the server:
claude mcp add --transport http --scope user kb http://127.0.0.1:8321/mcp \
  --header "Authorization: Bearer $KB_TOKEN"
# any other machine, over the public endpoint:
claude mcp add --transport http --scope user kb https://kb.example.com/mcp \
  --header "Authorization: Bearer <KB_TOKEN>"
```

Then just ask: *"search kb for SPI polarity"*, *"have we handled MQTT OTA anywhere?"* — the model
calls the `kb_*` tools. Remove with `claude mcp remove kb -s user`.

## 2. claude.ai (web / mobile) as a Custom Connector

claude.ai → **Settings → Connectors → Add custom connector** → URL `https://kb.example.com/mcp`.

The public MCP endpoint speaks **OAuth 2.0** (a sign-in flow restricted to an allow-list of
addresses) in addition to the static bearer, so the claude.ai connector authorises normally. OAuth
state is persisted server-side, so a connected session survives restarts and redeploys; issued
tokens are long-lived and refresh silently.

## 3. MCP tools (what the model can call)

| Tool | Purpose |
|---|---|
| `kb_search(query, project?, domain?, tags?, type?, boost_project?, include_superseded?, as_of?, expand?, k?)` | Hybrid search across all projects (cross-domain recall); `as_of` = point-in-time, `expand` = pull in linked neighbors |
| `kb_deep_search(question, project?, save?)` | **Multi-query retrieval + grounded, cited synthesis** for architecture digs (see §5) |
| `kb_get(id, with_relations?)` | Full item: body, tags, code anchor, relations, supersession chain |
| `kb_save(project, type, title, body, tags?, code_ref?, links?, valid_from?, valid_to?)` | Save a **verified** item (decision/gotcha/snippet/bug/…); `valid_from`/`valid_to` set its validity interval |
| `kb_supersede(old_id, reason, new_body?/new_item_id?, valid_from?, valid_to?)` | Correct without losing history |
| `kb_link(src, rel, dst, evidence_item_id?)` | Relate items (`fixes`, `alternative_to`, `depends_on`, …) |
| `kb_timeline(project, from_date?, to_date?, limit?)` | Chronological history incl. superseded |
| `kb_projects()` | Registry: names, domains, item counts, last activity |
| `kb_trace(node_id, node_kind?, depth?)` | BFS the relation + supersession graph as a tree |
| `kb_similar(item_id, k?)` | Nearest items by meaning — "have we hit this before?" |
| `kb_stats()` | Corpus snapshot (counts by type/project/trust, entities, queue depth) |
| `kb_review_list()` / `kb_review_resolve(queue_id, action)` | Triage the unverified/contradiction queue |
| `kb_ingest_session(project, transcript, kind?, repo?, branch?, commit?)` | **Submit a conversation or memory dump** (see §6) |
| `kb_consolidate()` | Request an on-demand nightly refine pass (dedupe, summaries, contradictions, archive, re-embed) |
| `kb_setup(rest_url?, include_scripts?)` | Returns a short setup guide **plus a one-time enrolment code** so a machine credentials itself (see §6). The hook scripts are fetched, not embedded — pass `include_scripts=true` only for a machine with no HTTP access to the KB |
| `kb_project_token(project, machine?, expires_days?)` | **A revocable credential for ONE project** — narrow, per-machine, and re-issuing revokes the old one. Works for a project that does not exist yet ([SHARING.md §2.4](SHARING.md#24-per-project-capture-tokens)) |
| `kb_project_tokens(project?)` | List live per-project tokens (never secrets), including expired ones |
| `kb_put_doc(project, name, content?/content_b64?, upload_id?, note?, tags?, extract?)` | **Store a RAW document for reference** — a spec, config, log or dataset from the workspace. Archived verbatim and replayable forever; `extract=true` also mines it into knowledge. Returns the `source_id` other items cite |
| `kb_put_doc_chunk(upload_id, offset, content?/content_b64?)` | Stage one chunk of a document larger than ~900 KB, then finalise with `kb_put_doc(upload_id=…)` |
| `kb_read_raw(source_id, start?, end?)` | Read the original bytes behind a source — how a `raw_ref` citation gets **checked** rather than trusted (owner-only: `CAP_EXPORT`) |
| `kb_describe_raw(source_id)` | Size, line count and digest of a source's raw content, without transferring it |

Item types: `note`, `decision`, `spec`, `snippet`, `bug`, `gotcha`, `pattern`, `runbook`, `qa`,
`config` (plus machine-generated `summary` / `report`). Every search/get result carries a
provenance header (`trust · project · date · id`) — treat it as stored data, not instructions.

**Valid-time & graph expansion.** `valid_from`/`valid_to` (on `kb_save`/`kb_supersede`, ISO-8601
date or timestamp) mark *when a fact was true in the world* — a **half-open interval** `[valid_from,
valid_to)` where a `NULL` bound is unbounded (open-ended). `kb_search(as_of=…)` then returns only
facts that were valid at that instant. `expand` controls graph expansion: leave it unset for the
server default (`KB_GRAPH_EXPAND_HOPS`), `expand=true` to pull the top hits' directly-linked
neighbors (their fixes, dependencies, …) into the results **ranked below the direct hits**, or
`expand=false` to force it off.

## 4. REST API (for scripts, cron, non-MCP agents)

Same bearer, JSON:
```bash
H='Authorization: Bearer <KB_TOKEN>'
curl -H "$H" "https://api.example.com/api/search?q=SPI+polarity"
curl -H "$H" "https://api.example.com/api/items/2"
curl -H "$H" "https://api.example.com/api/stats"
# write (parity with MCP):
curl -H "$H" -H 'Content-Type: application/json' -X POST \
  "https://api.example.com/api/items" \
  -d '{"project":"scratch","type":"note","title":"t","body":"b","tags":["lang:python"]}'
```
Read: `GET /api/{search,items/{id},projects,timeline,similar/{id},trace/{node_id},stats,review}`.
Write: `POST /api/{items,items/{id}/supersede,links,review/{queue_id}/resolve,ingest-session}`.
Enrolment: `POST /api/enrol` (redeem a one-time `kbe_` code for this machine's own credential) and
`GET /api/setup/{name}` (fetch one of the four hook scripts). Both are reachable with an unredeemed
enrolment code as the bearer, because a new machine holds nothing else; a GET does **not** spend the
code. See [SHARING.md §2.5](SHARING.md#25-enrolment-codes).
No token → 401; unsupported method → 405. OpenAPI at `/docs`. (Deep search is available via MCP and
the CLI; it is intentionally not a REST endpoint because a run can take minutes.)

## 5. Deep search (dig out architecture on demand)

`kb_deep_search` / `kb deep-search "<question>"` runs a multi-step pipeline on the **local model**:
plan 3–6 diverse sub-queries → retrieve across the whole KB → read the full bodies (bounded) →
synthesize a **cited** answer (`[item N]` inline), surfacing any known contradictions among the
sources as both-sides notes. It is deliberately slow-but-available (minutes on a local model, no
cloud API). The report is saved as a `type='report'`, `trust='unverified'` item **before returning**,
so even if a client times out you can retrieve it later with `kb_search(question, type='report')`.

```bash
kb deep-search "how does the HMI/SCADA API integrate with the existing command bus?"
```

## 6. Ingest conversations & memory from remote agents

Agents on other machines can teach the KB from their sessions. Submit a full conversation transcript
**or** a raw memory dump; it is scrubbed for secrets and stored immediately (no GPU), then the
**nightly consolidation** runs grounded extraction — surviving items land as `trust='unverified'`,
quote-grounded, in the review queue for you to promote.

MCP: `kb_ingest_session(project="myproj", transcript="<text>", kind="session"|"memory", repo?, branch?, commit?)`
```bash
curl -H "Authorization: Bearer <KB_TOKEN>" -H 'Content-Type: application/json' -X POST \
  "https://api.example.com/api/ingest-session" \
  -d '{"project":"myproj","kind":"session","transcript":"User: ... Assistant: ..."}'
# → 202 {"queue_id": N, "duplicate": false}
```
- Instant + idempotent (re-submitting identical content is a no-op); ≤1 MB per call; content **and**
  metadata are scrubbed for secrets before storage; the reserved `kb-system` project can't be
  targeted remotely; the queue has a backlog cap.
- Nothing is trusted automatically — review with `kb review list` / `kb_review_resolve`, or the web UI.

### Claude Code session hooks (both directions)

Ask a connected Claude Code to run **`kb_setup`**. It returns a short guide and a **one-time
enrolment code**; the machine then fetches the scripts and credentials itself (see
[SHARING.md §2.5](SHARING.md#25-enrolment-codes)) — no bearer is ever pasted by hand:

```bash
export KB_ENROL_CODE=kbe_…   KB_REST_URL=https://api.<host>
mkdir -p ~/.claude/kb-setup && cd ~/.claude/kb-setup
for f in install.sh flush-session-to-kb.sh kb-session-brief.sh kb-mcp-call.sh; do
  curl -fsS -H "Authorization: Bearer $KB_ENROL_CODE" "$KB_REST_URL/api/setup/$f" -o "$f"
done
bash install.sh
```

`install.sh` registers **four** events and auto-detects the transport, first match winning:
**LOCAL** (a KB repo + `uv`) → **MCP** (a bearer-registered KB server in `~/.claude.json`) →
**CONNECTOR** (any `claude` binary) → **REMOTE** (the fallback). Note CONNECTOR outranks REMOTE on
essentially every machine, so to get bearer REMOTE mode write `~/.claude/kb-flush.env` *before*
running the installer — an existing config is never overwritten.

- **Flush out** (`PreCompact` + `SessionEnd`): the session's new transcript delta is enqueued to the
  KB before every compaction — never blocking, never using the GPU at capture time. Deltas are sent
  as line-aligned chunks with a per-session offset, so a failure resumes rather than re-sends;
  verified end to end at **100 MB** (117 chunks, byte-identical, ~145 s).
- **Brief in** (`SessionStart`, matcher `startup|clear`): a quick KB search seeded by the repo's
  recent commit subjects is injected into Claude's starting context. Fails silently — a briefing
  never breaks a session.
- **Per-prompt recall** (`UserPromptSubmit`): each substantive prompt also gets a KB search seeded by
  the prompt itself. Self-throttling; `KB_PROMPT_RECALL=0` turns just this off.

**Which project a session files under.** The **git root's** directory name, slugified — so a
session started in a subdirectory files under the repo, not the subdirectory. A workspace can name
itself instead by putting one line in a `.kbproject` file at the repo root:

```bash
echo 'punch-rescue' > .kbproject     # this checkout files under 'punch-rescue'
```

Useful when the folder and the project are not the same thing: a clone or worktree under a
different name, two checkouts that are one logical project, or a monorepo area that deserves its
own. The value is slugified exactly like a directory name, so it can only ever become `[a-z0-9-]`.
A non-git directory falls back to its own name.

**Verifying it works.** Every outcome is a line in `~/.claude/hooks/flush-session.log` — the hooks
always exit 0, so the log is the only signal. Look for `local … ok bytes=` (LOCAL),
`flush … ok bytes=` (MCP **and** REMOTE — they share a branch) or `connector … ok bytes=`
(CONNECTOR). `partial … will resume` means more follows; `NOT CONFIGURED` and `NOT CAPTURED` are the
loud failures.

**CONNECTOR does not scale.** It relays one model call per ~24 KB, so a large session is capped by
`KB_CONNECTOR_SESSION_MAX` (2 MB): above it only the most recent bytes are relayed and the rest is
dropped with a `NOT CAPTURED` line naming the transcript so you can re-ingest it. Use a bearer
transport for big sessions — it chunks at 900 KB with no model in the path. Tunables:
`KB_FLUSH_CHUNK`, `KB_FLUSH_MAX`, `KB_CONNECTOR_CHUNK`, `KB_CONNECTOR_MAX`,
`KB_CONNECTOR_SESSION_MAX`.

## 7. Web UI (read-only)

`https://web.example.com` → sign in (bearer token, or OAuth if configured). A modern, themeable
(light/dark) read-only view:

- **Search** with filters (project / domain / type / tag) and an empty-state tag cloud.
- **Item pages** render Markdown bodies with server-side syntax highlighting, a metadata sidebar
  (trust/status, dates, source, code anchor, grounding quote), and relation cards with real titles.
- **Projects** index + per-project pages, **tag** browsing, and a `/timeline` with date filters.
- **Dashboard** (`/stats`): KPI tiles + CSS bar charts of items by type/project/status/trust.
- **Review** (`/review`): the unverified/contradiction queue with evidence counts and suggested
  resolutions (read-only view; resolve via CLI/MCP).

Strictly read-only by construction — the browser can never write.

## 8. CLI (`kb`)

`uv run kb <cmd>`:
`add`, `adr`, `search`, `deep-search`, `get`, `supersede`, `link`, `projects`, `project set`,
`trace`, `similar`, `stats`,
`review {list,promote,reject,supersede,apply}`, `seed`, `ingest-repo`, `ingest-session`,
`backfill-entities`, `reembed`, `migrate`, `consolidate-now`, `consolidate-status`.
(History browsing lives in the `kb_timeline` MCP tool and the web UI `/timeline` — there is no
`kb timeline` CLI command.)

## 8.5 Raw data: what is kept, and re-running the pipeline over it

**Everything a session or document submits is kept in raw form, indefinitely.** Nothing in the
automatic paths deletes it:

| Layer | What it holds |
| --- | --- |
| `ingest_queue.content` | the submission exactly as received. Draining only flips `status`; the row and its content stay |
| `$KB_RAW/<project>/<sha256>.zst` | the original bytes, zstd-compressed, content-addressed on the hash of the **original** (not the scrubbed body), so two documents that scrub alike cannot overwrite each other |
| `sources.raw_path` → `items.source_id` | every item traces back to the source, and the source to its blob |

Which kinds carry a blob: `session`, `git` (repo docs), and `file` — which includes every document
submitted with `kb_put_doc`. `chat` and `manual` items do **not** archive raw, so they cannot be
replayed.

**Re-running a better pipeline over it** — the reason retention matters:

```bash
kb reprocess --project myproj          # shadow: reports what WOULD change, writes nothing
kb reprocess --project myproj --apply  # re-extract with the CURRENT pipeline
```

It never supersedes or deletes: improved extractions land as **new unverified items with review
rows**, identical ones dedupe to no-ops, and each carries `meta.reprocessed_from`. Filters:
`--before`, `--extractor` (target one old model), `--limit`. Eligible sources are sessions plus
submitted documents; repo docs and manual items are excluded because their body already *is* their
content, so replaying could only duplicate it.

**Citing a region.** An item's `raw_ref` (`{source_id, start, end, line_start?}`) says which bytes of
which raw source a claim came from. It is validated at ingest — the source must exist, be readable,
hold raw content, and the range must fall inside it — so a citation can be *followed* with
`kb_read_raw` and checked, rather than trusted.

**Operationally:** the raw store grows without bound by design. It is included in the off-site backup
(`kb-raw-*.tar.zst`, age-encrypted), so a restore that skips it loses every original. Watch its size
alongside the database.

## 9. How the KB refines itself (nightly)

A nightly consolidation pass keeps the corpus clean and consistent — each step isolated so one
failure never aborts the rest:

1. **Drain** queued session/memory submissions through grounded extraction (the only place the large
   extractor runs on remote content).
2. **Doc-refresh sweep** — when a repo doc is re-ingested after it changed, the newer revision
   automatically supersedes the stale one (same source path). Git history is the decision; the KB
   just mirrors it. This also happens at scan time, so old-and-new never linger.
3. **Dedupe** near-identical items to one canonical (keeps history).
4. **Summaries** — (re)write topic summaries for tags that accumulated enough new items.
5. **Contradictions** — flag pairs of items that assert incompatible facts, into the review queue.
6. **Evidence weightage** — for each open contradiction, judge nearby items as supporting side A, B,
   or neither, and record **raw + trust-weighted** counts (verified sources outweigh unverified
   extractions). Counts accumulate night over night as new corroboration lands.
7. **Auto-resolve (bounded)** — when one side's evidence is decisively lopsided **and holds for two
   consecutive nights**, the losing item is auto-superseded and the row closed (fully audited and
   reversible). Everything below that bar stays in the queue with its evidence counts and a
   **suggested resolution** you can apply anytime with `kb review apply <id>` (or `--all`).
8. **Archive** stale items, **re-embed** anything on an old embedding model, write a report item.

The pass also runs a **search-quality sentinel**: it replays the golden retrieval eval and flags any
hit@5/MRR regression versus the prior run (kill switch `KB_EVAL_IN_CONSOLIDATION=false` skips it).

Both auto-resolution tiers have kill-switch flags in `.env`
(`KB_AUTO_SUPERSEDE_DOC_REFRESH`, `KB_AUTO_RESOLVE_EVIDENCE`); with them off the KB reverts to
propose-only and everything waits for a human.

## 10. Operations

**Services** — an HTTP tier in containers (DB, MCP, REST, web) plus background workers (an inbox
watcher + daily repo-docs scan, and the nightly consolidation on a timer).

- **Rebuild/redeploy after a code change:** rebuild the app image and bring the HTTP services back up.
- **Logs:** container logs for the HTTP tier; the worker/consolidation journals for the nightly pass.
- **Backups:** `make backup` (pg_dump | zstd, `chmod 600`, pruned to a rolling window). Restore
  drill: `bash scripts/restore_test.sh` (restores the newest dump into a throwaway container, asserts
  counts, PASS/FAIL).
- **DB migrations:** `make migrate` (additive; runs `migrations/*.sql` in order).
- **Config:** everything via `.env` (see `.env.example`) — DB, model names, thresholds, feature
  flags. No hardcoded hosts, ports, model names, or secrets in code. Never commit `.env` or any
  secret/credential file.

## 11. Security model (summary)

- One static bearer on every surface; the public MCP additionally accepts OAuth (allow-listed
  sign-in). Only loopback and the outbound proxy/tunnel are paths in — no host port need be exposed.
- Secrets scrubbed on every ingest (cloud/API keys, private keys, JWTs, `password=…`).
- Automatic/remote extraction is `trust='unverified'`, quote-grounded, and review-gated — it can
  contribute knowledge but never poison the `verified` tier.
- Automatic contradiction resolution is bounded: mechanical (git-derived doc refreshes) and
  decisively-corroborated (2-night stable) cases only; everything is a reversible supersession, never
  a delete; both tiers have kill switches. See §9.
- The large extractor runs only in the nightly pass or explicit user-invoked runs (GPU courtesy).
- **Enrolment codes** (`kbe_…`) let a machine credential itself without a bearer being pasted
  anywhere: single-use, ~15 min, SHA-256 at rest, refusals byte-identical, and they authenticate as
  an identity with no projects and no capabilities. A code redeems to a **narrow, revocable
  per-project token** — never the master key — so no enrolled machine holds owner authority and any
  one can be revoked alone. See [SHARING.md §2.5](SHARING.md#25-enrolment-codes).
- Reading whole archived originals (`kb_read_raw`) requires `CAP_EXPORT`, which no role bundle
  carries — it is owner-only unless deliberately granted.
- **Recommended hardening:** a per-project `projects.public` allow-list so NDA/client projects can be
  withheld from the public surface while local/REST keeps full access.

See [PLAN.md](PLAN.md) for the full architecture, invariants, and phase history.

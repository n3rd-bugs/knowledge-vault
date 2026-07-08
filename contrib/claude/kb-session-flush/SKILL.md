---
name: kb-session-flush
description: Install Claude Code hooks that connect sessions to the Knowledge Vault (KB) - flush each session's transcript to the KB before every compaction, and pre-brief each NEW session with relevant KB knowledge at start. Use when the user wants to auto-save Claude Code sessions to the KB, get KB context injected on session start, or add these hooks to another machine.
---

# Install the KB session hooks (flush + pre-brief)

This skill installs three hook behaviors (two scripts):

1. **Flush** (`PreCompact` + `SessionEnd`): the session's new transcript delta is
   flushed into the Knowledge Vault before every compaction. Never blocks
   compaction (detaches immediately), never uses the GPU at capture time — the
   delta is enqueued and the nightly `consolidate` pass runs the 14B grounded
   extraction, landing unverified items in the review queue (§7.2).
2. **Pre-brief** (`SessionStart`): when a NEW session starts (startup or /clear —
   resume/compact are skipped), a quick KB search seeded by the repo's recent
   commit subjects is INJECTED into Claude's starting context, so it begins the
   activity aware of prior decisions and gotchas. `KB_BRIEF_ON_RESUME=1` briefs
   resumes too.
3. **Per-prompt recall** (`UserPromptSubmit`): on every substantive prompt, a KB
   search seeded by THE PROMPT ITSELF is injected before Claude answers — so each
   question is grounded in prior knowledge, not just the session opener. Noise
   controls: slash commands and <12-char prompts skip; a CONTENT-WORD GATE skips
   prompts made only of acknowledgements/stopwords/generic imperatives ("Yes
   please review and update") — hybrid search would return k-best noise for
   those; and a repeat of the same query within 10 min is suppressed (its hits
   are already in context). Disable with `KB_PROMPT_RECALL=0`.
   CONNECTOR mode is skipped here by default (a headless `claude -p` per prompt
   costs tokens + latency) — opt in with `KB_PROMPT_RECALL_CONNECTOR=1`. Both
   pre-brief and recall are the same script (`kb-session-brief.sh`), branching on
   the hook event.

All hooks fail silently — recall/brief never break a prompt or session.

Four modes, auto-detected by `install.sh` (in priority order):
- **LOCAL** (the KB host): the hook runs `kb ingest-session <transcript> --queue`
  (checkpointed, GPU-free enqueue). Config: `KB_DIR` in `~/.claude/kb-flush.env`.
- **MCP** (any machine already connected via `claude mcp`): **zero config** — the
  installer auto-discovers the KB server's URL + bearer from `~/.claude.json` and
  the hooks call `kb_ingest_session` / `kb_search` over that same MCP endpoint (a
  tiny `kb-mcp-call.sh` helper speaks the streamable-HTTP protocol). Config keys
  `KB_MCP_URL` + `KB_TOKEN` are written automatically.
- **CONNECTOR** (no bearer anywhere, but Claude Code is logged into a claude.ai
  account whose KB connector is bridged in): **zero config** — the hooks relay
  through headless `claude -p` calls using the cloud-held connector auth
  (`KB_CONNECTOR=1` + `CLAUDE_BIN`, written automatically). The model is the
  data path, so chunks are small (~24 KB) and capped per firing
  (`KB_CONNECTOR_MAX`, default 240 KB); each flush/brief spends Claude usage.
  A recursion guard (`KB_HOOK_GUARD`) keeps the child `claude` run's own hooks
  inert.
- **REMOTE** (none of the above): the hook POSTs the delta to
  `POST /api/ingest-session` over HTTPS with a bearer token, tracking a per-session
  byte offset so each compaction submits only the new window. Config:
  `KB_REST_URL` + `KB_TOKEN` in `~/.claude/kb-flush.env`.

## Steps

1. Run the installer (it writes `~/.claude/hooks/flush-session-to-kb.sh`, creates
   `~/.claude/kb-flush.env`, and registers the hooks in `~/.claude/settings.json`):

   ```bash
   bash "<this skill dir>/install.sh"
   ```

2. Read the printed `mode:` line.
   - If it says **REMOTE**, the KB isn't local. Ask the user for the KB REST base
     URL (default `https://api.example.com`) and a bearer token, then write
     them into `~/.claude/kb-flush.env` (`chmod 600`), e.g.:
     ```
     KB_REST_URL=https://api.example.com
     KB_TOKEN=<the KB bearer token>
     ```
   - If **LOCAL**, nothing more to configure.

3. Run the smoke test the installer prints (feeds a fake `PreCompact` event to the
   hook) and confirm `~/.claude/hooks/flush-session.log` shows a `local … ` line
   (LOCAL) or `remote … http=202` line (REMOTE). Tell the user it's live: every
   compaction and session-end now flushes to the KB.

## Porting to another machine

Copy this whole skill directory to `~/.claude/skills/kb-session-flush/` on the
other machine and invoke it there (or run `install.sh` directly). On a non-KB
host it lands in REMOTE mode — just fill in `KB_REST_URL` + `KB_TOKEN`.

## Notes / limits (field-verified — KB items 495-498)

- The hook returns instantly; extraction is deferred to the nightly drain.
- **Chunking**: deltas are split into <=900 KB line-aligned chunks (server row cap
  is 1 MB); up to 1 GB per session. Chunks travel gzip+base64 in `transcript_gz`
  (~10x less wire). Do NOT use HTTP `Content-Encoding: gzip` — the origin does not
  decode it (KB item 497).
- **Edge limits** (when the KB is behind a CDN/proxy): keep requests small — a
  single huge request can hit the edge's ~100 s origin window (524). The chunker
  already guarantees this. A transient client status `000` whose body still
  carries a `queue_id` WAS accepted — the hooks treat it as success (item 497).
- **WAF caveat** (item 498): a managed WAF/OWASP ruleset in front of the KB API
  can block PLAINTEXT bodies containing shell/code snippets with an opaque HTML
  block page. The gzip path is immune (base64 body). For kb_save/plaintext
  writes, add a WAF skip rule for the bearer-authed API routes.
- **Windows / Git Bash** (item 496) — handled automatically by the installer and
  hooks: `nohup` detach fallback (no `setsid`), hooks registered as
  `bash.exe` + args (Claude Code on Windows spawns exe+args, not a shell), jq
  co-located next to the hooks + PATH-prepended, and native `C:\...` event
  paths converted with `cygpath` before use.
- **Never-silent failures**: the flush hook refuses to POST an empty/invalid
  body (logs `BUG: request body build failed`), and the SessionStart pre-brief
  surfaces any recent flush failure INTO the next session's context so it is
  seen, not buried in a log (item 495's sting was silent loss).
- `settings.json` merge is idempotent (matches both command and args forms) and
  preserves any other hooks you have.
- To disable: remove the entries from `~/.claude/settings.json` `hooks`, or
  delete `~/.claude/kb-flush.env` (the hooks no-op without config).

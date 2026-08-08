---
name: kb-session-flush
description: Install Claude Code hooks that connect sessions to the Knowledge Vault (KB) - flush each session's transcript to the KB before every compaction, and pre-brief each NEW session with relevant KB knowledge at start. Use when the user wants to auto-save Claude Code sessions to the KB, get KB context injected on session start, or add these hooks to another machine.
---

# Install the KB session hooks (flush + pre-brief)

This skill installs three hook behaviors, plus optional completion notifications
on `Stop` (inert until `~/.claude/notify.env` names a channel):

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
   those; and a repeat of the same query within 10 min IN THE SAME SESSION is
   suppressed (its hits are already in context). The suppression gate is keyed
   per session (`session_id`) + project + query, in per-session state files, so a
   different session, a different project, or `/clear` (a fresh session id) never
   falsely suppresses recall. Disable with `KB_PROMPT_RECALL=0`.
   CONNECTOR mode is skipped here by default (a headless `claude -p` per prompt
   costs tokens + latency) — opt in with `KB_PROMPT_RECALL_CONNECTOR=1`. Both
   pre-brief and recall are the same script (`kb-session-brief.sh`), branching on
   the hook event.

All hooks fail silently — recall/brief never break a prompt or session.

**One hook per event.** `install.sh` registers exactly ONE command with Claude
Code for each of the five events — `kb-hook-dispatch.sh <event>` — and that
script reads the event JSON once and pipes the same bytes to the scripts the
event needs (`UserPromptSubmit` → `turn-start.sh`, then `kb-session-brief.sh`).
Claude Code on Windows has an open upstream bug (GH #77078 and siblings): with
more than one hook on a single event, only one of the spawned processes receives
stdin and the others read empty — so recall ran with no prompt and captures died
in silence. Registering one dispatcher is the documented workaround. The
dispatcher always exits 0, runs its children sequentially, passes their stdout
through untouched (the brief's stdout IS the injected context), and logs one
`hook event arrived EMPTY: event=…` line to `flush-session.log` if it is ever
handed an empty event anyway, so residual hits stay countable.

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

## Requirements (NO sudo, ever)

`bash`, `curl`, and **one JSON tool**. The scripts resolve it through a fallback
chain, in this order (pinned by `tests/hookrig/cases/20`, `22`, `26`):

1. `jq` on `PATH`
2. a `jq` **co-located with the hooks** — `~/.claude/hooks/jq` (`jq.exe` on
   Windows); the installer copies whichever jq it used to there, so the box keeps
   working if `PATH` later loses it
3. `KB_JQ=/path/to/jq` in the environment
4. **`python3`** (stdlib `json` only) — a full fallback, not a degraded one:
   every jq program in the skill, including the `settings.json` merge, has a
   python twin that produces byte-identical output

Nothing needs installing as root: jq is a single static binary, so dropping it in
`~/.local/bin` (or beside the hooks, or anywhere `KB_JQ` points) is enough. If
NEITHER jq nor python3 exists, `install.sh` stops with those instructions instead
of a package-manager command, and a hook fired in that state writes a
rate-limited `NOT CONFIGURED` line to `~/.claude/hooks/flush-session.log` — which
the next session's pre-brief surfaces. A hook never dies silently.

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
   hook) and confirm `~/.claude/hooks/flush-session.log` shows the marker for the
   installed mode — `local <proj> ok bytes=…` (LOCAL), `flush <proj> ok bytes=…`
   (MCP **and** REMOTE — they share one branch), or `connector <proj> ok bytes=…`
   (CONNECTOR). `ok` means fully drained; `partial` means the rest follows on the
   next firing. (No hook writes `http=`.) Tell the user it's live: every
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
  hooks, and simulated end to end by `tests/hookrig/cases/23` + `25`:
  `nohup` detach fallback (no `setsid`), hooks registered as `bash.exe` + args
  (Claude Code on Windows spawns exe+args, not a shell), jq co-located next to
  the hooks + PATH-prepended, and native `C:\...` event paths converted before
  use. Every one of those decisions lives in ONE shared block (`kb_is_windows`,
  `kb_winpath`, `kb_nativepath`, `kb_detach_cmd`, `kb_timeout`, the JSON
  backend) that is byte-identical in all four scripts. **cygpath is not
  required**: `kb_winpath` falls back to the pure-shell mapping
  (`C:\Users\First Last\p` -> `/c/Users/First Last/p`) and `kb_nativepath` falls
  back to `$EXEPATH`, then to a bare `bash.exe` resolved through the Windows
  PATH. Paths with SPACES are the norm there, so every path expansion in the
  four scripts is quoted (`tests/hookrig/cases/24` drives install + flush +
  recall through a `HOME` and a project dir that both contain spaces).
- **macOS / BSD / Windows** — the brief+recall hook wraps every bounded search in a
  portable `kb_timeout`: it uses GNU `timeout` when present (Linux — unchanged),
  else `gtimeout` (Homebrew coreutils), else a built-in perl watchdog (perl ships
  on macOS and Git Bash) that forwards the child's stdout + exit code. Stock macOS
  has no `timeout` binary, so without this the pre-brief and per-prompt recall were
  a silent no-op there. A resolved `timeout`/`gtimeout` is TRUSTED only after a
  behaviour check (a coreutils `--version`, or a `<bin> 1 true` self-test with stdin
  redirected): on Git Bash `command -v timeout` finds
  `C:\Windows\System32\timeout.exe` — a wait utility that cannot run commands and
  rejects redirected stdin — so the bare name is not enough; it is rejected and the
  perl watchdog is used instead. If the perl `fork()` itself fails (heavy
  fork/memory pressure) `kb_timeout` now bails with an error rather than running the
  command unbounded, and the caller degrades to no recall for that firing.
- **Reachability probe / no prompt stalls** — because `UserPromptSubmit` recall runs
  synchronously (15 s hook budget), a cheap reachability check runs first: a
  `curl --connect-timeout 2` connect to the KB (MCP/REMOTE) or a trivial local check
  (LOCAL). A dead/blackholed KB skips recall in ~2 s instead of stalling the prompt
  for the whole budget. All inner search timeouts are kept strictly under the hook
  budget (recall ≤ ~8 s, connector recall ≤ 12 s), and `kb-mcp-call.sh` curls carry
  `--connect-timeout 3`.
- **Partial-line safety** — a flush window that ends in an unterminated JSONL line
  is trimmed to the last complete line and the remainder is held for the next
  firing (never advanced past mid-line, so no straddled message is lost); the
  terminal `SessionEnd` firing sends the final line as-is so nothing is dropped.
- **Bounded state** — the CONNECTOR/MCP/REMOTE `.offset` files are deleted on a
  fully-drained `SessionEnd` and swept if older than 30 days; `flush-session.log`
  is truncated to its last 500 lines once it exceeds 1 MB; per-session `recall-*`
  state files older than 7 days are pruned. All housekeeping is confined to the
  hooks' own `~/.claude/hooks` state/log dirs.
- **Never-silent failures**: the flush hook refuses to POST an empty/invalid
  body (logs `BUG: request body build failed`), and the SessionStart pre-brief
  surfaces any recent flush failure INTO the next session's context so it is
  seen, not buried in a log (item 495's sting was silent loss). A flush firing
  logs `ok bytes=… (offset X/Y)` ONLY when fully drained (`X>=Y`); a firing that
  broke early (a failed chunk, a held partial line, or the per-firing cap) logs a
  truthful `partial bytes=… (offset X/Y) — will resume` instead. The pre-brief
  reads any `partial` (offset `X<Y`) as an unresolved failure — so a partial drop
  on the terminal `SessionEnd`, where there is no next firing, can no longer be
  masked by an earlier chunk's success and vanish silently. LOCAL enqueue logs the
  same `ok bytes=` marker on success, so a one-off transient failure self-clears at
  the next successful firing instead of nagging every new session.
- `settings.json` merge is idempotent (matches both command and args forms) and
  preserves any other hooks you have.
- To disable: remove the entries from `~/.claude/settings.json` `hooks`, or
  delete `~/.claude/kb-flush.env` (the hooks no-op without config).

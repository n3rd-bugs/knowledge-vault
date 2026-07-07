---
name: kb-session-flush
description: Install Claude Code hooks that connect sessions to the Knowledge Vault (KB) - flush each session's transcript to the KB before every compaction, and pre-brief each NEW session with relevant KB knowledge at start. Use when the user wants to auto-save Claude Code sessions to the KB, get KB context injected on session start, or add these hooks to another machine.
---

# Install the KB session hooks (flush + pre-brief)

This skill installs two hook sets:

1. **Flush** (`PreCompact` + `SessionEnd`): the session's new transcript delta is
   flushed into the Knowledge Vault before every compaction. Never blocks
   compaction (detaches immediately), never uses the GPU at capture time — the
   delta is enqueued and the nightly `consolidate` pass runs the 14B grounded
   extraction, landing unverified items in the review queue (§7.2).
2. **Pre-brief** (`SessionStart`): when a NEW session starts (startup or /clear —
   resume/compact are skipped), the hook runs a quick KB search seeded by the
   repo's recent commit subjects + project name, and its stdout is INJECTED into
   Claude's starting context — so Claude begins the activity already aware of
   prior decisions and gotchas. Fails silently (a briefing must never break a
   session); set `KB_BRIEF_ON_RESUME=1` in kb-flush.env to brief resumes too.

Two modes, auto-detected by `install.sh`:
- **LOCAL** (the KB host): the hook runs `kb ingest-session <transcript> --queue`
  (checkpointed, GPU-free enqueue). Config: `KB_DIR` in `~/.claude/kb-flush.env`.
- **REMOTE** (any other machine): the hook POSTs the delta to the KB's
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
     URL (default `https://api.n3rd-bugs.dev`) and a bearer token, then write
     them into `~/.claude/kb-flush.env` (`chmod 600`), e.g.:
     ```
     KB_REST_URL=https://api.n3rd-bugs.dev
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

## Notes / limits

- The hook returns instantly; extraction is deferred to the nightly drain.
- REMOTE submissions are capped at ~1 MB per delta (the queue limit); an oversized
  window logs a non-2xx and is retried next compaction. Very long turns between
  compactions are the only case that can hit this.
- `settings.json` merge is idempotent and preserves any other hooks you have.
- To disable: remove the two entries from `~/.claude/settings.json` `hooks`, or
  delete `~/.claude/kb-flush.env` (the hook no-ops without config).

---
name: kb-session-flush
description: Install a Claude Code hook that flushes each session's transcript into the Knowledge Vault (KB) before every compaction, so long-session context is captured before it is summarized away. Use when the user wants to auto-save Claude Code sessions to the KB, set up the pre-compact flush, or add that hook to another machine.
---

# Install the KB session-flush hook

This skill installs a `PreCompact` + `SessionEnd` hook that flushes the current
Claude Code session's new transcript delta into the Knowledge Vault. It never
blocks compaction (the hook detaches immediately) and never uses the GPU at
capture time — the delta is enqueued and the nightly `consolidate` pass runs the
14B grounded extraction, landing unverified items in the review queue (§7.2).

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

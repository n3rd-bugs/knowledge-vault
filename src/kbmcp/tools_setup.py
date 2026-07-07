"""kbmcp.tools_setup — the kb_setup MCP tool: install the KB Claude Code hooks.

A server-side tool can't touch the CALLING machine's ``~/.claude``; instead it
returns a complete, ready-to-apply guide (the two hook scripts + the idempotent
installer + steps) that a Claude Code client can write and run itself. The hook
scripts ship as package data under ``setup_assets/`` (same pattern as
kbweb/static) so this works from the container, where ``contrib/`` is absent.
A drift-guard test keeps the shipped copies identical to ``contrib/``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastmcp.exceptions import ToolError

from kbcore.config import settings

log = logging.getLogger("kbmcp.setup")

ASSETS_DIR = Path(__file__).parent / "setup_assets"
_SCRIPTS = (
    "flush-session-to-kb.sh",
    "kb-session-brief.sh",
    "kb-mcp-call.sh",
    "install.sh",
)
_PLACEHOLDER_URL = "https://api.example.com"


def _read(name: str) -> str:
    return (ASSETS_DIR / name).read_text()


def build_setup_guide(rest_url: str | None = None) -> str:
    """Assemble the install guide + embedded scripts. Pure text, no I/O beyond
    reading the packaged assets.

    The REMOTE config's ``KB_REST_URL`` is filled from (in order) the ``rest_url``
    argument, the server's configured ``kb_public_rest_url``, else a generic
    placeholder; the token is always user-supplied (never emitted — SEC).
    """
    configured = (rest_url or settings.kb_public_rest_url or "").strip()
    url = (configured or _PLACEHOLDER_URL).rstrip("/")
    if configured:
        remote_hint = (
            "Your KB's REST endpoint is filled in below — you only need to add "
            "`KB_TOKEN` (the same bearer you registered the MCP server with)."
        )
    else:
        remote_hint = (
            "Set `KB_REST_URL` to your KB's REST base (e.g. `https://api.<your-host>`) "
            "and `KB_TOKEN` to a bearer token."
        )
    scripts = {name: _read(name) for name in _SCRIPTS}
    files_block = "\n\n".join(
        f"### File: `~/.claude/kb-setup/{name}`\n\n```bash\n{body.rstrip()}\n```"
        for name, body in scripts.items()
    )
    return f"""# Install the KB Claude Code session hooks (on THIS machine)

Two hooks connect this machine's Claude Code sessions to the Knowledge Vault, in
both directions:

- **Flush out** (`PreCompact` + `SessionEnd`): the session's new transcript delta
  is enqueued to the KB before every compaction — non-blocking, GPU-free.
- **Pre-brief in** (`SessionStart`): a new session (startup or `/clear`) gets a
  quick KB search — seeded by the repo's recent commit subjects — injected into
  Claude's starting context, so it begins aware of prior decisions and gotchas.

## Which mode is this machine? (`install.sh` auto-detects)

- **KB host** (a local KB repo + `uv` present): LOCAL mode — no token needed.
- **Already connected via `claude mcp`** (a bearer-registered KB server in
  `~/.claude.json`): **MCP mode — zero config.** The installer reuses that
  registration's URL + token, and the hooks call `kb_ingest_session` /
  `kb_search` over the same MCP endpoint. Nothing to fill in.
- **Otherwise, self-hosted KB**: REMOTE mode — the hooks POST to your KB's REST
  endpoint with a bearer token (fill in step 3).
- **claude.ai OAuth connector only** (no static bearer): **CONNECTOR mode,
  zero config.** claude.ai connectors bridge into Claude Code automatically, and
  the hooks relay through a headless `claude -p` call using that cloud-held
  auth. Caveats: the model is the data path (small ~24 KB chunks, capped per
  firing) and each flush/brief spends Claude usage — bearer modes are the
  efficient path for heavy sessions.

## Steps (a Claude Code client can perform these directly)

1. Check `jq` is installed: `command -v jq` (else `sudo apt install jq`).
2. Write the four files below to `~/.claude/kb-setup/`.
3. **REMOTE machines only** (skip if `install.sh` reports LOCAL or MCP) — create
   `~/.claude/kb-flush.env`, then `chmod 600` it. {remote_hint}
   ```
   KB_REST_URL={url}
   KB_TOKEN=<your KB bearer token>
   ```
4. Run the installer (idempotent; registers PreCompact + SessionEnd + Session
   Start in `~/.claude/settings.json`, preserving any other hooks):
   ```bash
   bash ~/.claude/kb-setup/install.sh
   ```
5. Smoke test (the installer prints exact commands): feed a fake event to the
   flush hook and confirm `~/.claude/hooks/flush-session.log` shows a `local …`
   (LOCAL) or `remote … http=202` (REMOTE) line. New sessions will then start
   with a `[KB pre-brief · project …]` block.

To disable later: remove the entries from `~/.claude/settings.json` `hooks`, or
delete `~/.claude/kb-flush.env` (the hooks no-op without config).

{files_block}
"""


def _consolidation_status_line() -> str:
    from kbcore import jobs

    st = jobs.latest_consolidation_status()
    if not st:
        return "no runs recorded yet"
    tail = f" — {st['result']}" if st.get("result") else ""
    return f"latest run #{st['id']} ({st['requested_by']}): {st['status']}{tail}"


def register(mcp) -> None:
    """Register the control-plane tools (kb_setup, kb_consolidate)."""

    @mcp.tool
    def kb_consolidate() -> str:
        """Request an on-demand nightly consolidation run (dedupe, summaries,
        contradiction detection + evidence + auto-resolution, archive, re-embed).

        The run executes on the host worker (GPU) and takes MINUTES; this only
        enqueues it (deduped against an in-flight run) and returns the current
        status. Poll kb_consolidate again — or kb_stats — for completion.
        """
        from kbcore import jobs

        req_id, created = jobs.request_consolidation("mcp")
        head = (
            f"queued consolidation request #{req_id}"
            if created
            else f"a run is already in progress (request #{req_id})"
        )
        return f"{head}\n{_consolidation_status_line()}"

    @mcp.tool
    def kb_setup(rest_url: str | None = None) -> str:
        """Set up the KB Claude Code session hooks on the machine running this
        client. Returns a ready-to-apply guide plus the hook scripts and an
        idempotent installer: a flush hook (captures each session into the KB
        before compaction) and a pre-brief hook (injects relevant KB knowledge
        into new sessions). On the KB host it installs in LOCAL mode; from any
        other machine pass rest_url (your KB REST base, e.g.
        https://api.your-host) and set a KB_TOKEN. If you are running in Claude
        Code, you can write the files and run install.sh yourself.
        """
        try:
            return build_setup_guide(rest_url)
        except OSError as e:  # packaged assets unreadable — surface cleanly
            log.exception("kb_setup asset read failed")
            raise ToolError(f"setup assets unavailable: {e}") from e

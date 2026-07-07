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

log = logging.getLogger("kbmcp.setup")

ASSETS_DIR = Path(__file__).parent / "setup_assets"
_SCRIPTS = ("flush-session-to-kb.sh", "kb-session-brief.sh", "install.sh")
_PLACEHOLDER_URL = "https://api.example.com"


def _read(name: str) -> str:
    return (ASSETS_DIR / name).read_text()


def build_setup_guide(rest_url: str | None = None) -> str:
    """Assemble the install guide + embedded scripts. Pure text, no I/O beyond
    reading the packaged assets. ``rest_url`` fills the REMOTE config template."""
    url = (rest_url or _PLACEHOLDER_URL).rstrip("/")
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

## Steps (a Claude Code client can perform these directly)

1. Check `jq` is installed: `command -v jq` (else `sudo apt install jq`).
2. Write the three files below to `~/.claude/kb-setup/`.
3. **Only on a machine that is NOT the KB host** (i.e. no local KB repo): create
   `~/.claude/kb-flush.env` with the REST endpoint + a bearer token, then
   `chmod 600` it:
   ```
   KB_REST_URL={url}
   KB_TOKEN=<your KB bearer token>
   ```
   On the KB host itself, skip this — `install.sh` auto-detects LOCAL mode and
   writes the config for you.
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


def register(mcp) -> None:
    """Register the kb_setup tool on the given FastMCP instance."""

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

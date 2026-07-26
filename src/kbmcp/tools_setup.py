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
from fastmcp.server.dependencies import get_access_token, get_http_headers

from kbcore import scope
from kbcore.config import settings
from kbmcp import ANNOTATIONS_WRITE, ANNOTATIONS_WRITE_IDEMPOTENT

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


def _presented_bearer_token() -> str | None:
    """The exact bearer the CALLER presented on THIS request, or None.

    Per-project sharing makes this ECHO the caller's own token — never
    ``settings.kb_token`` unconditionally. Cases:
    - **OAuth identity** (Google, ``client_id != 'bearer'``) → None: an
      identity-authed session presented no static bearer; echoing one would
      ESCALATE it (the pre-fix bug handed such callers the MASTER key).
    - **Public master** (``client_id == 'bearer'``) or **local bearer**
      (``get_access_token()`` is None; the ASGI middleware already validated it) →
      echo the presented Authorization header. For the owner that is the master;
      for a ``kbg_`` grantee that is THEIR OWN token — disclosing nothing new.
    """
    try:
        tok = get_access_token()
    except Exception:  # outside a request context (tests calling directly)
        tok = None
    if tok is not None and getattr(tok, "client_id", "") != "bearer":
        return None
    header = get_http_headers().get("authorization", "")
    if header[:7].lower() == "bearer ":
        return header[7:].strip() or None
    return None


def build_setup_guide(rest_url: str | None = None, token: str | None = None) -> str:
    """Assemble the install guide + embedded scripts. Pure text, no I/O beyond
    reading the packaged assets.

    The REMOTE config's ``KB_REST_URL`` is filled from (in order) the ``rest_url``
    argument, the server's configured ``kb_public_rest_url``, else a generic
    placeholder. ``token`` is the caller's OWN presented bearer (see
    ``_presented_bearer_token``) — the master for the owner, a grantee's own kbg_
    token for a grantee, and None for OAuth-authed callers (who get an explanation
    + tokenless alternatives instead).
    """
    configured = (rest_url or settings.kb_public_rest_url or "").strip()
    url = (configured or _PLACEHOLDER_URL).rstrip("/")
    if token:
        remote_hint = (
            "Your bearer is embedded below (you authenticated this call with that "
            "same token, so this discloses nothing new). Write the file EXACTLY as "
            "shown and `chmod 600` it — do not echo the token elsewhere."
        )
        token_line = token
    elif configured:
        remote_hint = (
            "Your KB's REST endpoint is filled in below — you only need to add "
            "`KB_TOKEN` (the same bearer you registered the MCP server with). "
            "(Token not embedded: this call was authenticated via OAuth identity, "
            "not the bearer — use the web UI /setup page to copy it, or skip "
            "tokens entirely with CONNECTOR mode.)"
        )
        token_line = "<your KB bearer token>"
    else:
        remote_hint = (
            "Set `KB_REST_URL` to your KB's REST base (e.g. `https://api.<your-host>`) "
            "and `KB_TOKEN` to a bearer token."
        )
        token_line = "<your KB bearer token>"
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

1. Check you have a JSON tool: `command -v jq || command -v python3`. **No sudo
   needed** — the scripts fall back to `python3`, and jq (a single static binary)
   is picked up from `PATH`, from `~/.claude/hooks/jq`, or from `$KB_JQ`.
2. Write the four files below to `~/.claude/kb-setup/`.
3. **REMOTE machines only** (skip if `install.sh` reports LOCAL or MCP) — create
   `~/.claude/kb-flush.env`, then `chmod 600` it. {remote_hint}
   ```
   KB_REST_URL={url}
   KB_TOKEN={token_line}
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

    @mcp.tool(annotations=ANNOTATIONS_WRITE_IDEMPOTENT)
    def kb_consolidate() -> str:
        """Request an on-demand nightly consolidation run (dedupe, summaries,
        contradiction detection + evidence + auto-resolution, archive, re-embed).

        The run executes on the host worker (GPU) and takes MINUTES; this only
        enqueues it (deduped against an in-flight run) and returns the current
        status. Poll kb_consolidate again — or kb_stats — for completion.
        """
        from kbcore import jobs

        # Owner-only (v1): consolidation spans the WHOLE KB and spends the owner's
        # GPU/14B (Invariant 8) — a grantee cannot trigger it.
        scope.require_owner("kb_consolidate")
        req_id, created = jobs.request_consolidation("mcp")
        head = (
            f"queued consolidation request #{req_id}"
            if created
            else f"a run is already in progress (request #{req_id})"
        )
        return f"{head}\n{_consolidation_status_line()}"

    # NOT read-only despite writing nothing: kb_setup can return the caller's bearer
    # (the master for the owner), so it must keep full approval friction (a lighter,
    # read-only badge would let a prompt-injected session pull the token silently).
    @mcp.tool(annotations=ANNOTATIONS_WRITE)
    def kb_setup(rest_url: str | None = None) -> str:
        """Set up the KB Claude Code session hooks on the machine running this
        client. Returns a ready-to-apply guide plus the hook scripts and an
        idempotent installer: a flush hook (captures each session into the KB
        before compaction) and a pre-brief hook (injects relevant KB knowledge
        into new sessions). On the KB host it installs in LOCAL mode; from any
        other machine pass rest_url (your KB REST base, e.g.
        https://api.your-host). If this call is authenticated with the static
        bearer, the token is embedded in the returned config automatically
        (OAuth-authenticated calls never receive it). If you are running in
        Claude Code, you can write the files and run install.sh yourself.
        """
        try:
            return build_setup_guide(rest_url, token=_presented_bearer_token())
        except OSError as e:  # packaged assets unreadable — surface cleanly
            log.exception("kb_setup asset read failed")
            raise ToolError(f"setup assets unavailable: {e}") from e

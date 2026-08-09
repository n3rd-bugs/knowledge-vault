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
from datetime import UTC, datetime
from pathlib import Path

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token, get_http_headers

from kbcore import enrolment, scope, setup_url
from kbcore.config import settings
from kbcore.errors import InvalidOperation, KBError
from kbmcp import ANNOTATIONS_WRITE, ANNOTATIONS_WRITE_IDEMPOTENT

log = logging.getLogger("kbmcp.setup")

ASSETS_DIR = Path(__file__).parent / "setup_assets"
_SCRIPTS = (
    "flush-session-to-kb.sh",
    "kb-session-brief.sh",
    "kb-mcp-call.sh",
    # The dispatcher is the ONLY thing install.sh registers with Claude Code, so
    # the air-gapped embed is incomplete without it (a box would end up with five
    # registered hooks pointing at a file it was never given). It is ~100 lines.
    "kb-hook-dispatch.sh",
    "install.sh",
)
_PLACEHOLDER_URL = "https://api.example.com"
# MCP lives on its own host (kb.<domain>), not under the REST base.
_PLACEHOLDER_MCP_URL = "https://kb.example.com/mcp"


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


def build_setup_guide(
    rest_url: str | None = None,
    token: str | None = None,
    enrol: object | None = None,
    include_scripts: bool = False,
) -> str:
    """Assemble the install guide + embedded scripts. Pure text, no I/O beyond
    reading the packaged assets.

    The REMOTE config's ``KB_REST_URL`` is filled from (in order) the ``rest_url``
    argument, the server's configured ``kb_public_rest_url``, else a generic
    placeholder. ``token`` is the caller's OWN presented bearer (see
    ``_presented_bearer_token``) — the master for the owner, a grantee's own kbg_
    token for a grantee, and None for OAuth-authed callers (who get an explanation
    + tokenless alternatives instead).
    """
    # rest_url is a fetch-and-execute target (curl|bash at install time), so it is
    # validated + host-gated in the kbcore chokepoint, never merely .strip()'d here.
    configured = setup_url.rest_url_to_render(rest_url)
    url = configured or _PLACEHOLDER_URL
    # MCP is a DIFFERENT service on a DIFFERENT host from REST — kbrest serves only /api/* and has
    # no /mcp route at all, so deriving the MCP endpoint by appending "/mcp" to the REST base hands
    # out a URL that cannot work. The MCP base is kb_public_url (what the server advertises for
    # OAuth metadata). It is not set on every deployment path, so fall back to a labelled
    # placeholder rather than inventing one.
    mcp_base = (settings.kb_public_url or "").strip().rstrip("/")
    mcp_url = f"{mcp_base}/mcp" if mcp_base else _PLACEHOLDER_MCP_URL
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
    mcp_hint = (
        ""
        if mcp_base
        else (
            "\n   *(This server does not publish its MCP base URL, so the value "
            "above is a placeholder — replace it with your KB's MCP endpoint. "
            "It is the URL you registered with `claude mcp add`, ending in "
            "`/mcp`.)*"
        )
    )
    # A one-time enrolment code turns steps 2-4 into a single command. The code is what travels
    # through the transcript, NOT the credential: it is single use and expires in minutes, so once
    # the machine has redeemed it the transcript holds a dead string. That matters here
    # specifically because the session hooks flush transcripts into the KB, which is then backed
    # up. A code with no project cannot redeem (an owner enrolment must name the project it scopes
    # to), so emitting a bootstrap block for one advertises a command that always fails — exactly
    # what happened in the field (KB 1198).
    if enrol is not None and not getattr(enrol, "project", None):
        enrol = None
    if enrol is not None:
        mins = max(1, round((enrol.expires_at - datetime.now(UTC)).total_seconds() / 60))
        auth = (
            f"a narrow, revocable credential for project "
            f"'{getattr(enrol, 'project', None) or '?'}' (ask for another with "
            "kb_project_token as you start other projects)"
            if enrol.grants_owner
            else "a credential for THIS MACHINE carrying exactly your own grants, "
            "revocable on its own"
        )
        enrol_block = f"""
## Set this machine up (one block, nothing to copy by hand)

```bash
export KB_REST_URL={url}
export KB_ENROL_CODE={enrol.code}   # single use, expires in ~{mins} min
mkdir -p ~/.claude/kb-setup && cd ~/.claude/kb-setup
for f in install.sh flush-session-to-kb.sh kb-session-brief.sh \
         kb-mcp-call.sh notify-complete.sh turn-start.sh \
         kb-hook-dispatch.sh kb-flush-backfill.sh; do
  curl -fsS -H "Authorization: Bearer $KB_ENROL_CODE" "$KB_REST_URL/api/setup/$f" -o "$f"
done
bash install.sh
~/.claude/hooks/kb-flush-backfill.sh          # what this box never captured
~/.claude/hooks/kb-flush-backfill.sh --push   # send it
```

That fetches the hooks, redeems the code for {auth}, writes `~/.claude/kb-flush.env`
(mode 600) and registers the four hook events. The installer prints the mode it
chose and a smoke test.

If the code is refused it was already redeemed or it expired — ask for a new one
rather than reusing this message.
"""
    else:
        enrol_block = (
            "\n## One-command setup\n\n"
            "Not available here: an enrolment code must name the project this machine "
            'will capture. Ask again as `kb_setup(project="<project>")` — usually the '
            "repo you are in — and this section becomes a single copy-paste block.\n"
        )

    # The four scripts are ~23,000 tokens. Embedding them burned that much of the caller's context
    # on every single kb_setup call — including a passing "how do I set this up?" — for bytes
    # nobody reads: they are written to disk verbatim and executed. The machine fetches them
    # instead, and the full embed stays available for a box that cannot reach the KB over HTTP.
    if include_scripts:
        scripts = {name: _read(name) for name in _SCRIPTS}
        files_block = "## The files (verbatim)\n\n" + "\n\n".join(
            f"### File: `~/.claude/kb-setup/{name}`\n\n```bash\n{body.rstrip()}\n```"
            for name, body in scripts.items()
        )
    else:
        files_block = (
            "## The scripts\n\n"
            "Fetched by the command above, not reproduced here: they are ~23k tokens "
            "of shell that is written to disk and executed verbatim, so putting them "
            "in this reply would spend most of your context on bytes nobody reads.\n\n"
            "- `install.sh` — idempotent installer; detects the mode and registers the hooks\n"
            "- `flush-session-to-kb.sh` — PreCompact + SessionEnd capture\n"
            "- `kb-session-brief.sh` — SessionStart pre-brief + UserPromptSubmit recall\n"
            "- `kb-mcp-call.sh` — MCP transport helper\n"
            "- `kb-hook-dispatch.sh` — the ONLY script registered with Claude Code: "
            "one hook per event, reading the event JSON once and fanning it out "
            "(Claude Code on Windows delivers stdin to only ONE of several hooks "
            "registered on the same event)\n"
            "- `kb-flush-backfill.sh` — owner-run sweep: pushes the unflushed "
            "remainder of EVERY transcript already on the box through the flush "
            "hook (not registered as a hook; run it right after install)\n\n"
            "Each is served at `GET {url}/api/setup/<name>` and lives in the repo at "
            "`contrib/claude/kb-session-flush/`. Call `kb_setup(include_scripts=true)` "
            "to get them inline instead (for a machine with no HTTP access to the KB)."
        )
    return f"""# Install the KB Claude Code session hooks (on THIS machine)

Two hook scripts connect this machine's Claude Code sessions to the Knowledge
Vault, across **four** registered events:

- **Flush out** (`PreCompact` + `SessionEnd`): the session's new transcript delta
  is enqueued to the KB before every compaction — non-blocking, GPU-free.
- **Pre-brief in** (`SessionStart`, matcher `startup|clear`): a new session gets a
  quick KB search — seeded by the repo's recent commit subjects — injected into
  Claude's starting context, so it begins aware of prior decisions and gotchas.
- **Per-prompt recall** (`UserPromptSubmit`): each substantive prompt also gets a
  KB search seeded by the prompt itself, injected as `[KB recall · project …]`.
  It self-throttles (slash commands, very short prompts and repeated queries are
  skipped). Set `KB_PROMPT_RECALL=0` in the config to turn just this one off.

## Which mode is this machine?

`install.sh` auto-detects, and the **first match wins** — this order matters:

1. **LOCAL mode** — chosen when `$KB_DIR/pyproject.toml` exists (default
   `$HOME/data-server`) **and** `uv` is on `PATH`. No token needed; the hook
   enqueues straight into the KB repo. Writes `KB_DIR`, `UV`.
   If your checkout is elsewhere, run the installer as
   `KB_DIR=/path/to/kb-repo bash ~/.claude/kb-setup/install.sh`.
2. **MCP mode — zero config** — chosen when `~/.claude.json` holds an mcpServers
   entry whose name matches `kb`/`knowledge`, whose url ends `/mcp`, and whose
   `headers.Authorization` carries a Bearer. The installer reuses that
   registration, and the hooks call `kb_ingest_session` / `kb_search` over the
   same endpoint. Writes `KB_MCP_URL`, `KB_TOKEN`. Nothing to fill in.
3. **CONNECTOR mode — zero config** — chosen when a `claude` binary is found (on
   `PATH`, in `~/.local/bin`, `~/.claude/local`, or the VS Code extension
   bundle). For a claude.ai **OAuth connector only** machine with no static
   bearer: the claude.ai KB connector bridges into Claude Code, and the hooks
   relay through a headless `claude -p` using that cloud-held auth. Writes
   `KB_CONNECTOR=1`, `CLAUDE_BIN`.
4. **REMOTE mode** — the unconditional fallback: the hooks POST to your KB's REST
   endpoint with a bearer token. Writes `KB_REST_URL`, `KB_TOKEN`.

Two consequences worth knowing before you run it:

- **CONNECTOR outranks REMOTE.** Essentially every machine running Claude Code
  has a `claude` binary, so auto-detection will pick CONNECTOR, not REMOTE. If
  you want bearer REMOTE mode you must **write `~/.claude/kb-flush.env` yourself
  before running the installer** (step 3) — the installer keeps an existing
  config and never overwrites or upgrades one.
- **The mode is sticky.** Detection only runs when `~/.claude/kb-flush.env` does
  not exist. To change modes later, edit or delete that file and re-run.

CONNECTOR is zero-config but not free: the model is the data path, so the
transcript relays in ~24 KB chunks through a headless `claude -p`, and each
flush spends Claude usage. Bearer modes (MCP/REMOTE) are the efficient path for
heavy sessions.

{enrol_block}
## Steps (a Claude Code client can perform these directly)

1. Check you have a JSON tool: `command -v jq || command -v python3`. **No sudo
   needed** — the scripts fall back to `python3`, and jq (a single static binary)
   is picked up from `PATH`, from `~/.claude/hooks/jq`, or from `$KB_JQ`.
2. Write the four files below to `~/.claude/kb-setup/`.
3. **Only if you want a specific mode** — write `~/.claude/kb-flush.env` first,
   then `chmod 600` it. Skip this entirely to let step 4 auto-detect.

   **REMOTE** (a self-hosted KB over REST). {remote_hint}
   ```
   KB_REST_URL={url}
   KB_TOKEN={token_line}
   ```

   **MCP** (the same KB, over its MCP endpoint — note this is a DIFFERENT host
   from the REST base above; the REST service has no `/mcp` route):
   ```
   KB_MCP_URL={mcp_url}
   KB_TOKEN={token_line}
   ```{mcp_hint}

   **CONNECTOR** (no token at all; relays through the claude.ai KB connector):
   ```
   KB_CONNECTOR=1
   CLAUDE_BIN=/absolute/path/to/claude
   # KB_CONNECTOR_CHUNK=24000          # bytes per chunk = ONE model call
   # KB_CONNECTOR_MAX=240000           # bytes per mid-session firing
   # KB_CONNECTOR_SESSION_MAX=2000000  # absolute ceiling per firing
   ```
   **CONNECTOR does not scale to large sessions.** It spends one headless
   `claude -p` call per chunk, so a 300 MB transcript would be ~13,000 model
   calls. Above `KB_CONNECTOR_SESSION_MAX` only the most RECENT bytes are
   relayed and the rest is dropped with a loud `NOT CAPTURED` line naming the
   transcript so you can re-ingest it. If your sessions are large, use a bearer
   transport (MCP or REMOTE) — it chunks at 900 KB with no model in the path.

   **LOCAL** (this machine IS the KB host):
   ```
   KB_DIR=/absolute/path/to/the/kb/repo
   UV=/absolute/path/to/uv
   ```
4. Run the installer (idempotent; registers all four events in
   `~/.claude/settings.json`, preserving any other hooks you have):
   ```bash
   bash ~/.claude/kb-setup/install.sh
   ```
   It prints the mode it chose, the JSON backend it will use, and an exact smoke
   test. On Windows/Git Bash it registers `bash.exe` as the command with the
   script as an argument, because Claude Code there spawns hooks via
   CreateProcess and cannot execute a bare `.sh`.
5. Smoke test — run the commands the installer printed, then check
   `~/.claude/hooks/flush-session.log`. **The marker depends on the mode:**

   | Mode | Success line |
   |------|--------------|
   | LOCAL | `local <project> ok bytes=N (offset N/N)` |
   | MCP **or** REMOTE | `flush <project> ok bytes=N (offset X/Y)` |
   | CONNECTOR | `connector <project> ok bytes=N (offset X/Y)` |

   `ok` means the delta drained fully; `partial … — will resume` means the rest
   goes on the next firing (normal for a big transcript in CONNECTOR mode). New
   sessions then start with a `[KB pre-brief · project …]` block.

## Which project a session files under

The **git root's** directory name, slugified — a session started in a subdirectory
files under the repo, not the subdirectory. To name a workspace explicitly, put
one line in `.kbproject` at the repo root (e.g. `punch-rescue`); it is slugified
the same way, so it can only ever become `[a-z0-9-]`. Each project needs its own
token (`kb_project_token`), and the hooks say `NEEDS TOKEN` naming the exact call
when one is missing.

## If it does not work

Every failure is written to `~/.claude/hooks/flush-session.log`; the hooks always
exit 0 so they can never block Claude Code, which means **the log is the only
place a problem shows up**. What to look for:

- `NOT CONFIGURED: no transport in …` — no mode is configured; the session was
  **not** captured. Write `~/.claude/kb-flush.env` (step 3) and re-run step 4.
- `NOT CONFIGURED: no JSON tool …` — neither jq nor python3 was found. Drop a
  static `jq` in `~/.claude/hooks/`, or set `KB_JQ=/path/to/jq` in the config.
- `chunk FAILED (401)` / `(403)` — the bearer is wrong or lacks write access to
  that project. `(000)` means the endpoint was unreachable at all.
- `chunk FAILED (…)` in CONNECTOR mode — the relay could not reach the KB tool;
  confirm the KB connector is enabled for your claude.ai account.
- `NOT CAPTURED <n> bytes` — the session was too large for CONNECTOR mode and
  those bytes were dropped (not queued, not resumable). The line names the
  transcript file; re-ingest it, or switch to a bearer transport.
- Nothing at all in the log — the hooks are not registered. Check that
  `~/.claude/settings.json` lists all four events, and re-run the installer.

A failed chunk never advances the offset, so nothing is lost: the next firing
re-sends from exactly where it stopped.

To disable later: remove the entries from `~/.claude/settings.json` `hooks`, or
delete `~/.claude/kb-flush.env` (the hooks no-op without config, apart from one
rate-limited `NOT CONFIGURED` line so the silence is never mistaken for success).

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
    def kb_setup(
        rest_url: str | None = None,
        project: str | None = None,
        include_scripts: bool = False,
    ) -> str:
        """Set up the KB Claude Code session hooks on the machine running this
        client. Returns a ready-to-apply guide plus the hook scripts and an
        idempotent installer: a flush hook (captures each session into the KB
        before compaction) and a pre-brief hook (injects relevant KB knowledge
        into new sessions). On the KB host it installs in LOCAL mode; from any
        other machine pass rest_url (your KB REST base, e.g.
        https://api.your-host).

        Setup is one command: the guide carries a ONE-TIME ENROLMENT CODE that
        the installer exchanges for this machine's own credential, so no bearer
        is ever pasted by hand. The code is single-use and expires in minutes —
        it is safe to emit into a conversation precisely because it stops being
        a credential the moment it is used. A caller with no access to enrol
        anything gets the manual guide instead. If this call is authenticated
        with the static bearer, that token is also embedded directly.

        Pass `project` to scope the machine's first credential (usually the repo
        you are in). The machine gets a NARROW, revocable token for that project
        — never the master key — and can request more later with
        kb_project_token as it starts working in other projects.

        The reply does NOT include the hook scripts: they are ~23k tokens that
        get written to disk and executed verbatim, and the bootstrap command
        fetches them. Pass include_scripts=true only for a machine that cannot
        reach the KB over HTTP.
        """
        # VALIDATE BEFORE MINTING. rest_url is a fetch-and-execute target, and build_setup_guide
        # used to be the first thing to reject a bad one — AFTER a single-use enrolment code had
        # been issued and burnt. Five burns disable the issuer, so a fat-fingered URL could lock
        # enrolment out; the kbcore chokepoint is asked first and writes nothing.
        try:
            setup_url.rest_url_to_render(rest_url)
        except KBError as e:
            raise ToolError(str(e)) from e
        # Issue an enrolment code automatically for any caller that HAS authority
        # to enrol something. It is safe to emit unprompted precisely because it
        # is not a credential: single use, minutes long, and useless once
        # redeemed. A caller with no grants gets no code and the manual guide.
        issued = None
        try:
            issued = enrolment.create_code(note="kb_setup", project=project)
        except (InvalidOperation, KBError) as exc:
            log.info("no enrolment code issued: %s", exc)
        except Exception:  # noqa: BLE001 — enrolment must never break setup
            log.warning("enrolment code issue failed; returning the manual guide", exc_info=True)
        try:
            return build_setup_guide(
                rest_url,
                token=_presented_bearer_token(),
                enrol=issued,
                include_scripts=include_scripts,
            )
        except OSError as e:  # packaged assets unreadable — surface cleanly
            log.exception("kb_setup asset read failed")
            raise ToolError(f"setup assets unavailable: {e}") from e
        except KBError as e:  # e.g. an invalid rest_url (a fetch-and-execute target)
            raise ToolError(str(e)) from e

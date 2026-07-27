"""kb_setup MCP tool — install guide for the Claude Code session hooks."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kbmcp import tools_setup

CONTRIB = Path(__file__).resolve().parents[1] / "contrib" / "claude" / "kb-session-flush"


@pytest.fixture(autouse=True)
def _no_configured_rest_url(monkeypatch):
    """Hermetic default: ignore the machine's real .env KB_PUBLIC_REST_URL so
    the placeholder-path tests are deterministic (the fallback test sets it)."""
    from kbcore.config import settings

    monkeypatch.setattr(settings, "kb_public_rest_url", "")


def test_setup_assets_match_contrib():
    """Single-source guard for the KB hook assets.

    contrib/claude/kb-session-flush/ is CANONICAL; src/kbmcp/setup_assets/ is the
    shipped package-data copy (the container ships src/ only, so kb_setup reads the
    packaged copies). Keep them in lockstep with `make sync-assets` (copies contrib
    -> setup_assets). This asserts the full FILE SET matches (so an added/removed
    file is caught — SKILL.md is package data outside tools_setup._SCRIPTS and used
    to escape this guard) AND every file is byte-identical.
    """
    contrib_files = sorted(p.name for p in CONTRIB.iterdir() if p.is_file())
    shipped_files = sorted(p.name for p in tools_setup.ASSETS_DIR.iterdir() if p.is_file())
    assert shipped_files == contrib_files, (
        f"asset file set drifted (contrib={contrib_files} shipped={shipped_files}) "
        "— run `make sync-assets`"
    )
    for name in contrib_files:
        shipped = (tools_setup.ASSETS_DIR / name).read_bytes()
        canonical = (CONTRIB / name).read_bytes()
        assert shipped == canonical, f"{name} drifted from contrib — run `make sync-assets`"


def test_guide_embeds_all_scripts_when_asked():
    """include_scripts=True is the air-gapped path: a machine that cannot reach
    the KB over HTTP still needs the four scripts verbatim."""
    guide = tools_setup.build_setup_guide(include_scripts=True)
    # both hook behaviors named
    assert "PreCompact" in guide and "SessionStart" in guide
    # every script embedded (a distinctive line from each of the four)
    assert "flush this session's NEW transcript" in guide  # flush hook
    assert "per-prompt recall" in guide  # brief/recall hook (SessionStart + UserPromptSubmit)
    assert "Minimal MCP streamable-HTTP client" in guide  # kb-mcp-call.sh helper
    assert "reuses your 'claude mcp' KB registration" in guide  # install.sh MCP branch
    # actionable install + smoke test
    assert "bash ~/.claude/kb-setup/install.sh" in guide
    assert "flush-session.log" in guide
    # no site-specific host leaked; generic placeholder present
    assert "n3rd-bugs" not in guide
    assert "https://api.example.com" in guide


def test_guide_explains_mcp_zero_config_mode():
    guide = tools_setup.build_setup_guide()
    assert "MCP mode" in guide and "zero config" in guide
    assert "kb_ingest_session" in guide  # the tool the MCP-mode flush hook calls


def test_guide_substitutes_rest_url():
    guide = tools_setup.build_setup_guide(rest_url="https://api.myhost.internal/")
    # the step-3 config block uses the caller's URL (trailing slash trimmed).
    # (install.sh's own embedded REMOTE stub keeps its generic placeholder — the
    # guide tells users to write kb-flush.env first, which install.sh preserves.)
    assert "KB_REST_URL=https://api.myhost.internal" in guide


def test_guide_falls_back_to_configured_public_rest_url(monkeypatch):
    from kbcore.config import settings

    monkeypatch.setattr(settings, "kb_public_rest_url", "https://api.configured.host")
    guide = tools_setup.build_setup_guide()  # no arg -> use the configured base
    assert "KB_REST_URL=https://api.configured.host" in guide
    assert "you only need to add" in guide  # "token only" hint when pre-filled
    # explicit arg still overrides the configured default
    g2 = tools_setup.build_setup_guide(rest_url="https://api.override.host")
    assert "KB_REST_URL=https://api.override.host" in g2


def test_guide_explains_oauth_only_connector_mode():
    guide = tools_setup.build_setup_guide()
    assert "OAuth connector only" in guide
    assert "CONNECTOR mode" in guide and "zero config" in guide
    assert "spends Claude usage" in guide  # the honest cost caveat


def test_kb_setup_registered_and_callable():
    from fastmcp import Client, FastMCP

    mcp = FastMCP(name="kb-test")
    tools_setup.register(mcp)

    async def run():
        async with Client(mcp) as client:
            tools = await client.list_tools()
            assert any(t.name == "kb_setup" for t in tools)
            res = await client.call_tool("kb_setup", {"rest_url": "https://api.x.test"})
            return res.content[0].text

    text = asyncio.run(run())
    assert "install.sh" in text and "https://api.x.test" in text


# --- auth-gated token embedding ------------------------------------------------------


def test_guide_embeds_token_when_provided():
    guide = tools_setup.build_setup_guide(token="kbtest-" + "9" * 32)
    assert "KB_TOKEN=kbtest-" + "9" * 32 in guide
    assert "discloses nothing new" in guide  # the rationale is stated
    assert "<your KB bearer token>" not in guide


def test_guide_withholds_token_by_default(monkeypatch):
    from kbcore.config import settings

    monkeypatch.setattr(settings, "kb_public_rest_url", "https://api.demo.test")
    guide = tools_setup.build_setup_guide()  # no token arg
    assert "KB_TOKEN=<your KB bearer token>" in guide
    assert "OAuth identity" in guide  # explains why + points at alternatives


def test_kb_setup_gates_token_on_caller_principal(kb, monkeypatch):
    """kb_setup echoes the caller's OWN presented bearer: the master for the owner,
    a grantee's own kbg_ token for a grantee (NEVER the master — escalation guard),
    and nothing for OAuth callers (no static bearer to echo)."""
    import asyncio

    from fastmcp import Client, FastMCP

    from kbcore.config import settings

    master = "kbtest-" + "7" * 32
    grantee = "kbg_" + "a" * 43
    monkeypatch.setattr(settings, "kb_token", master)

    class _Tok:
        def __init__(self, cid):
            self.client_id = cid

    def run_with(principal, header):
        monkeypatch.setattr(tools_setup, "get_access_token", lambda: principal)
        monkeypatch.setattr(
            tools_setup,
            "get_http_headers",
            lambda *a, **k: {"authorization": header} if header else {},
        )
        mcp = FastMCP(name="kb-test")
        tools_setup.register(mcp)

        async def call():
            async with Client(mcp) as c:
                res = await c.call_tool("kb_setup", {})
                return res.content[0].text

        return asyncio.run(call())

    # local instance (FastMCP auth off -> None): the presented master header is echoed
    assert f"KB_TOKEN={master}" in run_with(None, f"Bearer {master}")
    # public static-bearer principal (client_id 'bearer'): the master is echoed
    assert f"KB_TOKEN={master}" in run_with(_Tok("bearer"), f"Bearer {master}")
    # grantee on the local/bearer surface: gets THEIR OWN token, never the master
    g = run_with(None, f"Bearer {grantee}")
    assert f"KB_TOKEN={grantee}" in g
    assert master not in g, "master key must NEVER be handed to a grantee (escalation)"
    # OAuth principal: withheld regardless of any header, with the explanation
    oauth_guide = run_with(_Tok("google-oauth-client-abc"), "Bearer some-google-access-token")
    assert master not in oauth_guide
    assert "OAuth" in oauth_guide


def test_scrub_redacts_own_kb_token(kb, monkeypatch):
    from kbcore.config import settings
    from kbcore.scrub import scrub

    monkeypatch.setattr(settings, "kb_token", "kbtest-" + "5" * 32)
    clean, red = scrub(
        f"the config was KB_TOKEN={settings.kb_token} and also bare "
        f"{settings.kb_token} mid-sentence"
    )
    assert settings.kb_token not in clean
    assert red.get("kb_token", 0) >= 2, "bare occurrences redacted too"


# --------------------------------------------------------------------------
# Drift guards: the guide is DOCUMENTATION FOR CODE, and it silently rotted.
# For a long time it told operators to verify a fresh install by looking for
# `mcp … ok` / `remote … http=202` in flush-session.log. No hook has ever
# written either string, so anyone who followed the instructions grepped for a
# marker that cannot appear and concluded a working install had failed. The
# assertions below tie every claim the guide makes back to the shipped scripts,
# so the next such divergence fails a test instead of a user's setup.
# --------------------------------------------------------------------------

FLUSH_HOOK = (CONTRIB / "flush-session-to-kb.sh").read_text()
INSTALL_SH = (CONTRIB / "install.sh").read_text()


def _prose(guide: str) -> str:
    """The guide's own text, excluding any verbatim script source.

    The scripts are only embedded when include_scripts=True; in the default lean
    reply there is nothing to strip. Either way, assertions about what the GUIDE
    claims must not accidentally match a script's own comments.
    """
    head, sep, _ = guide.partition("### File: ")
    return head if sep else guide


def _emitted_log_lines(script: str) -> str:
    """Only the lines a script actually WRITES to the log (not its comments)."""
    return "\n".join(ln for ln in script.splitlines() if 'echo "$(date -Is)' in ln)


def test_guide_covers_every_transport_with_its_config_keys():
    """All four transports, each with the keys its config block actually needs.

    A mode documented without its keys is unusable: auto-detection is skipped
    whenever ~/.claude/kb-flush.env already exists, so writing that file by hand
    is the ONLY way to choose a mode, and the keys must therefore be in reach.
    """
    guide = _prose(tools_setup.build_setup_guide())
    required = {
        "LOCAL": ["KB_DIR", "UV"],
        "MCP": ["KB_MCP_URL", "KB_TOKEN"],
        "CONNECTOR": ["KB_CONNECTOR=1", "CLAUDE_BIN"],
        "REMOTE": ["KB_REST_URL", "KB_TOKEN"],
    }
    for mode, keys in required.items():
        assert mode in guide, f"{mode} mode is never named in the setup guide"
        for key in keys:
            assert key in guide, f"{mode} mode is documented without its {key} config key"


def test_guide_documents_all_four_registered_events():
    """The guide must describe every event the installer registers.

    UserPromptSubmit went undocumented for a long time: the guide said three
    events while the installer registered four, so per-prompt recall injected KB
    hits into context without ever being mentioned to the operator.
    """
    guide = _prose(tools_setup.build_setup_guide())
    events = ["PreCompact", "SessionEnd", "SessionStart", "UserPromptSubmit"]
    for ev in events:
        assert f'"{ev}"' in INSTALL_SH or f".hooks.{ev}" in INSTALL_SH or f"'{ev}'" in INSTALL_SH, (
            f"{ev} is no longer registered by install.sh — update this guard and the guide"
        )
        assert ev in guide, f"install.sh registers {ev} but the setup guide never mentions it"


def test_guide_success_markers_are_ones_the_hook_actually_writes():
    """Every log marker the guide tells the operator to expect must be real.

    The marker is the ONLY signal a hook gives: it always exits 0 so it can never
    block Claude Code. Documenting an unreachable marker turns a healthy install
    into an apparently broken one.
    """
    prose = _prose(tools_setup.build_setup_guide())
    emitted_lines = _emitted_log_lines(FLUSH_HOOK)
    # (documented marker, the emitting line in the shipped hook)
    for marker, emitted in [
        ("local <project> ok bytes=", "local $PROJECT ok bytes="),
        ("flush <project> ok bytes=", "flush $PROJECT ok bytes="),
        ("connector <project> ok bytes=", "connector $PROJECT ok bytes="),
    ]:
        assert marker in prose, f"the guide no longer documents the {marker!r} marker"
        assert emitted in emitted_lines, (
            f"the guide promises {marker!r} but no hook line emits {emitted!r}"
        )


def test_guide_never_promises_a_marker_no_hook_emits():
    """The specific regression: `http=` and a `remote …` log tag are fiction.

    MCP and REMOTE share ONE branch in the flush hook and both log the tag
    `flush`; no line anywhere writes an HTTP status as `http=NNN`.
    """
    prose = _prose(tools_setup.build_setup_guide())
    emitted = _emitted_log_lines(FLUSH_HOOK)
    assert "http=" not in prose, "the guide promises an http= marker the hooks never write"
    assert "http=" not in emitted, (
        "a hook line started emitting http= — update the guide, this guard, and "
        "the pre-brief failure scanner together"
    )
    assert "remote …" not in prose and "remote ..." not in prose, (
        "the guide names a 'remote' log tag; MCP and REMOTE both log 'flush'"
    )


def test_guide_states_connector_outranks_remote():
    """Auto-detection picks CONNECTOR before REMOTE whenever a claude binary
    exists — which is nearly every machine running Claude Code. An operator who
    wants bearer REMOTE mode must be told to write the config FIRST, or they
    will never get the mode they asked for."""
    guide = _prose(tools_setup.build_setup_guide())
    assert guide.index("CONNECTOR mode") < guide.index("REMOTE mode"), (
        "the guide lists REMOTE before CONNECTOR, contradicting the installer's priority"
    )
    assert "CONNECTOR outranks REMOTE" in guide
    # And the installer really does order them that way: connector branch first.
    assert INSTALL_SH.index("KB_CONNECTOR=1") < INSTALL_SH.index("KB_REST_URL=https://"), (
        "install.sh no longer prefers CONNECTOR over REMOTE — the guide now lies"
    )


def test_guide_documents_the_failure_signals():
    """A setup guide that cannot explain failure is half a guide: the hooks are
    silent by design, so the operator needs the exact strings to look for."""
    prose = _prose(tools_setup.build_setup_guide())
    for signal in ["NOT CONFIGURED", "chunk FAILED", "flush-session.log"]:
        assert signal in prose, f"the guide never mentions the {signal!r} failure signal"
        assert signal in FLUSH_HOOK, f"{signal!r} is documented but no longer emitted"


def test_installer_smoke_hint_matches_what_the_hook_emits():
    """install.sh prints its own verification hint — a third copy of the same
    claim, and it carried the same fiction (`mcp … ok | remote … http=202`).
    Whatever it tells the operator to expect must be a marker the flush hook
    really writes."""
    hint = "\n".join(
        ln for ln in INSTALL_SH.splitlines() if "expect" in ln or "flush-session.log" in ln
    )
    assert hint, "install.sh no longer prints a smoke-test hint"
    assert "http=" not in hint, "install.sh promises an http= marker no hook emits"
    emitted = _emitted_log_lines(FLUSH_HOOK)
    for tag in ["local", "flush", "connector"]:
        assert f"{tag} <proj> ok bytes=" in INSTALL_SH, (
            f"install.sh's hint no longer names the {tag!r} success marker"
        )
        assert f"{tag} $PROJECT ok bytes=" in emitted, (
            f"install.sh promises a {tag!r} marker the hook does not emit"
        )


def test_mcp_url_is_not_derived_from_the_rest_base(monkeypatch):
    """MCP and REST are different services on different hosts.

    kbrest serves only `/api/*` and has no `/mcp` route, so building the MCP
    endpoint by appending "/mcp" to the REST base hands the operator a URL that
    cannot work. The MCP base is `kb_public_url` — what the server advertises
    for its own OAuth metadata.
    """
    from kbcore.config import settings

    monkeypatch.setattr(settings, "kb_public_rest_url", "https://api.example.org")
    monkeypatch.setattr(settings, "kb_public_url", "https://kb.example.org")
    guide = _prose(tools_setup.build_setup_guide())
    assert "KB_MCP_URL=https://kb.example.org/mcp" in guide
    assert "KB_REST_URL=https://api.example.org" in guide
    assert "KB_MCP_URL=https://api.example.org/mcp" not in guide, (
        "the MCP endpoint was derived from the REST base — that host has no /mcp route"
    )


def test_mcp_url_falls_back_to_a_LABELLED_placeholder(monkeypatch):
    """Not every deployment path publishes an MCP base URL (it is set on the
    public MCP container but not the local one). When it is absent the guide
    must say the value is a placeholder rather than present a fabricated URL as
    if it were this server's real endpoint."""
    from kbcore.config import settings

    monkeypatch.setattr(settings, "kb_public_rest_url", "https://api.example.org")
    monkeypatch.setattr(settings, "kb_public_url", "")
    guide = _prose(tools_setup.build_setup_guide())
    assert "KB_MCP_URL=https://api.example.org/mcp" not in guide
    assert "placeholder" in guide, "an unknown MCP URL is presented without warning"


def test_rest_route_prefix_still_matches_what_the_guide_implies():
    """The REMOTE config takes a BASE url; the hook appends the path itself.
    If kbrest ever stopped serving /api/ingest-session, the documented base
    would silently become wrong."""
    rest = (Path(__file__).resolve().parents[1] / "src" / "kbrest" / "server.py").read_text()
    assert '"/api/ingest-session"' in rest, (
        "the REST ingest path moved — the guide's base URL is now wrong"
    )
    assert '"/mcp"' not in rest, "kbrest now serves /mcp — revisit the MCP URL derivation"


# --------------------------------------------------------------------------
# Context cost. kb_setup's reply lands in the caller's context window, so its
# SIZE is part of its contract: embedding the four scripts spent ~23,000 tokens
# — 91% of the reply — on bytes that are written to disk and executed without
# anyone reading them. That was paid on every call, including a passing "how do
# I set this up?".
# --------------------------------------------------------------------------

_CHARS_PER_TOKEN = 4  # rough, and deliberately conservative for a budget check


def test_default_reply_is_small_enough_to_ask_casually():
    guide = tools_setup.build_setup_guide()
    approx_tokens = len(guide) // _CHARS_PER_TOKEN
    assert approx_tokens < 4_000, (
        f"the default kb_setup reply is ~{approx_tokens} tokens; it must stay small "
        "enough that asking how to set up the system does not consume the context "
        "needed to actually do it"
    )


def test_default_reply_does_not_embed_the_scripts():
    guide = tools_setup.build_setup_guide()
    assert "### File: " not in guide
    # A distinctive line from each script must be absent from the lean reply.
    for needle in (
        "flush this session's NEW transcript",
        "Minimal MCP streamable-HTTP client",
    ):
        assert needle not in guide, f"script body leaked into the lean reply: {needle!r}"


def test_the_lean_reply_still_says_where_the_scripts_come_from():
    """Leaving them out is only acceptable if the reply says how to get them."""
    guide = _prose(tools_setup.build_setup_guide())
    assert "/api/setup/" in guide, "the lean reply never says where to fetch the scripts"
    assert "include_scripts" in guide, "no way to discover the inline alternative"
    for name in ("install.sh", "flush-session-to-kb.sh", "kb-session-brief.sh", "kb-mcp-call.sh"):
        assert name in guide, f"{name} is not named in the lean reply"


def test_embedding_is_still_available_and_is_the_expensive_one():
    lean = tools_setup.build_setup_guide()
    full = tools_setup.build_setup_guide(include_scripts=True)
    assert len(full) > 5 * len(lean), "include_scripts no longer embeds anything"
    assert "### File: " in full

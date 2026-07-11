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


def test_guide_embeds_all_scripts_and_steps():
    guide = tools_setup.build_setup_guide()
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
    """Bearer/local principals get the token; OAuth principals never do."""
    import asyncio

    from fastmcp import Client, FastMCP

    from kbcore.config import settings

    monkeypatch.setattr(settings, "kb_token", "kbtest-" + "7" * 32)

    class _Tok:
        def __init__(self, cid):
            self.client_id = cid

    def run_with(principal):
        monkeypatch.setattr(tools_setup, "get_access_token", lambda: principal)
        mcp = FastMCP(name="kb-test")
        tools_setup.register(mcp)

        async def call():
            async with Client(mcp) as c:
                res = await c.call_tool("kb_setup", {})
                return res.content[0].text

        return asyncio.run(call())

    # local instance (FastMCP auth off -> None) and static-bearer principal: embedded
    assert "KB_TOKEN=kbtest-" + "7" * 32 in run_with(None)
    assert "KB_TOKEN=kbtest-" + "7" * 32 in run_with(_Tok("bearer"))
    # OAuth principal: withheld, with the explanation
    oauth_guide = run_with(_Tok("google-oauth-client-abc"))
    assert "kbtest-" + "7" * 32 not in oauth_guide
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

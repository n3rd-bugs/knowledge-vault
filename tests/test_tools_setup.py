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
    """The shipped package-data scripts must stay byte-identical to contrib —
    the container ships src/ only, so the tool reads the packaged copies."""
    for name in tools_setup._SCRIPTS:
        shipped = (tools_setup.ASSETS_DIR / name).read_bytes()
        canonical = (CONTRIB / name).read_bytes()
        assert shipped == canonical, f"{name} drifted from contrib — re-copy it"


def test_guide_embeds_all_three_scripts_and_steps():
    guide = tools_setup.build_setup_guide()
    # both hook behaviors named
    assert "PreCompact" in guide and "SessionStart" in guide
    # every script embedded (a distinctive line from each)
    assert "flush this session's NEW transcript" in guide  # flush hook
    assert "pre-brief a NEW session" in guide  # brief hook
    assert "registers PreCompact + SessionEnd" in guide.replace("Session\nStart", "SessionStart")
    # actionable install + smoke test
    assert "bash ~/.claude/kb-setup/install.sh" in guide
    assert "flush-session.log" in guide
    # no site-specific host leaked; generic placeholder present
    assert "n3rd-bugs" not in guide
    assert "https://api.example.com" in guide


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


def test_guide_explains_oauth_only_dead_end():
    guide = tools_setup.build_setup_guide()
    assert "OAuth connector only" in guide
    assert "not applicable" in guide


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

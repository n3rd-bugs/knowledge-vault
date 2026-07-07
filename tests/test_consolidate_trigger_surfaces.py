"""REST + MCP + CLI surfaces for the on-demand consolidation trigger."""

from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from kbcore import db, jobs
from kbcore.config import settings

TEST_TOKEN = "kbtest-" + "0" * 32
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _clean(kb):
    with db.transaction() as conn:
        conn.execute("DELETE FROM consolidation_requests")


@pytest.fixture()
def rest(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbrest.server import build_app

    return TestClient(build_app())


# --- REST ---------------------------------------------------------------------------


def test_rest_consolidate_enqueues_and_dedupes(rest):
    r = rest.post("/api/consolidate", headers=AUTH)
    assert r.status_code == 202
    body = r.json()
    assert body["created"] is True and isinstance(body["request_id"], int)
    # second click while pending → same request, created False
    r2 = rest.post("/api/consolidate", headers=AUTH)
    assert r2.json()["created"] is False and r2.json()["request_id"] == body["request_id"]
    # status endpoint reflects it
    st = rest.get("/api/consolidate", headers=AUTH).json()["status"]
    assert st["status"] == "pending" and st["requested_by"] == "rest"


def test_rest_consolidate_requires_auth(rest):
    assert rest.post("/api/consolidate").status_code == 401
    assert rest.get("/api/consolidate").status_code == 401


def test_rest_cors_preflight_allows_web_origin(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    monkeypatch.setattr(settings, "kb_web_public_url", "https://web.demo.test")
    from kbrest.server import build_app

    c = TestClient(build_app())
    # preflight carries no bearer — CORS must answer it before the auth check
    r = c.options(
        "/api/consolidate",
        headers={
            "Origin": "https://web.demo.test",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "https://web.demo.test"


# --- MCP --------------------------------------------------------------------------


def test_mcp_kb_consolidate(kb):
    from fastmcp import Client, FastMCP

    from kbmcp import tools_setup

    mcp = FastMCP(name="kb-test")
    tools_setup.register(mcp)

    async def run():
        async with Client(mcp) as client:
            names = {t.name for t in await client.list_tools()}
            assert "kb_consolidate" in names
            res = await client.call_tool("kb_consolidate", {})
            return res.content[0].text

    text = asyncio.run(run())
    assert "queued consolidation request" in text
    assert jobs.latest_consolidation_status()["requested_by"] == "mcp"


# --- CLI --------------------------------------------------------------------------


def test_cli_consolidate_now_and_status(kb):
    from typer.testing import CliRunner

    from kbcli.main import app

    runner = CliRunner()
    r = runner.invoke(app, ["consolidate-now"])
    assert r.exit_code == 0 and "queued consolidation request" in r.output
    r2 = runner.invoke(app, ["consolidate-status"])
    assert r2.exit_code == 0 and "cli" in r2.output and "pending" in r2.output

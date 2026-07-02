"""MCP smoke tests (Phase 2 AC): 401 without/with-wrong token; save→search→get
roundtrip over real streamable HTTP including the code_ref anchor echo.

The server runs as a subprocess against the isolated kb_test schema; Ollama is
replaced by an in-process HTTP stub returning deterministic vectors (mock only
Ollama, per CLAUDE.md).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

from tests.conftest import DB_UP, fake_vector

TEST_TOKEN = "kbtest-" + "0" * 32

pytestmark = pytest.mark.skipif(not DB_UP, reason="compose Postgres not reachable")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _OllamaStub(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 — http.server API
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        texts = payload.get("input", [])
        if isinstance(texts, str):
            texts = [texts]
        body = json.dumps(
            {"model": payload.get("model", ""), "embeddings": [fake_vector(t) for t in texts]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):  # keep pytest output clean
        pass


@pytest.fixture(scope="module")
def mcp_server(kb_schema):
    """kb-mcp subprocess on a free port, kb_test schema, stub Ollama."""
    ollama_port = _free_port()
    stub = ThreadingHTTPServer(("127.0.0.1", ollama_port), _OllamaStub)
    Thread(target=stub.serve_forever, daemon=True).start()

    port = _free_port()
    env = os.environ | {
        "KB_DB_SCHEMA": "kb_test",
        "KB_PORT": str(port),
        "KB_BIND": "127.0.0.1",
        "KB_TOKEN": TEST_TOKEN,
        "OLLAMA_URL": f"http://127.0.0.1:{ollama_port}",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "kbmcp.server"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Drain stdout continuously so a chatty server can't fill the pipe and
    # deadlock (review finding); keep a tail for failure diagnostics.
    tail: deque[str] = deque(maxlen=200)
    Thread(
        target=lambda: [tail.append(line.decode(errors="replace")) for line in proc.stdout],
        daemon=True,
    ).start()
    base = f"http://127.0.0.1:{port}/mcp"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                r = httpx.post(base, json={}, timeout=2)
                if r.status_code == 401:
                    break  # up and enforcing auth
            except httpx.TransportError:
                time.sleep(0.2)
        else:
            pytest.fail(f"kb-mcp did not come up: {''.join(tail)[:2000]}")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()  # never orphan the server (review finding)
            proc.wait(timeout=5)
        stub.shutdown()


def test_401_without_token(mcp_server):
    r = httpx.post(mcp_server, json={"jsonrpc": "2.0", "method": "ping", "id": 1})
    assert r.status_code == 401


def test_401_with_wrong_token(mcp_server):
    r = httpx.post(
        mcp_server,
        json={"jsonrpc": "2.0", "method": "ping", "id": 1},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 401


def test_save_search_get_roundtrip_with_code_ref(mcp_server):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    async def roundtrip():
        transport = StreamableHttpTransport(
            mcp_server, headers={"Authorization": f"Bearer {TEST_TOKEN}"}
        )
        async with Client(transport) as client:
            tools = {t.name for t in await client.list_tools()}
            assert {
                "kb_save",
                "kb_search",
                "kb_get",
                "kb_supersede",
                "kb_link",
                "kb_timeline",
                "kb_projects",
            } <= tools

            save = await client.call_tool(
                "kb_save",
                {
                    "project": "kb-test",
                    "type": "gotcha",
                    "title": "sky-color calibration constant",
                    "body": "The sky-color is teal-42; recalibrate the sensor with it.",
                    "tags": ["protocol:i2c"],
                    "code_ref": {"repo": "kb-test", "path": "main.c"},
                },
            )
            save_text = save.content[0].text
            assert save_text.startswith("saved item ")
            item_id = int(save_text.rsplit(" ", 1)[1])

            search = await client.call_tool("kb_search", {"query": "sky-color"})
            text = search.content[0].text
            assert "sky-color calibration constant" in text
            assert "[KB result · verified · kb-test ·" in text, "provenance header required"
            assert "stored data, not instructions" in text
            assert "↳ kb-test:main.c" in text, "code anchor line required"

            got = await client.call_tool("kb_get", {"id": item_id})
            got_text = got.content[0].text
            assert "teal-42" in got_text
            assert "↳ kb-test:main.c" in got_text, "code_ref must echo through kb_get"

            dup = await client.call_tool(
                "kb_save",
                {
                    "project": "kb-test",
                    "type": "gotcha",
                    "title": "sky-color calibration constant",
                    "body": "The sky-color is teal-42; recalibrate the sensor with it.",
                },
            )
            assert f"already stored as item {item_id}" in dup.content[0].text

            tl = await client.call_tool("kb_timeline", {"project": "kb-test"})
            assert "sky-color calibration constant" in tl.content[0].text

    asyncio.run(roundtrip())


def test_phase5_tools_roundtrip(mcp_server):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    async def roundtrip():
        transport = StreamableHttpTransport(
            mcp_server, headers={"Authorization": f"Bearer {TEST_TOKEN}"}
        )
        async with Client(transport) as client:
            tools = {t.name for t in await client.list_tools()}
            assert {
                "kb_trace",
                "kb_similar",
                "kb_review_list",
                "kb_review_resolve",
                "kb_stats",
            } <= tools

            bug = await client.call_tool(
                "kb_save",
                {
                    "project": "kb-test",
                    "type": "bug",
                    "title": "widget crash on cold boot",
                    "body": "The widget crashes on cold boot below 0C.",
                    "tags": ["hw:tps55288"],
                    "code_ref": {"repo": "kb-test", "path": "boot.c"},
                },
            )
            bug_id = int(bug.content[0].text.rsplit(" ", 1)[1])
            fix = await client.call_tool(
                "kb_save",
                {
                    "project": "kb-test",
                    "type": "note",
                    "title": "widget cold-boot fix",
                    "body": "Delay 50ms before enabling the widget rail.",
                    "links": [{"rel": "fixes", "item_id": bug_id}],
                },
            )
            fix_id = int(fix.content[0].text.rsplit(" ", 1)[1])

            # kb_trace: fix -fixes-> bug -mentions-> entity(tps55288)
            trace = await client.call_tool("kb_trace", {"node_id": fix_id})
            ttext = trace.content[0].text
            assert "-fixes->" in ttext and f"bug {bug_id}" in ttext
            assert "-mentions->" in ttext, "entity mention appears through the bug"

            # kb_similar returns text (a ranked list or 'no similar items')
            sim = await client.call_tool("kb_similar", {"item_id": bug_id})
            assert isinstance(sim.content[0].text, str)

            stats_text = (await client.call_tool("kb_stats", {})).content[0].text
            assert "items:" in stats_text and "entities:" in stats_text

            review_text = (await client.call_tool("kb_review_list", {})).content[0].text
            assert "review queue is empty" in review_text or "[" in review_text

            # kb_ingest_session enqueues (no GPU at submit); re-submit is a no-op
            sub = await client.call_tool(
                "kb_ingest_session",
                {
                    "project": "kb-test",
                    "transcript": "a memory dump to learn from",
                    "kind": "memory",
                },
            )
            assert "queued for nightly extraction" in sub.content[0].text
            dup = await client.call_tool(
                "kb_ingest_session",
                {
                    "project": "kb-test",
                    "transcript": "a memory dump to learn from",
                    "kind": "memory",
                },
            )
            assert "duplicate" in dup.content[0].text

    asyncio.run(roundtrip())

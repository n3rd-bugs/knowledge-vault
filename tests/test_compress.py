"""Compressed transcript submission (kbcore.compress + REST/MCP round-trips)."""

from __future__ import annotations

import base64
import gzip
import zlib

import pytest
from starlette.testclient import TestClient

from kbcore import compress, db
from kbcore.config import settings
from kbcore.errors import InvalidOperation

TEST_TOKEN = "kbtest-" + "0" * 32
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


def _gz64(text: str) -> str:
    return base64.b64encode(gzip.compress(text.encode())).decode()


# --- decode guards -------------------------------------------------------------------


def test_round_trip():
    text = "User: héllo\nAssistant: wörld\n" * 100
    assert compress.decode_transcript_gz(_gz64(text)) == text


def test_rejects_bad_base64_and_bad_gzip():
    with pytest.raises(InvalidOperation, match="base64"):
        compress.decode_transcript_gz("not!!base64@@")
    with pytest.raises(InvalidOperation, match="gzip"):
        compress.decode_transcript_gz(base64.b64encode(b"plain bytes not gzip").decode())


def test_rejects_zip_bomb():
    # 60 MB of zeros compresses to ~60 KB — must be rejected at the row limit,
    # mid-stream, without materializing the expansion.
    bomb = base64.b64encode(zlib.compress(b"\x00" * 60_000_000, 9)).decode()
    with pytest.raises(InvalidOperation, match="row limit"):
        compress.decode_transcript_gz(bomb)


def test_rejects_oversized_compressed_input():
    big = base64.b64encode(b"x" * (compress.MAX_COMPRESSED_BYTES + 10)).decode()
    with pytest.raises(InvalidOperation, match="size limit"):
        compress.decode_transcript_gz(big)


def test_resolve_exclusivity():
    with pytest.raises(InvalidOperation, match="not both"):
        compress.resolve_transcript("text", _gz64("text"))
    with pytest.raises(InvalidOperation, match="required"):
        compress.resolve_transcript(None, None)
    assert compress.resolve_transcript("plain", None) == "plain"


# --- REST round-trip -----------------------------------------------------------------


@pytest.fixture()
def rest(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbrest.server import build_app

    return TestClient(build_app())


def test_rest_ingest_accepts_transcript_gz(rest):
    text = "User: compressed REST submission works?\nAssistant: verifying.\n"
    r = rest.post(
        "/api/ingest-session",
        headers=AUTH,
        json={"project": "gzrest", "kind": "session", "transcript_gz": _gz64(text)},
    )
    assert r.status_code == 202, r.text
    qid = r.json()["queue_id"]
    assert qid is not None
    with db.connect() as conn:
        stored = conn.execute("SELECT content FROM ingest_queue WHERE id = %s", (qid,)).fetchone()[
            0
        ]
    assert "compressed REST submission" in stored, "stored DECOMPRESSED"


def test_rest_ingest_rejects_bomb_cleanly(rest):
    bomb = base64.b64encode(zlib.compress(b"\x00" * 60_000_000, 9)).decode()
    r = rest.post(
        "/api/ingest-session",
        headers=AUTH,
        json={"project": "gzrest", "transcript_gz": bomb},
    )
    assert r.status_code == 400
    assert "row limit" in r.json()["error"]


# --- MCP round-trip -----------------------------------------------------------------


def test_mcp_ingest_accepts_transcript_gz(kb):
    import asyncio

    from fastmcp import Client, FastMCP

    from kbmcp import tools_phase5

    mcp = FastMCP(name="kb-test")
    tools_phase5.register(mcp)
    text = "User: compressed MCP submission works?\nAssistant: verifying.\n"

    async def run():
        async with Client(mcp) as c:
            res = await c.call_tool(
                "kb_ingest_session",
                {"project": "gzmcp", "transcript_gz": _gz64(text), "kind": "session"},
            )
            return res.content[0].text

    out = asyncio.run(run())
    assert "queued" in out
    with db.connect() as conn:
        stored = conn.execute(
            "SELECT content FROM ingest_queue WHERE project = 'gzmcp' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert "compressed MCP submission" in stored

"""kbweb.lookups — the web UI's credential lookups run off the event loop and fail
fast: with kb-db down, a grantee request or a kbg_ login is a quick constant 503,
not a 60 s connect retry that also holds every other request on the loop.
Real DB (kb_test) to mint and sign in a grantee; the outage is a patched connect.
"""

from __future__ import annotations

import time

import psycopg
import pytest
from starlette.testclient import TestClient

from kbcore import db, principals
from kbcore.config import settings

TEST_TOKEN = "kbtest-" + "0" * 32


@pytest.fixture()
def app(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbweb.server import build_app

    return build_app()


def _db_goes_down(monkeypatch) -> list[str]:
    calls: list[str] = []

    def refuse(url, **kw):
        calls.append(url)
        raise psycopg.OperationalError(
            'connection failed: connection to server at "172.26.0.5", port 5432 failed: '
            "Connection refused"
        )

    monkeypatch.setattr(db.psycopg, "connect", refuse)
    monkeypatch.setattr(settings, "kb_db_connect_retry_secs", 60.0)
    return calls


def _assert_fast_503(response, started: float) -> None:
    assert time.monotonic() - started < 5, "the 60 s connect retry must not apply"
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.text == "Service Unavailable"
    assert "172.26" not in response.text


def test_a_grantee_session_gets_a_fast_503_while_the_db_is_down(app, monkeypatch):
    _pid, token = principals.create_principal("web-reader", kind="token")
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/login", data={"token": token}).status_code == 200
    principals._clear_cache()
    calls = _db_goes_down(monkeypatch)
    started = time.monotonic()
    _assert_fast_503(c.get("/", follow_redirects=False), started)
    assert len(calls) == 1, "one refused connect, no retry loop"


def test_a_token_login_with_the_db_down_is_a_fast_503(app, monkeypatch):
    calls = _db_goes_down(monkeypatch)
    c = TestClient(app, base_url="https://testserver")
    started = time.monotonic()
    _assert_fast_503(c.post("/login", data={"token": "kbg_" + "Z" * 43}), started)
    assert len(calls) == 1


def test_the_owner_gate_needs_no_database(app, monkeypatch):
    """The owner cookie resolves inline: through the gate to a route-less path
    (the router's own 404) without a single connect attempt."""
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/login", data={"token": TEST_TOKEN}).status_code == 200
    calls = _db_goes_down(monkeypatch)
    assert c.get("/no-such-zone", follow_redirects=False).status_code == 404
    assert calls == []

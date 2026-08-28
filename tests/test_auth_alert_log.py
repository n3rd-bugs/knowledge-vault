"""Wave G (review #6) small instrument fix: the 401-burst alert leaves ONE INFO
log line at fire time, so journald — not lossy ntfy phone history — is the
durable record of every firing. The line carries only window/IP counts (no
token, no body), and the no-oracle 401 forwarding is untouched. No DB.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from kbcore.config import settings
from kbrest import auth_alert

_401_MESSAGES = [
    {
        "type": "http.response.start",
        "status": 401,
        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
    },
    {"type": "http.response.body", "body": b'{"error": "unauthorized"}'},
]


async def _app(scope, receive, send):
    for m in _401_MESSAGES:
        await send(m)


def _run(mw, client_ip: str = "203.0.113.7") -> list[dict]:
    out: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        out.append(m)

    asyncio.run(mw({"type": "http", "client": (client_ip, 4444), "headers": []}, receive, send))
    return out


def test_fire_time_writes_exactly_one_info_line(monkeypatch, caplog):
    monkeypatch.setattr(settings, "kb_auth_alert_threshold", 1)
    pushed = threading.Event()
    monkeypatch.setattr(auth_alert, "push_alert", lambda line: pushed.set())
    mw = auth_alert.AuthAlertMiddleware(_app)

    with caplog.at_level(logging.INFO, logger="kbrest.auth_alert"):
        assert _run(mw) == _401_MESSAGES, "the no-oracle 401 bytes are untouched"
    assert pushed.wait(timeout=5), "the push still dispatches off-thread"

    lines = [r.getMessage() for r in caplog.records if r.name == "kbrest.auth_alert"]
    assert lines == ["auth alert fired: AUTH: 1 failed-auth in 10min from 1 IPs"]
    assert "kbg_" not in lines[0] and "Bearer" not in lines[0], "counts only — no credential"


def test_no_fire_no_log_line(monkeypatch, caplog):
    monkeypatch.setattr(settings, "kb_auth_alert_threshold", 10_000)
    mw = auth_alert.AuthAlertMiddleware(_app)
    with caplog.at_level(logging.INFO, logger="kbrest.auth_alert"):
        _run(mw)
    assert [r for r in caplog.records if r.name == "kbrest.auth_alert"] == []


def test_log_line_lands_even_when_the_push_delivers_nothing(monkeypatch, caplog):
    """The durable record must not depend on ntfy: the INFO line is written at
    fire time on the request path, BEFORE the push thread runs — a push that
    silently delivers nothing (dead tunnel; push_alert swallows every failure)
    still leaves the journald record."""
    monkeypatch.setattr(settings, "kb_auth_alert_threshold", 1)
    monkeypatch.setattr(auth_alert, "push_alert", lambda line: None)  # delivered nowhere
    mw = auth_alert.AuthAlertMiddleware(_app)
    with caplog.at_level(logging.INFO, logger="kbrest.auth_alert"):
        assert _run(mw) == _401_MESSAGES
    lines = [r.getMessage() for r in caplog.records if r.name == "kbrest.auth_alert"]
    assert lines == ["auth alert fired: AUTH: 1 failed-auth in 10min from 1 IPs"]

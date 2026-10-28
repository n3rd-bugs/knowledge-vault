"""kbmcp.bearer — the bearer gate never blocks the event loop, a down database is a
503 (not a 401, not an unhandled 500), and every rejection names the real client.

2026-10-28: while kb-db was down, each kbg_ lookup in kb-rest's async bearer
middleware retried the connect for 60 s ON the event loop (every other request
queued behind it), then escaped as a 500. The 401 line logged only the tunnel's
bridge address, so a 283-request scan through Cloudflare could not be attributed.
Pure ASGI + monkeypatched connects; only the cache-concurrency test uses kb_test.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import psycopg
import pytest
from starlette.testclient import TestClient

from kbcore import db, principals, scope
from kbcore.config import settings
from kbcore.errors import DatabaseUnavailable
from kbmcp import bearer, principal_ctx
from kbrest import auth_alert

MASTER = "kbtest-" + "0" * 32
GRANTEE = "kbg_" + "A" * 43  # the minted shape; no such row exists
LEGACY_401 = [
    {
        "type": "http.response.start",
        "status": 401,
        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
    },
    {"type": "http.response.body", "body": b'{"error": "unauthorized"}'},
]


@pytest.fixture(autouse=True)
def _master(monkeypatch):
    monkeypatch.setattr(settings, "kb_token", MASTER)
    principals._clear_cache()


async def _ok_app(asgi_scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _scope(auth: str | None = None, *, cf: bytes | None = None, path: str = "/api/stats"):
    headers = [(b"user-agent", b"zgrab/0.x")]
    if auth is not None:
        headers.append((b"authorization", auth.encode()))
    if cf is not None:
        headers.append((b"cf-connecting-ip", cf))
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "client": ("172.26.0.8", 44934),
        "headers": headers,
    }


async def _call(app, asgi_scope) -> list[dict]:
    out: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        out.append(message)

    await app(asgi_scope, receive, send)
    return out


def _refusing_connects(monkeypatch) -> list[str]:
    """psycopg.connect as it behaves with kb-db down: refused, every time."""
    calls: list[str] = []

    def refuse(url, **kw):
        calls.append(url)
        raise psycopg.OperationalError(
            'connection failed: connection to server at "172.26.0.5", port 5432 failed: '
            "Connection refused\n\tIs the server running on that host?"
        )

    monkeypatch.setattr(db.psycopg, "connect", refuse)
    monkeypatch.setattr(settings, "kb_db_connect_retry_secs", 60.0)
    return calls


# --- attribution --------------------------------------------------------------


def test_the_401_line_names_the_real_client_the_peer_and_what_was_asked(caplog):
    gate = bearer.BearerAuthMiddleware(_ok_app)
    with caplog.at_level(logging.WARNING, logger="kbmcp"):
        out = asyncio.run(_call(gate, _scope(cf=b"203.0.113.9", path="/robots.txt")))
    assert out == LEGACY_401, "the no-oracle 401 bytes are unchanged"
    assert [r.getMessage() for r in caplog.records if r.name == "kbmcp"] == [
        "401 unauthorized client=203.0.113.9 peer=172.26.0.8:44934 GET /robots.txt ua=zgrab/0.x"
    ]


def test_without_the_cloudflare_header_the_peer_is_the_client(caplog):
    gate = bearer.BearerAuthMiddleware(_ok_app)
    with caplog.at_level(logging.WARNING, logger="kbmcp"):
        asyncio.run(_call(gate, _scope("Bearer nope")))
    [line] = [r.getMessage() for r in caplog.records if r.name == "kbmcp"]
    assert line.startswith("401 unauthorized client=172.26.0.8 peer=172.26.0.8:44934 GET ")
    assert "nope" not in line, "the presented credential is never logged"


def test_a_garbage_cf_header_or_path_cannot_forge_a_log_line(caplog):
    gate = bearer.BearerAuthMiddleware(_ok_app)
    forged = _scope(cf=b"1.2.3.4\nkbmcp WARNING all clear", path="/x\nkbmcp INFO fake")
    with caplog.at_level(logging.WARNING, logger="kbmcp"):
        asyncio.run(_call(gate, forged))
    [line] = [r.getMessage() for r in caplog.records if r.name == "kbmcp"]
    assert "\n" not in line and "client=172.26.0.8 " in line, "non-IP header → the peer"
    assert bearer.client_ip({"headers": [(b"cf-connecting-ip", b" 2001:db8::7 ")]}) == "2001:db8::7"


def test_the_oauth_instance_logs_its_rejections_the_same_way(caplog):
    async def fastmcp_says_401(asgi_scope, receive, send):
        for m in LEGACY_401:
            await send(m)

    logger = bearer.RejectLogMiddleware(fastmcp_says_401)
    with caplog.at_level(logging.WARNING, logger="kbmcp"):
        assert asyncio.run(_call(logger, _scope(cf=b"198.51.100.4", path="/mcp"))) == LEGACY_401
        assert asyncio.run(_call(bearer.RejectLogMiddleware(_ok_app), _scope()))[0]["status"] == 200
    assert [r.getMessage() for r in caplog.records if r.name == "kbmcp"] == [
        "401 unauthorized client=198.51.100.4 peer=172.26.0.8:44934 GET /mcp ua=zgrab/0.x"
    ]


# --- a down database ---------------------------------------------------------


def test_a_down_database_answers_503_at_once_not_after_the_retry_budget(monkeypatch, caplog):
    calls = _refusing_connects(monkeypatch)
    gate = bearer.BearerAuthMiddleware(_ok_app)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        start, body = asyncio.run(_call(gate, _scope(f"Bearer {GRANTEE}")))
    assert time.monotonic() - started < 5, "the 60 s connect retry must not apply here"
    assert len(calls) == 1, "one refused connect, no retry loop"
    assert start["status"] == 503 and (b"retry-after", b"5") in start["headers"]
    assert json.loads(body["body"]) == {"error": "service unavailable"}
    assert b"172.26.0.5" not in body["body"], "the database host never reaches the client"
    [line] = [r.getMessage() for r in caplog.records if r.name == "kbmcp"]
    assert line.startswith("503 database unavailable client=172.26.0.8 ") and GRANTEE not in line
    [cause] = [r.getMessage() for r in caplog.records if r.name == "kbcore.db"]
    assert cause.startswith("database unavailable: connection failed:"), "the cause is logged"


def test_the_master_token_never_needs_the_database(monkeypatch):
    calls = _refusing_connects(monkeypatch)
    out = asyncio.run(_call(bearer.BearerAuthMiddleware(_ok_app), _scope(f"Bearer {MASTER}")))
    assert out[0]["status"] == 200 and calls == []


def test_a_503_is_not_counted_as_a_failed_auth(monkeypatch):
    _refusing_connects(monkeypatch)
    monkeypatch.setattr(settings, "kb_auth_alert_threshold", 1)
    app = auth_alert.AuthAlertMiddleware(bearer.BearerAuthMiddleware(_ok_app))
    assert asyncio.run(_call(app, _scope(f"Bearer {GRANTEE}")))[0]["status"] == 503
    assert len(app.counter._events) == 0, "an outage must not look like a credential probe"


def test_the_lookup_never_blocks_the_event_loop(monkeypatch):
    """A slow lookup (one DB round-trip that hangs) must not stall OTHER requests."""
    release = threading.Event()
    lookup_threads: list[int] = []

    def slow_resolve(header):
        lookup_threads.append(threading.get_ident())
        release.wait(timeout=10)
        return None

    monkeypatch.setattr(principal_ctx, "resolve_bearer", slow_resolve)
    gate = bearer.BearerAuthMiddleware(_ok_app)

    async def scenario():
        loop_thread = threading.get_ident()
        stuck = asyncio.create_task(_call(gate, _scope(f"Bearer {GRANTEE}")))
        for _ in range(500):  # wait (bounded) until the slow lookup is in flight
            if lookup_threads:
                break
            await asyncio.sleep(0.01)
        assert lookup_threads, "the slow lookup never started"
        # A second request completes while the first lookup is still blocked.
        other = await asyncio.wait_for(_call(gate, _scope(f"Bearer {MASTER}")), timeout=2)
        assert other[0]["status"] == 200 and not stuck.done()
        release.set()
        assert (await asyncio.wait_for(stuck, timeout=5))[0]["status"] == 401
        assert lookup_threads[0] != loop_thread, "the DB lookup ran on a worker thread"

    asyncio.run(scenario())


def test_fail_fast_is_scoped_to_its_block_and_its_context(monkeypatch):
    seen_elsewhere: list[float | None] = []
    with db.fail_fast():
        assert db._retry_override.get() == 0.0
        t = threading.Thread(target=lambda: seen_elsewhere.append(db._retry_override.get()))
        t.start()
        t.join()
    assert db._retry_override.get() is None, "the full budget is back after the block"
    assert seen_elsewhere == [None], "a concurrent caller keeps the full budget"
    with pytest.raises(DatabaseUnavailable, match="^database unavailable$"):
        with db.fail_fast():  # constant text: psycopg's names the database host
            raise psycopg.OperationalError('connection to server at "10.9.8.7" failed\n\tdetail')
    with pytest.raises(ValueError):  # anything that is not an outage passes through as itself
        with db.fail_fast():
            raise ValueError("not a database problem")


def test_fail_fast_also_bounds_a_hung_connect(monkeypatch):
    """No retry is not enough when the database HANGS: libpq would wait psycopg's
    default 130 s. Inside fail_fast every attempt carries a short connect_timeout."""
    seen: list[dict] = []

    def capture(url, **kw):
        seen.append(kw)
        raise psycopg.OperationalError("boom")  # not retryable: stops at once either way

    monkeypatch.setattr(db.psycopg, "connect", capture)
    with pytest.raises(DatabaseUnavailable), db.fail_fast():
        db.connect()
    with pytest.raises(psycopg.OperationalError):
        db.connect()
    assert seen[0]["connect_timeout"] == db._FAIL_FAST_CONNECT_TIMEOUT
    assert "connect_timeout" not in seen[1], "the nightly's connections are unchanged"


def test_credential_lookups_have_a_few_slots_of_their_own(monkeypatch):
    """A flood of bogus kbg_ bearers must not become one Postgres connection per
    thread of the shared 40-thread limiter."""
    lock = threading.Lock()
    active = [0, 0]  # now, peak

    def slow_resolve(header):
        with lock:
            active[0] += 1
            active[1] = max(active[1], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return None

    monkeypatch.setattr(principal_ctx, "resolve_bearer", slow_resolve)
    gate = bearer.BearerAuthMiddleware(_ok_app)

    async def flood():
        outs = await asyncio.gather(*(_call(gate, _scope(f"Bearer {GRANTEE}")) for _ in range(12)))
        assert all(o[0]["status"] == 401 for o in outs)

    asyncio.run(flood())
    assert 1 < active[1] <= principal_ctx._LOOKUP_SLOTS


def test_a_scoped_ipv6_cf_header_cannot_smuggle_text_into_the_line(caplog):
    forged = _scope(cf=b"::1%x peer=6.6.6.6:1 POST /api/enrol ua=curl\x1b[2J\x07")
    gate = bearer.BearerAuthMiddleware(_ok_app)
    with caplog.at_level(logging.WARNING, logger="kbmcp"):
        asyncio.run(_call(gate, forged))
    [line] = [r.getMessage() for r in caplog.records if r.name == "kbmcp"]
    assert line.startswith("401 unauthorized client=172.26.0.8 peer=172.26.0.8:44934 GET ")
    assert "6.6.6.6" not in line and "\x1b" not in line and "\x07" not in line


# --- the MCP principal middleware --------------------------------------------


def test_mcp_principal_resolution_runs_on_a_worker_thread(monkeypatch):
    grantee = scope.Principal(name="reader-1", is_owner=False)
    ran_on: list[int] = []

    def resolve(tok, raw):
        ran_on.append(threading.get_ident())
        return grantee

    monkeypatch.setattr(principal_ctx, "_request_inputs", lambda: (None, f"Bearer {GRANTEE}"))
    monkeypatch.setattr(principal_ctx, "_principal_fail_fast", resolve)

    async def scenario():
        async def call_next(_ctx):
            return scope.current()

        return threading.get_ident(), await principal_ctx.PrincipalMiddleware().on_message(
            None, call_next
        )

    loop_thread, seen = asyncio.run(scenario())
    assert seen is grantee and ran_on and ran_on[0] != loop_thread


@pytest.mark.parametrize(
    "inputs",
    [(None, f"Bearer {MASTER}"), (type("Tok", (), {"client_id": "bearer"})(), None)],
    ids=["local-master", "oauth-static-bearer"],
)
def test_the_owner_tags_resolve_inline_without_a_thread_or_the_db(monkeypatch, inputs):
    def must_not_run(tok, raw):
        raise AssertionError("an owner tag must not need a DB lookup")

    monkeypatch.setattr(principal_ctx, "_request_inputs", lambda: inputs)
    monkeypatch.setattr(principal_ctx, "_principal_fail_fast", must_not_run)

    async def call_next(_ctx):
        return scope.current()

    seen = asyncio.run(principal_ctx.PrincipalMiddleware().on_message(None, call_next))
    assert seen.is_owner


def test_mcp_resolution_with_the_database_down_fails_fast(monkeypatch):
    """...and what reaches an MCP client (the SDK returns str(exc)) names no host."""
    calls = _refusing_connects(monkeypatch)
    started = time.monotonic()
    with pytest.raises(DatabaseUnavailable) as raised:
        principal_ctx._principal_fail_fast(None, f"Bearer {GRANTEE}")
    assert time.monotonic() - started < 5 and len(calls) == 1
    assert str(raised.value) == "database unavailable"


def test_the_mcp_principal_is_read_from_the_authorization_header(monkeypatch):
    """FastMCP's get_http_headers() drops `authorization` unless asked for it by
    name — without that the local instance never stamped a principal and tools ran
    under whoever created the MCP session."""
    from fastmcp.server import dependencies
    from starlette.requests import Request

    def a_request():
        return Request(
            {"type": "http", "headers": [(b"authorization", f"Bearer {GRANTEE}".encode())]}
        )

    monkeypatch.setattr(dependencies, "get_http_request", a_request)
    assert "authorization" not in dependencies.get_http_headers(), "FastMCP's default"
    assert principal_ctx._request_inputs()[1] == f"Bearer {GRANTEE}"


def test_build_app_logs_rejections_on_the_oauth_instance(monkeypatch):
    from kbmcp import server

    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    monkeypatch.setattr(settings, "kb_oauth_enabled", True)
    monkeypatch.setattr(settings, "google_client_id", "client-id.apps.example")
    monkeypatch.setattr(settings, "google_client_secret", "GOCSPX-" + "t" * 28)
    monkeypatch.setattr(settings, "kb_public_url", "https://kb.example.test")
    assert isinstance(server.build_app().app, bearer.RejectLogMiddleware)
    monkeypatch.setattr(settings, "kb_oauth_enabled", False)
    assert isinstance(server.build_app().app, bearer.BearerAuthMiddleware)


# --- REST routes and the shared cache -----------------------------------------


def test_a_rest_route_that_meets_a_down_database_gets_a_clean_503(monkeypatch):
    from kbcore import stats

    def down():
        raise DatabaseUnavailable('connection failed: connection to server at "172.26.0.5"')

    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    monkeypatch.setattr(stats, "stats", down)
    from kbrest.server import build_app

    r = TestClient(build_app()).get("/api/stats", headers={"Authorization": f"Bearer {MASTER}"})
    assert r.status_code == 503 and r.json() == {"error": "service unavailable"}


class _SlowIterDict(dict):
    """A cache whose items() yields slowly and says when a scan has begun, so a
    concurrent clear() lands MID-scan: unlocked, the live iteration then raises
    'dictionary changed size during iteration'."""

    def __init__(self, *args):
        super().__init__(*args)
        self.scanning = threading.Event()

    def items(self):
        self.scanning.set()
        for pair in dict.items(self):
            time.sleep(0.005)
            yield pair


def test_concurrent_token_lookups_share_the_cache_safely(kb, monkeypatch):
    """Lookups now run on worker threads: the evict-then-insert scan must hold the
    lock against a concurrent clear (this fails on an unlocked cache)."""
    _pid, token = principals.create_principal("cache-race", kind="token")
    stale = scope.Principal(name="stale", is_owner=False)
    cache = _SlowIterDict({f"h{i}": (stale, float("inf")) for i in range(20)})
    monkeypatch.setattr(principals, "_cache", cache)
    errors: list[BaseException] = []

    def resolve():
        try:  # a cache miss: the DB, then the eviction scan over the 20 entries
            assert principals.resolve_token(token) is not None
        except BaseException as e:  # noqa: BLE001 — surfaced by the assertion below
            errors.append(e)

    def clear_mid_scan():
        assert cache.scanning.wait(timeout=10)
        principals._clear_cache()

    threads = [threading.Thread(target=resolve), threading.Thread(target=clear_mid_scan)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == [] and cache.scanning.is_set()

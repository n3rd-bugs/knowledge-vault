"""Phase 6b — Google sign-in for the read-only web UI. In-process via TestClient;
ALL Google HTTP is mocked (monkeypatch on kbweb.auth) — no real network calls.
Covers the allowlist gate, CSRF state guard, anonymous reachability of the OAuth
routes, and that no mutating route/import slipped into the read-only UI (Inv. 11).
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from kbcore.config import settings
from kbweb import auth

TEST_TOKEN = "kbtest-" + "0" * 32
ALLOWED = "alice@example.com"


@pytest.fixture()
def app(monkeypatch):
    # No DB needed: the OAuth + login routes never touch kbcore reads/writes.
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    monkeypatch.setattr(settings, "kb_web_public_url", "https://web.example.com")
    monkeypatch.setattr(settings, "kb_web_allowed_emails", ALLOWED)
    from kbweb.server import build_app

    return build_app()


@pytest.fixture()
def client(app):
    return TestClient(app)


def _start_login(client) -> str:
    """Begin the flow; return the random state Google would echo back."""
    r = client.get("/auth/google/login", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    return parse_qs(urlsplit(loc).query)["state"][0]


# (d) both OAuth routes must be reachable with NO existing session -------------


def test_oauth_routes_reachable_without_session(client):
    r = client.get("/auth/google/login", follow_redirects=False)
    assert r.status_code == 302  # went to Google, NOT redirected to /login
    assert r.headers["location"].startswith("https://accounts.google.com")

    r2 = client.get("/auth/google/callback", follow_redirects=False)
    assert r2.status_code == 403  # rendered the login page, not a 302 → /login
    assert r2.headers.get("location") != "/login"


def test_login_url_carries_expected_params(client):
    r = client.get("/auth/google/login", follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["response_type"] == ["code"]
    assert q["scope"] == ["openid email"]
    assert q["redirect_uri"] == ["https://web.example.com/auth/google/callback"]
    assert q["prompt"] == ["select_account"]
    # state cookie was set alongside the redirect
    assert auth.STATE_COOKIE in r.cookies


# (a) an allowlisted email logs in --------------------------------------------


def test_allowlisted_email_signs_in(client, monkeypatch):
    monkeypatch.setattr(auth, "exchange_google_code", lambda code: ALLOWED)
    state = _start_login(client)
    r = client.get(
        "/auth/google/callback",
        params={"state": state, "code": "good-code"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    session = r.cookies.get(auth.COOKIE)
    assert session and auth.cookie_valid(session), "a valid kb_session cookie is set"


def test_allowlist_is_case_insensitive(client, monkeypatch):
    monkeypatch.setattr(auth, "exchange_google_code", lambda code: "Alice@Example.com")
    state = _start_login(client)
    r = client.get(
        "/auth/google/callback",
        params={"state": state, "code": "good-code"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.cookies.get(auth.COOKIE)


# (b) a non-allowlisted email is rejected -------------------------------------


def test_non_allowlisted_email_rejected(client, monkeypatch):
    monkeypatch.setattr(auth, "exchange_google_code", lambda code: "mallory@evil.com")
    state = _start_login(client)
    r = client.get(
        "/auth/google/callback",
        params={"state": state, "code": "good-code"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert r.cookies.get(auth.COOKIE) is None, "no session issued"
    assert "not accepted" in r.text
    assert "mallory@evil.com" not in r.text, "email must not leak to the client"


def test_empty_allowlist_denies_everyone(client, monkeypatch):
    monkeypatch.setattr(settings, "kb_web_allowed_emails", "")  # safe default: deny all
    monkeypatch.setattr(auth, "exchange_google_code", lambda code: ALLOWED)
    state = _start_login(client)
    r = client.get(
        "/auth/google/callback",
        params={"state": state, "code": "good-code"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert r.cookies.get(auth.COOKIE) is None


# (c) a state mismatch is rejected (exchange must never even run) --------------


def test_state_mismatch_rejected(client, monkeypatch):
    called = {"n": 0}

    def _boom(code):
        called["n"] += 1
        return ALLOWED

    monkeypatch.setattr(auth, "exchange_google_code", _boom)
    _start_login(client)  # sets a valid state cookie in the jar
    r = client.get(
        "/auth/google/callback",
        params={"state": "forged-state", "code": "good-code"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert r.cookies.get(auth.COOKIE) is None
    assert called["n"] == 0, "code exchange must not run when state fails"


def test_exchange_failure_is_rejected(client, monkeypatch):
    monkeypatch.setattr(auth, "exchange_google_code", lambda code: None)  # Google said no
    state = _start_login(client)
    r = client.get(
        "/auth/google/callback",
        params={"state": state, "code": "bad-code"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert r.cookies.get(auth.COOKIE) is None


# (e) the read-only invariant still holds — no mutating route/import added -----


def test_oauth_routes_are_get_only(app):
    paths = {r.path for r in app.routes}
    assert {"/auth/google/login", "/auth/google/callback"} <= paths
    for route in app.routes:
        methods = set(route.methods or ())
        if route.path.startswith("/auth/google"):
            assert methods <= {"GET", "HEAD"}, f"{route.path} is not GET-only: {methods}"
            assert not (methods & {"PUT", "POST", "DELETE", "PATCH"})

    from kbweb import server as web

    for name in ("ingest", "supersede", "link", "create_summary", "promote", "reject"):
        assert name not in dir(web), f"{name} leaked into the read-only UI"


# --- unit-level checks of the pure auth helpers (no network) ------------------


def test_email_allowed_helper(monkeypatch):
    monkeypatch.setattr(settings, "kb_web_allowed_emails", "A@x.com, b@x.com")
    assert auth.email_allowed("a@x.com")
    assert auth.email_allowed("B@X.com")
    assert not auth.email_allowed("c@x.com")
    monkeypatch.setattr(settings, "kb_web_allowed_emails", "")
    assert not auth.email_allowed("a@x.com")


def test_state_roundtrip(monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    signed = auth.issue_state("abc123")
    assert auth.state_ok(signed, "abc123")
    assert not auth.state_ok(signed, "different")
    assert not auth.state_ok(None, "abc123")
    assert not auth.state_ok(signed, "")

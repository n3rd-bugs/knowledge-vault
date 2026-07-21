"""Google-grantee path end-to-end (security-review follow-up).

ONLY the Google verification boundary is mocked: the web flow patches
``kbweb.auth.exchange_google_code`` (the code→verified-email exchange), and the
connector flow feeds the verified email claim straight into
``kbmcp.principal_ctx.resolve_google_email``. Everything below that boundary —
principal resolution, cookie sessions, middleware, scope walling — is the REAL
code against kb_test (Ollama mocked by conftest). Asserts the four outcomes:
(a) allowlisted owner email → OWNER (unrestricted); (b) kind='google' grantee →
walled to exactly its grants (withheld byte-identical to nonexistent);
(c) unknown email → empty-walled; (d) revoked/expired → no longer resolves.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from kbcore import principals, scope
from kbcore.config import settings
from kbcore.errors import NotFound
from kbcore.ingest import ingest
from kbcore.items import get_item, list_projects, set_project
from kbcore.search import search
from kbmcp.principal_ctx import resolve_google_email
from kbweb import auth

TEST_TOKEN = "kbtest-" + "0" * 32
OWNER_EMAIL = "owner@example.com"
GRANTEE_EMAIL = "dev@client.example"


@pytest.fixture()
def world(kb, monkeypatch):
    """Two private projects; a kind='google' grantee granted read on 'shared'."""
    monkeypatch.setattr(settings, "kb_web_allowed_emails", OWNER_EMAIL)
    set_project("shared", public=False)
    set_project("nda", public=False)
    ids = {
        "shared": ingest("shared", "note", "shared spi note", "the shared widget uses SPI mode 1"),
        "nda": ingest("nda", "gotcha", "nda client gotcha", "the NDA client uses SPI mode 1 too"),
    }
    principals.create_principal("g-dev", kind="google", email=GRANTEE_EMAIL)
    principals.grant("g-dev", "shared", rw=False)
    return ids


# --- (a)-(d) at the resolver (connector path: verified claim → Principal) ------


def test_allowlisted_owner_email_resolves_owner_unrestricted(world):
    p = resolve_google_email(OWNER_EMAIL)
    assert p.is_owner
    with scope.use_principal(p):
        assert {pr.name for pr in list_projects()} == {"shared", "nda"}
        assert get_item(world["nda"]).project == "nda"  # nothing withheld


def test_google_grantee_walled_to_exact_grants(world):
    p = resolve_google_email(GRANTEE_EMAIL)
    assert p is not None and not p.is_owner and p.name == "g-dev"
    assert p.read_projects == frozenset({"shared"})
    assert p.write_projects == frozenset(), "web-admin grants are read-only"
    with pytest.raises(NotFound) as missing:  # owner baseline: truly-missing id
        get_item(9_999_999)
    with scope.use_principal(p):
        assert get_item(world["shared"]).project == "shared"  # granted → visible
        hits = search("SPI mode 1", k=10)
        assert {h.project for h in hits} == {"shared"}
        assert world["nda"] not in {h.item_id for h in hits}
        assert {pr.name for pr in list_projects()} == {"shared"}
        with pytest.raises(NotFound) as withheld:
            get_item(world["nda"])  # withheld → looks absent
    assert str(withheld.value) == str(missing.value).replace("9999999", str(world["nda"])), (
        "withheld must be byte-identical to nonexistent"
    )


def test_unknown_email_is_empty_walled(world):
    p = resolve_google_email("stranger@nowhere.example")
    assert not p.is_owner
    assert p.read_projects == frozenset() and p.write_projects == frozenset()
    with scope.use_principal(p):
        assert search("SPI mode 1", k=10) == []
        assert list_projects() == []
        with pytest.raises(NotFound):
            get_item(world["shared"])
    for claim in (None, "", "   "):  # degenerate claims collapse to the same wall
        q = resolve_google_email(claim)
        assert not q.is_owner and q.read_projects == frozenset()


def test_revoked_google_grantee_no_longer_resolves(world):
    principals.revoke("g-dev")
    assert principals.resolve_email(GRANTEE_EMAIL) is None
    p = resolve_google_email(GRANTEE_EMAIL)  # connector path: falls to empty wall
    assert not p.is_owner and p.read_projects == frozenset()
    with scope.use_principal(p), pytest.raises(NotFound):
        get_item(world["shared"])


def test_expired_google_grantee_no_longer_resolves(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_web_allowed_emails", OWNER_EMAIL)
    set_project("shared", public=False)
    item = ingest("shared", "note", "exp note", "body")
    past = datetime.now(UTC) - timedelta(days=1)
    principals.create_principal("g-exp", kind="google", email="exp@client.example", expires_at=past)
    principals.grant("g-exp", "shared", rw=False)
    assert principals.resolve_email("exp@client.example") is None
    p = resolve_google_email("exp@client.example")
    assert not p.is_owner and p.read_projects == frozenset()
    with scope.use_principal(p), pytest.raises(NotFound):
        get_item(item)


# --- the same four outcomes through the REAL web OAuth flow --------------------


@pytest.fixture()
def app(world, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    monkeypatch.setattr(settings, "kb_web_public_url", "https://web.example.com")
    from kbweb.server import build_app

    return build_app()


def _google_signin(app, monkeypatch, email: str):
    """Drive the real /auth/google flow with only the code exchange mocked."""
    client = TestClient(app, base_url="https://testserver")
    monkeypatch.setattr(auth, "exchange_google_code", lambda code: email)
    r = client.get("/auth/google/login", follow_redirects=False)
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    cb = client.get(
        "/auth/google/callback",
        params={"state": state, "code": "mock-code"},
        follow_redirects=False,
    )
    return client, cb


def test_web_owner_email_gets_owner_session(app, world, monkeypatch):
    c, cb = _google_signin(app, monkeypatch, OWNER_EMAIL)
    assert cb.status_code == 303
    assert "nda" in c.get("/projects").text  # owner sees every project
    assert c.get(f"/item/{world['nda']}").status_code == 200
    assert c.get("/admin").status_code == 200  # owner reaches the admin surface


def test_web_google_grantee_walled_end_to_end(app, world, monkeypatch):
    c, cb = _google_signin(app, monkeypatch, GRANTEE_EMAIL)
    assert cb.status_code == 303
    projects_page = c.get("/projects").text
    assert "shared" in projects_page and "nda" not in projects_page
    assert c.get(f"/item/{world['shared']}").status_code == 200
    withheld = c.get(f"/item/{world['nda']}")
    ghost = c.get("/item/9999999")
    assert withheld.status_code == ghost.status_code == 404
    assert withheld.content == ghost.content, "withheld item page differs from nonexistent"
    # no admin chrome, and the admin surface is the unknown-path 404
    assert "/admin" not in c.get("/").text
    unknown = c.get("/no-such-route")
    got = c.get("/admin")
    assert got.status_code == 404 and got.content == unknown.content


def test_web_unknown_email_refused_no_session(app, monkeypatch):
    c, cb = _google_signin(app, monkeypatch, "stranger@nowhere.example")
    assert cb.status_code == 403
    assert cb.cookies.get(auth.COOKIE) is None, "no session for an unknown email"
    assert "stranger@nowhere.example" not in cb.text, "email must not echo to the client"


def test_web_revoked_google_grantee_cookie_dies_immediately(app, world, monkeypatch):
    c, cb = _google_signin(app, monkeypatch, GRANTEE_EMAIL)
    assert cb.status_code == 303
    assert c.get(f"/item/{world['shared']}").status_code == 200
    principals.revoke("g-dev")  # owner revokes while the signed cookie is still valid
    r = c.get(f"/item/{world['shared']}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login", (
        "a revoked google grantee's live cookie must stop authenticating immediately"
    )

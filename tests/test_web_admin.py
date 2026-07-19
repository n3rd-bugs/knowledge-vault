"""Owner-only web share admin (the Inv. 11 carve-out) — behavior matrix.

Route-shape + confinement assertions live in test_phase6_web.py; this file
covers the surface's behavior: owner grant/revoke round trip (verified through
kbcore.principals and the DB), rw=false hardwiring, the grantee 404 wall
(byte-identical to an unknown path across every method — no admin-existence
oracle), CSRF (missing / garbage / expired / foreign-identity / session-cookie
confusion) and the same-origin defense layer. Real DB in kb_test; Ollama mocked
by conftest.
"""

from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from kbcore import db, principals
from kbcore.config import settings
from kbcore.ingest import ingest
from kbweb import auth

TEST_TOKEN = "kbtest-" + "0" * 32


@pytest.fixture()
def app(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbweb.server import build_app

    return build_app()


@pytest.fixture()
def owner(app):
    c = TestClient(app)
    assert c.post("/login", data={"token": TEST_TOKEN}).status_code == 200
    return c


def _grantee_client(app, name: str = "mallory", projects: tuple[str, ...] = ()) -> TestClient:
    """A signed-in kbg_ grantee session (login via the real /login route)."""
    _pid, token = principals.create_principal(name, kind="token")
    for p in projects:
        principals.grant(name, p, rw=False)
    c = TestClient(app)
    assert c.post("/login", data={"token": token}).status_code == 200
    return c


def _csrf_from(page_html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', page_html)
    assert m, "admin page must mint a CSRF token"
    return m.group(1)


def _grants_of(name: str) -> tuple[tuple[str, bool], ...]:
    return {p.name: p for p in principals.list_principals()}[name].grants


# --- owner path ----------------------------------------------------------------


def test_owner_sees_admin_page_and_nav_link(owner):
    ingest("proj-x", "note", "seed", "seed body")
    principals.create_principal("alice", kind="token")
    r = owner.get("/admin")
    assert r.status_code == 200
    assert "alice" in r.text and "proj-x" in r.text
    assert 'action="/admin/grant"' in r.text
    assert 'href="/admin"' in owner.get("/").text, "owner nav shows the admin link"


def test_owner_grant_then_revoke_round_trip(owner):
    ingest("proj-x", "note", "seed", "seed body")
    principals.create_principal("alice", kind="token")
    csrf = _csrf_from(owner.get("/admin").text)

    r = owner.post(
        "/admin/grant",
        data={"name": "alice", "project": "proj-x", "csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert _grants_of("alice") == (("proj-x", False),), "grant created, read-only"
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT pg.rw FROM principal_grants pg
            JOIN principals p ON p.id = pg.principal_id
            WHERE p.name = %s AND pg.project = %s
            """,
            ("alice", "proj-x"),
        ).fetchone()
    assert row == (False,), "rw=false persisted in the DB"

    page = owner.get("/admin").text
    assert "read-only" in page and 'action="/admin/revoke"' in page

    r = owner.post(
        "/admin/revoke",
        data={"name": "alice", "project": "proj-x", "csrf": _csrf_from(page)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert _grants_of("alice") == (), "grant removed"


def test_web_grant_is_read_only_even_if_form_lies(owner):
    ingest("proj-x", "note", "seed", "seed body")
    principals.create_principal("alice", kind="token")
    csrf = _csrf_from(owner.get("/admin").text)
    r = owner.post(
        "/admin/grant",
        data={"name": "alice", "project": "proj-x", "csrf": csrf, "rw": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert _grants_of("alice") == (("proj-x", False),), "an injected rw field must be ignored"


def test_owner_grant_unknown_principal_is_404(owner):
    ingest("proj-x", "note", "seed", "seed body")  # so the page renders a form + CSRF
    principals.create_principal("alice", kind="token")
    csrf = _csrf_from(owner.get("/admin").text)
    r = owner.post("/admin/grant", data={"name": "ghost", "project": "proj-x", "csrf": csrf})
    assert r.status_code == 404  # kbcore NotFound → domain 404 (owner-only surface)
    assert _grants_of("alice") == ()


# --- grantee wall (no oracle) ---------------------------------------------------


def test_grantee_admin_is_byte_identical_404_all_methods(app):
    ingest("proj-a", "note", "seed", "seed body")
    g = _grantee_client(app, projects=("proj-a",))
    ghost = g.get("/definitely-not-a-route")
    assert ghost.status_code == 404
    paths = ("/admin", "/admin/grant", "/admin/revoke", "/admin/principal", "/admin/x/y")
    for path in paths:
        for method in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"):
            r = g.request(method, path)
            assert r.status_code == 404, f"grantee {method} {path} → {r.status_code}"
            if method != "HEAD":
                assert r.content == ghost.content, f"{method} {path} 404 body differs"
            assert r.headers["content-type"] == ghost.headers["content-type"]


def test_grantee_post_cannot_mutate_even_with_owner_csrf(app):
    ingest("proj-a", "note", "seed", "seed body")
    principals.create_principal("alice", kind="token")
    g = _grantee_client(app, name="gm", projects=("proj-a",))
    # Even holding a validly-signed owner-bound CSRF token, a grantee session
    # must be stopped by the identity wall BEFORE any mutation.
    r = g.post(
        "/admin/grant",
        data={"name": "alice", "project": "proj-a", "csrf": auth.issue_csrf(auth.OWNER_IDENTITY)},
    )
    assert r.status_code == 404
    assert _grants_of("alice") == (), "no grant may be created by a grantee session"


def test_grantee_never_sees_admin_link(app):
    ingest("proj-a", "note", "seed", "seed body")
    g = _grantee_client(app, projects=("proj-a",))
    home = g.get("/")
    assert home.status_code == 200
    assert "/admin" not in home.text, "admin chrome leaked to a grantee"


def test_unauthenticated_admin_redirects_like_any_page(app):
    c = TestClient(app)
    for path, method in (("/admin", "get"), ("/admin/grant", "post"), ("/admin/revoke", "post")):
        r = getattr(c, method)(path, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/login"


# --- CSRF ----------------------------------------------------------------------


def _grant_attempt(owner, csrf: str | None) -> int:
    data = {"name": "alice", "project": "proj-x"}
    if csrf is not None:
        data["csrf"] = csrf
    return owner.post("/admin/grant", data=data, follow_redirects=False).status_code


@pytest.fixture()
def csrf_world(owner):
    ingest("proj-x", "note", "seed", "seed body")
    principals.create_principal("alice", kind="token")
    return owner


def test_post_without_csrf_rejected(csrf_world):
    assert _grant_attempt(csrf_world, None) == 403
    assert _grant_attempt(csrf_world, "") == 403
    assert _grants_of("alice") == ()


def test_post_with_garbage_csrf_rejected(csrf_world):
    assert _grant_attempt(csrf_world, "garbage-token") == 403
    assert _grants_of("alice") == ()


def test_post_with_expired_csrf_rejected(csrf_world, monkeypatch):
    csrf = _csrf_from(csrf_world.get("/admin").text)
    monkeypatch.setattr(auth, "CSRF_MAX_AGE", -1)  # everything is now expired
    assert _grant_attempt(csrf_world, csrf) == 403
    assert _grants_of("alice") == ()


def test_session_cookie_is_not_a_valid_csrf_token(csrf_world):
    # Distinct itsdangerous salts: the (signed) session cookie must never pass
    # as a CSRF token even though both are signed with the same secret.
    cookie = csrf_world.cookies.get(auth.COOKIE)
    assert cookie
    assert _grant_attempt(csrf_world, cookie) == 403
    assert _grants_of("alice") == ()


def test_csrf_identity_binding_unit():
    assert auth.csrf_ok(auth.issue_csrf(auth.OWNER_IDENTITY), auth.OWNER_IDENTITY)
    assert not auth.csrf_ok(auth.issue_csrf("g:mallory"), auth.OWNER_IDENTITY)
    assert not auth.csrf_ok(None, auth.OWNER_IDENTITY)
    assert not auth.csrf_ok("", auth.OWNER_IDENTITY)


# --- same-origin defense layer ---------------------------------------------------


def test_cross_origin_post_rejected_even_with_valid_csrf(csrf_world):
    csrf = _csrf_from(csrf_world.get("/admin").text)
    for headers in (
        {"origin": "https://evil.example"},
        {"origin": "null"},
        {"referer": "https://evil.example/admin"},
    ):
        r = csrf_world.post(
            "/admin/grant",
            data={"name": "alice", "project": "proj-x", "csrf": csrf},
            headers=headers,
            follow_redirects=False,
        )
        assert r.status_code == 403, f"cross-origin POST passed with {headers}"
    assert _grants_of("alice") == ()

    # the genuine same-origin browser case still works
    r = csrf_world.post(
        "/admin/grant",
        data={"name": "alice", "project": "proj-x", "csrf": csrf},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert _grants_of("alice") == (("proj-x", False),)

"""Web admin extension (A11-2, owner decision 2026-07-19) — behavior matrix.

POST /admin/principal (create a kind='google' grantee + initial read-only
shares in one submit), the per-grantee invite block (mailto: href + copyable
text — no secret by construction), and the /welcome getting-started page.
Complements test_web_admin.py (grant/revoke, CSRF, 404 wall) and
test_phase6_web.py (route-shape + confinement). Real DB in kb_test; Ollama
mocked by conftest.
"""

from __future__ import annotations

import inspect
import re

import pytest
from starlette.testclient import TestClient

from kbcore import db, principals
from kbcore.config import settings
from kbcore.ingest import ingest
from kbweb import views_admin, views_admin_invite

TEST_TOKEN = "kbtest-" + "0" * 32
MCP_BASE = "https://kb.example.test"
WEB_BASE = "https://web.example.test"
GOOD = {"name": "alice", "email": "alice@client.example"}


@pytest.fixture()
def app(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    monkeypatch.setattr(settings, "kb_public_url", MCP_BASE)
    monkeypatch.setattr(settings, "kb_web_public_url", WEB_BASE)
    from kbweb.server import build_app

    return build_app()


@pytest.fixture()
def owner(app):
    c = TestClient(app)
    assert c.post("/login", data={"token": TEST_TOKEN}).status_code == 200
    return c


def _grantee_client(app, name: str = "mallory", projects: tuple[str, ...] = ()) -> TestClient:
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


def _create(owner, /, **fields):
    """POST /admin/principal with a fresh CSRF; fields override the GOOD base."""
    data = {**GOOD, "csrf": _csrf_from(owner.get("/admin").text), **fields}
    return owner.post("/admin/principal", data=data, follow_redirects=False)


def _names() -> set[str]:
    return {p.name for p in principals.list_principals()}


def _info(name: str) -> principals.PrincipalInfo:
    return {p.name: p for p in principals.list_principals()}[name]


# --- happy path -----------------------------------------------------------------


def test_create_google_grantee_with_initial_shares(owner):
    ingest("proj-x", "note", "seed", "seed body")
    ingest("proj-y", "note", "seed2", "seed body 2")
    r = _create(owner, projects=["proj-x", "proj-y"])
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    info = _info("alice")
    assert info.kind == "google" and info.email == "alice@client.example"
    assert info.grants == (("proj-x", False), ("proj-y", False)), "all initial shares rw=False"
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT pg.rw FROM principal_grants pg
            JOIN principals p ON p.id = pg.principal_id WHERE p.name = %s
            """,
            ("alice",),
        ).fetchall()
    assert rows == [(False,), (False,)], "rw=false persisted for every initial share"
    # the invite block appears immediately on the dashboard
    page = owner.get("/admin").text
    assert "mailto:alice@client.example" in page and "proj-x" in page


def test_email_is_normalized_lowercase(owner):
    r = _create(owner, email="Alice@Client.Example")
    assert r.status_code == 303
    assert _info("alice").email == "alice@client.example"


def test_unknown_submitted_project_is_silently_skipped(owner):
    ingest("proj-x", "note", "seed", "seed body")
    r = _create(owner, projects=["proj-x", "ghost-project", "nda; DROP TABLE items"])
    assert r.status_code == 303
    assert _info("alice").grants == (("proj-x", False),), "only existing projects are granted"


def test_duplicate_name_rerenders_with_message(owner):
    principals.create_principal("alice", kind="google", email="old@client.example")
    r = _create(owner)
    assert r.status_code == 400
    assert "already exists" in r.text and 'action="/admin/principal"' in r.text
    assert _info("alice").email == "old@client.example", "the existing row is untouched"


def test_duplicate_email_rerenders_clean_400_not_500(owner):
    # Regression (security review 2026-07-19): a duplicate EMAIL used to escape
    # as a raw psycopg UniqueViolation -> 500; create_principal now pre-checks
    # email so it re-renders the clean 400 like the duplicate-name path.
    principals.create_principal("bob", kind="google", email="alice@client.example")
    r = _create(owner)  # GOOD reuses alice@client.example on a NEW name 'alice'
    assert r.status_code == 400
    assert "email" in r.text and 'action="/admin/principal"' in r.text
    assert "alice" not in _names(), "no principal created on the duplicate-email path"


def test_grant_share_rejects_unknown_project(owner):
    # Regression (security review 2026-07-19): /admin/grant used to upsert a
    # phantom grant for a nonexistent project; it now re-renders a 400.
    principals.create_principal("carol", kind="google", email="carol@client.example")
    csrf = _csrf_from(owner.get("/admin").text)
    data = {"name": "carol", "project": "ghost-project", "csrf": csrf}
    r = owner.post("/admin/grant", data=data, follow_redirects=False)
    assert r.status_code == 400 and "unknown project" in r.text
    assert _info("carol").grants == (), "no phantom grant created"


# --- validation (server-side; page re-rendered, no row created) -----------------


@pytest.mark.parametrize(
    "name",
    ["", "Alice", "-alice", ".alice", "al ice", "a" * 64, "al<script>", "g:owner", "a,b"],
)
def test_bad_name_rejected(owner, name):
    r = _create(owner, name=name)
    assert r.status_code == 400
    assert 'action="/admin/principal"' in r.text, "the admin page re-renders"
    assert "name must be a slug" in r.text
    assert name not in _names() and "alice" not in _names()


@pytest.mark.parametrize(
    "email",
    [
        "",
        "no-at-sign.example",
        "two@at@signs.example",
        "space in@example.com",
        "semi;colon@example.com",
        "comma,inside@example.com",
        "percent%40hex@example.com",
        "victim@example.com%0d%0aBcc:everyone@evil.example",  # header-injection shape
        "crlf\r\nBcc:everyone@evil.example",
        "a" * 250 + "@example.com",  # > 254 chars
    ],
)
def test_bad_email_rejected(owner, email):
    r = _create(owner, email=email)
    assert r.status_code == 400
    assert "email must be a plain address" in r.text
    assert _names() == set(), "no principal row may be created on a rejected email"


def test_oversized_note_rejected(owner):
    r = _create(owner, note="x" * 201)
    assert r.status_code == 400
    assert "note must be 200 characters or fewer" in r.text
    assert _names() == set()


# --- gates: CSRF + the non-owner 404 wall ---------------------------------------


def test_create_without_csrf_is_403(owner):
    r = owner.post("/admin/principal", data=dict(GOOD), follow_redirects=False)
    assert r.status_code == 403
    assert _names() == set()


def test_non_owner_create_is_byte_identical_404(app):
    ingest("proj-a", "note", "seed", "seed body")
    g = _grantee_client(app, projects=("proj-a",))
    ghost = g.get("/definitely-not-a-route")
    assert ghost.status_code == 404
    r = g.post("/admin/principal", data=dict(GOOD))
    assert r.status_code == 404
    assert r.content == ghost.content, "grantee POST 404 must match the unknown-path body"
    assert r.headers["content-type"] == ghost.headers["content-type"]
    assert "alice" not in _names()


# --- forged kind: the web can NEVER mint a token principal ----------------------


def test_forged_kind_field_cannot_mint_token(owner):
    ingest("proj-x", "note", "seed", "seed body")
    r = _create(owner, name="sneaky", kind="token", token="1", projects=["proj-x"])
    assert r.status_code == 303, "the forged field is ignored, not an error"
    with db.connect() as conn:
        row = conn.execute(
            "SELECT kind, secret_hash FROM principals WHERE name = %s", ("sneaky",)
        ).fetchone()
    assert row == ("google", None), "still google, and NO secret exists to leak"
    # and the view has no parameter path for a kind at all (defense in source)
    src = inspect.getsource(views_admin)
    assert 'form.get("kind"' not in src and 'kind="google"' in src


# --- invite block: mailto encoding + text content, no secrets -------------------


def test_mailto_href_percent_encoded_no_raw_newlines(owner):
    ingest("proj-x", "note", "seed", "seed body")
    principals.create_principal("alice", kind="google", email="alice@client.example")
    principals.grant("alice", "proj-x", rw=False)
    page = owner.get("/admin").text
    m = re.search(r'href="(mailto:[^"]+)"', page)
    assert m, "invite mailto link rendered"
    href = m.group(1)
    assert href.startswith("mailto:alice@client.example?subject=")
    assert "\n" not in href and "\r" not in href and " " not in href
    assert "%0A" in href, "body newlines are percent-encoded"
    # unit level: the helper itself (quote-built, never format-string interpolation)
    unit = views_admin_invite.mailto_href(
        "alice@client.example", views_admin_invite.invite_text("alice", ["proj-x"])
    )
    assert "\n" not in unit and "\r" not in unit and " " not in unit
    assert "subject=You%27ve%20been%20given%20access" in unit


def test_invite_text_has_projects_urls_and_no_secret(owner):
    ingest("proj-x", "note", "seed", "seed body")
    _pid, kbg_token = principals.create_principal("bob-script", kind="token")
    principals.create_principal("alice", kind="google", email="alice@client.example")
    principals.grant("alice", "proj-x", rw=False)
    text = views_admin_invite.invite_text("alice", ["proj-x"])
    assert "Hi alice," in text and "- proj-x" in text
    assert f"{MCP_BASE}/mcp" in text and WEB_BASE in text
    assert "READ-ONLY" in text and "revoked" in text and "Sign in with Google" in text
    for secret in ("kbg_", TEST_TOKEN):
        assert secret not in text, "the invite must never carry token material"
    page = owner.get("/admin").text
    assert "proj-x" in page and f"{MCP_BASE}/mcp" in page and WEB_BASE in page
    assert "kbg_" not in page and TEST_TOKEN not in page and kbg_token not in page


def test_invite_degrades_to_placeholder_without_public_urls(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    monkeypatch.setattr(settings, "kb_public_url", "")
    monkeypatch.setattr(settings, "kb_web_public_url", "")
    text = views_admin_invite.invite_text("alice", [])
    assert "KB_PUBLIC_URL" in text and "KB_WEB_PUBLIC_URL" in text, "placeholders, not a crash"
    from kbweb.server import build_app

    principals.create_principal("alice", kind="google", email="alice@client.example")
    c = TestClient(build_app())
    assert c.post("/login", data={"token": TEST_TOKEN}).status_code == 200
    r = c.get("/admin")
    assert r.status_code == 200 and "KB_PUBLIC_URL" in r.text


def test_revoked_google_principal_gets_no_invite_block(owner):
    principals.create_principal("alice", kind="google", email="alice@client.example")
    principals.revoke("alice")
    assert "mailto:" not in owner.get("/admin").text


# --- /welcome -------------------------------------------------------------------


def test_welcome_grantee_sees_exactly_their_grants(app):
    ingest("shared", "note", "seed", "seed body")
    ingest("withheld-proj", "note", "seed2", "seed body 2")
    g = _grantee_client(app, name="dev", projects=("shared",))
    r = g.get("/welcome")
    assert r.status_code == 200
    assert "dev" in r.text and "shared" in r.text
    assert "withheld-proj" not in r.text, "a grantee must see ONLY its own grants"
    assert "Owner identity" not in r.text
    assert f"{MCP_BASE}/mcp" in r.text and WEB_BASE in r.text
    assert 'href="/welcome"' in g.get("/").text, "signed-in nav shows the welcome link"


def test_welcome_google_grantee_via_oauth_identity(app):
    ingest("shared", "note", "seed", "seed body")
    principals.create_principal("g-dev", kind="google", email="dev@client.example")
    principals.grant("g-dev", "shared", rw=False)
    from kbweb import auth

    c = TestClient(app)
    c.cookies.set(auth.COOKIE, auth.issue_cookie(auth.grantee_identity("g-dev")))
    r = c.get("/welcome")
    assert r.status_code == 200 and "g-dev" in r.text and "shared" in r.text


def test_welcome_owner_renders_everything_visible_note(owner):
    ingest("proj-x", "note", "seed", "seed body")
    r = owner.get("/welcome")
    assert r.status_code == 200
    assert "Owner identity" in r.text and "every project" in r.text


def test_welcome_anonymous_redirects_to_login(app):
    r = TestClient(app).get("/welcome", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"

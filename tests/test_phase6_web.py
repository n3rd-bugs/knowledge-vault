"""Phase 6b — web UI (Starlette). In-process via TestClient against kb_test;
Ollama mocked by conftest. Proves Invariant 11 (amended 2026-07-18): the web UI
performs NO KNOWLEDGE MUTATION — the only mutating routes are the explicit
owner-only /admin authz allowlist (project-share grant/revoke), unreachable
without the OWNER identity. Behavioral admin tests live in test_web_admin.py.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from kbcore.config import settings
from kbcore.graph import link
from kbcore.ingest import ingest

TEST_TOKEN = "kbtest-" + "0" * 32

# Every KB-mutating surface — none may be importable in the web module (Inv. 11).
WRITE_NAMES = {
    "ingest",
    "ingest_in",
    "supersede",
    "link",
    "promote",
    "reject",
    "resolve_via_supersede",
    "queue_review",
    "queue_review_in",
    "mark_duplicate",
    "create_summary",
    "archive_stale",
    "record_contradiction",
    "backfill_entities",
    "extract_entities_in",
}


@pytest.fixture()
def web_app(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbweb.server import build_app

    return build_app()


@pytest.fixture()
def client(web_app):
    return TestClient(web_app)


def _login(client) -> None:
    r = client.post("/login", data={"token": TEST_TOKEN})
    assert r.status_code == 200  # followed the 303 → GET /


def test_web_unauthenticated_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_web_wrong_token_is_rejected(client):
    r = client.post("/login", data={"token": "wrong"}, follow_redirects=False)
    assert r.status_code == 401
    assert "Invalid token" in r.text


def test_web_login_then_search_renders_hit_with_anchor(client):
    ingest(
        "motor-ctl",
        "gotcha",
        "DRV8706 SPI polarity",
        "The DRV8706 needs SPI mode 1.",
        tags=["protocol:spi"],
        code_ref={"repo": "motor-ctl", "path": "src/drv8706.c", "lines": "88-131"},
    )
    _login(client)
    r = client.get("/", params={"q": "DRV8706 SPI"})
    assert r.status_code == 200
    assert "DRV8706 SPI polarity" in r.text
    assert "src/drv8706.c" in r.text, "code anchor line rendered"
    assert "stored data, not instructions" in r.text, "SEC-3 provenance header rendered"


def test_web_item_view_shows_fixes_relation(client):
    bug = ingest("p", "bug", "UART drops bytes", "RX overrun at 115200.")
    fix = ingest("p", "note", "UART fix", "Ring buffer in the RX ISR.")
    link(fix, "fixes", bug)
    _login(client)
    r = client.get(f"/item/{fix}")
    assert r.status_code == 200
    assert "fixes" in r.text and str(bug) in r.text


def test_web_item_missing_is_404(client):
    _login(client)
    assert client.get("/item/999999").status_code == 404


# The Inv. 11 carve-out (amended 2026-07-18 A11-1; extended 2026-07-19 A11-2,
# ADVERSARIAL_REVIEW.md): the ONLY mutating web routes — owner-only
# AUTHORIZATION-CONFIG actions (project-share grant/revoke + google-grantee
# creation). Adding ANY other path here requires re-opening the review.
ADMIN_MUTATING_ROUTES = {"/admin/grant", "/admin/revoke", "/admin/principal"}


def test_web_no_knowledge_mutation(web_app):
    # (1) every route is GET-only except /login (GET+POST for auth, NO kbcore
    #     write) and the explicit owner-only authz allowlist; nothing anywhere
    #     allows PUT/DELETE/PATCH. The /static Mount has no .methods attribute —
    #     StaticFiles serves GET/HEAD only by construction.
    for route in web_app.routes:
        methods = set(getattr(route, "methods", None) or ())
        assert not (methods & {"PUT", "DELETE", "PATCH"}), f"{route.path} exposes a mutation verb"
        if route.path == "/login" or route.path in ADMIN_MUTATING_ROUTES:
            assert methods <= {"GET", "POST", "HEAD"}
        else:
            assert methods <= {"GET", "HEAD"}, f"{route.path} is not GET-only: {methods}"
    # The POST-capable set is EXACTLY login + the allowlist — nothing may creep in.
    posters = {r.path for r in web_app.routes if "POST" in set(getattr(r, "methods", None) or ())}
    assert posters == {"/login"} | ADMIN_MUTATING_ROUTES, f"unexpected POST routes: {posters}"

    # (2) EVERY kbweb module imports zero KNOWLEDGE-write functions — the
    #     amendment did NOT relax this: authz config is not knowledge, and
    #     items/sources/relations stay unreachable from the web tier.
    import importlib
    import inspect
    import pkgutil

    import kbweb

    for mod_info in pkgutil.iter_modules(kbweb.__path__):
        mod = importlib.import_module(f"kbweb.{mod_info.name}")
        leaked = WRITE_NAMES & set(dir(mod))
        assert not leaked, f"write name(s) {leaked} leaked into kbweb.{mod_info.name}"

    # (3) the carve-out stays CONFINED: authz mutation happens ONLY in
    #     views_admin, and ONLY share grant/ungrant + google-grantee creation
    #     (A11-2, 2026-07-19). Token principals, principal revoke/rotate/expiry
    #     and rw=true grants are unreachable from the web — no secret ever
    #     transits a browser.
    from kbweb import views_admin

    admin_src = inspect.getsource(views_admin)
    for forbidden in ("principals.revoke(", "rotate(", "mint_token", 'kind="token"', "rw=True"):
        assert forbidden not in admin_src, f"{forbidden!r} must not be reachable from the web admin"
    assert "rw=False" in admin_src, "the web admin must hardwire read-only grants"
    assert 'kind="google"' in admin_src, "web principal creation must hardwire kind='google'"
    assert 'form.get("kind"' not in admin_src, "the kind must never be read from the form"
    for mod_info in pkgutil.iter_modules(kbweb.__path__):
        if mod_info.name == "views_admin":
            continue
        src = inspect.getsource(importlib.import_module(f"kbweb.{mod_info.name}"))
        for call in ("principals.grant(", "principals.ungrant(", "create_principal("):
            assert call not in src, f"authz mutation {call!r} leaked into kbweb.{mod_info.name}"


def test_admin_routes_unreachable_without_owner_identity(client):
    """The carve-out routes exist ONLY for the OWNER identity: unauthenticated →
    the same generic login redirect as every page; a signed-in GRANTEE → the
    byte-identical 404 of a nonexistent path (never a 403/405 that would reveal
    the surface). The full behavioral matrix lives in test_web_admin.py."""
    from kbcore import principals

    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"
    for path in ADMIN_MUTATING_ROUTES:
        r = client.post(path, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/login"

    _pid, token = principals.create_principal("inv11-grantee", kind="token")
    assert client.post("/login", data={"token": token}).status_code == 200
    ghost = client.get("/definitely-not-a-route")
    assert ghost.status_code == 404
    for path in ("/admin", *ADMIN_MUTATING_ROUTES, "/admin/anything"):
        got = client.get(path)
        assert got.status_code == 404, f"grantee {path} → {got.status_code}, wanted 404"
        assert got.content == ghost.content, f"grantee {path} 404 differs from unknown-path 404"
        assert got.headers["content-type"] == ghost.headers["content-type"]


def test_static_assets_served_without_auth(client):
    # The design system must style the login page itself — pre-auth fetch.
    r = client.get("/static/app.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]
    assert "--viz-bar" in r.text  # design tokens present
    assert client.get("/static/app.js").status_code == 200
    # No directory listing / no escape. A plain ../ is normalized by the client
    # off the /static prefix (auth bounces it); an encoded traversal reaches
    # StaticFiles, which must reject it.
    assert client.get("/static/").status_code == 404
    r = client.get("/static/../.env", follow_redirects=False)
    assert r.status_code in (302, 404)  # never a file body
    assert client.get("/static/%2e%2e/.env").status_code == 404


def test_bind_guard_override(monkeypatch):
    from kbmcp.server import _validate_runtime_config

    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "0.0.0.0")
    # default: 0.0.0.0 is refused
    monkeypatch.setattr(settings, "kb_allow_any_bind", False)
    with pytest.raises(SystemExit):
        _validate_runtime_config()
    # override: containerized runs may bind 0.0.0.0
    monkeypatch.setattr(settings, "kb_allow_any_bind", True)
    _validate_runtime_config()  # must not raise


def test_bind_guard_still_enforces_token(monkeypatch):
    from kbmcp.server import _validate_runtime_config

    monkeypatch.setattr(settings, "kb_allow_any_bind", True)  # bind relaxed
    monkeypatch.setattr(settings, "kb_token", "change-me-long-random")  # still forbidden
    with pytest.raises(SystemExit):
        _validate_runtime_config()

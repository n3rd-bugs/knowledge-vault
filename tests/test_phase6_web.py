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
# Extended for the Phase-1 harness (WEBUI_TIERED_ACCESS_PLAN §5): the newer
# write/GPU surfaces below joined kbcore after the original guard landed;
# tests/test_web_caps.py asserts this set stays a superset of that expansion.
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
    # Phase-1 expansion (2026-07-20): jobs / deep search / review resolution /
    # triage / reprocess / item+project mutation.
    "enqueue_deep_search",
    "request_consolidation",
    "deep_search",
    "run_triage",
    "store_triage",
    "apply_triage",
    "dismiss",
    "bulk_resolve",
    "reprocess",
    "archive_item",
    "set_project",
    # A11-3 (2026-07-21): the COMPOSED web-write chokepoints. Confined to
    # kbweb.views_write (below); leaking into ANY other kbweb module still fails.
    "save_for_review",
    "supersede_for_review",
}

# A11-3: the ONLY kbweb module permitted to mutate knowledge, and the EXACT set of
# write-surface functions it may reach (the confined-write allowlist, cross-checked
# against WRITE_NAMES). Trust escalation (promote / ingest / supersede raw) is NOT
# in it — importing any of those into the web fails this guard AND the allowlist test.
CONFINED_WRITE_MODULE = "views_write"
CONFINED_WRITE_NAMES = {"save_for_review", "supersede_for_review", "link", "reject", "dismiss"}


@pytest.fixture()
def web_app(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbweb.server import build_app

    return build_app()


@pytest.fixture()
def client(web_app):
    return TestClient(web_app, base_url="https://testserver")


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

# Owner-only web backup-setup surface (owner decision 2026-07-21): mutating but
# HOST-CONFIG, never knowledge — these write the offsite conf file + the
# write-only rclone credential (kbcore.offsite_config), touching NO
# items/sources/relations/review state. Owner-gated exactly like /admin.
BACKUP_CONFIG_ROUTES = {"/backups/config", "/backups/keygen", "/backups/rclone"}


def test_web_no_knowledge_mutation(web_app):
    # A11-3: the confined web-write routes are the EXACTLY four permitted mutating
    # paths, each its own top-level zone. Derived from the module so a renamed route
    # can't slip past this guard.
    from kbweb import views_write

    write_routes = {r.path for r in views_write.ROUTES}
    assert write_routes == {"/save", "/supersede/{id:int}", "/link/{id:int}", "/resolve/{qid:int}"}

    # (1) every route is GET-only except /login (auth), the owner-only authz
    #     allowlist, the owner-only backup-config surface, and the A11-3 confined
    #     write routes; nothing anywhere allows PUT/DELETE/PATCH. The /static Mount
    #     has no .methods — StaticFiles is GET/HEAD.
    post_ok = {"/login"} | ADMIN_MUTATING_ROUTES | BACKUP_CONFIG_ROUTES | write_routes
    for route in web_app.routes:
        methods = set(getattr(route, "methods", None) or ())
        assert not (methods & {"PUT", "DELETE", "PATCH"}), f"{route.path} exposes a mutation verb"
        if route.path in post_ok:
            assert methods <= {"GET", "POST", "HEAD"}, f"{route.path}: {methods}"
        else:
            assert methods <= {"GET", "HEAD"}, f"{route.path} is not GET-only: {methods}"
    # The POST-capable set is EXACTLY login + authz allowlist + confined writes.
    posters = {r.path for r in web_app.routes if "POST" in set(getattr(r, "methods", None) or ())}
    assert posters == post_ok, f"unexpected POST routes: {posters}"

    # (2) EVERY kbweb module imports zero KNOWLEDGE-write functions — EXCEPT the
    #     single confined module kbweb.views_write, which may reach ONLY the A11-3
    #     allowlist {save_for_review, supersede_for_review, link, reject, dismiss}
    #     and never promote/ingest/supersede (trust escalation is owner-only).
    import importlib
    import inspect
    import pkgutil

    import kbweb

    for mod_info in pkgutil.iter_modules(kbweb.__path__):
        mod = importlib.import_module(f"kbweb.{mod_info.name}")
        leaked = WRITE_NAMES & set(dir(mod))
        if mod_info.name == CONFINED_WRITE_MODULE:
            assert leaked == CONFINED_WRITE_NAMES, (
                f"views_write write surface drifted: {leaked} != {CONFINED_WRITE_NAMES}"
            )
            for forbidden in ("promote", "ingest", "ingest_in", "supersede", "queue_review_in"):
                assert forbidden not in dir(mod), (
                    f"{forbidden} must never be reachable from the web"
                )
        else:
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

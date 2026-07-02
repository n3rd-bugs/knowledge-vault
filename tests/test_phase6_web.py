"""Phase 6b — read-only web UI (Starlette). In-process via TestClient against
kb_test; Ollama mocked by conftest. Proves the read-only invariant (Inv. 11).
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


def test_web_is_read_only(web_app):
    # (1) every route is GET-only except /login (which is GET+POST for auth,
    #     performs NO kbcore write); nothing anywhere allows PUT/DELETE/PATCH.
    for route in web_app.routes:
        methods = set(route.methods or ())
        assert not (methods & {"PUT", "DELETE", "PATCH"}), f"{route.path} exposes a mutation verb"
        if route.path == "/login":
            assert methods <= {"GET", "POST", "HEAD"}
        else:
            assert methods <= {"GET", "HEAD"}, f"{route.path} is not GET-only: {methods}"

    # (2) the web module imports zero KB-write functions (Invariant 11).
    from kbweb import server as web

    assert WRITE_NAMES.isdisjoint(dir(web)), "a write function leaked into the read-only UI"


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

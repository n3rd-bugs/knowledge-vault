"""Phase 6a — REST facade (FastAPI). In-process ASGI via Starlette TestClient
against kb_test; Ollama mocked by conftest. Auth reuses kb-mcp's bearer.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from kbcore.config import settings
from kbcore.ingest import ingest

TEST_TOKEN = "kbtest-" + "0" * 32
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture()
def client(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbrest.server import build_app

    return TestClient(build_app())


def test_rest_401_without_token(client):
    assert client.get("/api/stats").status_code == 401


def test_rest_401_wrong_token(client):
    r = client.get("/api/stats", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_rest_search_returns_hits_with_code_ref_and_trust(client):
    ingest(
        "motor-ctl",
        "gotcha",
        "DRV8706 SPI polarity",
        "The DRV8706 needs SPI mode 1; mode 0 reads zeros.",
        tags=["protocol:spi"],
        code_ref={"repo": "motor-ctl", "path": "src/drv8706.c", "lines": "88-131"},
    )
    r = client.get("/api/search", params={"q": "DRV8706 SPI"}, headers=AUTH)
    assert r.status_code == 200
    hits = r.json()
    assert hits and any(h["title"] == "DRV8706 SPI polarity" for h in hits)
    top = next(h for h in hits if h["title"] == "DRV8706 SPI polarity")
    assert top["trust"] == "verified"
    assert top["code_ref"]["path"] == "src/drv8706.c"


def test_rest_get_item_matches_kbcore(client):
    item_id = ingest("p", "note", "hello item", "a body about widgets")
    r = client.get(f"/api/items/{item_id}", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["item_id"] == item_id
    assert body["title"] == "hello item"
    assert "widgets" in body["body"]


def test_rest_post_item_then_search_finds_it(client):
    r = client.post(
        "/api/items",
        headers=AUTH,
        json={
            "project": "p",
            "type": "note",
            "title": "teal note",
            "body": "the sky-color is teal-42",
            "tags": ["protocol:i2c"],
            "code_ref": {"repo": "r", "path": "m.c"},
        },
    )
    assert r.status_code == 201
    item_id = r.json()["item_id"]
    assert isinstance(item_id, int)

    found = client.get("/api/search", params={"q": "teal-42 sky-color"}, headers=AUTH).json()
    assert any(h["item_id"] == item_id for h in found)


def test_rest_item_404_is_clean_not_500(client):
    r = client.get("/api/items/999999", headers=AUTH)
    assert r.status_code == 404
    assert "error" in r.json() and "traceback" not in r.text.lower()


def test_rest_bad_method_405(client):
    assert client.delete("/api/stats", headers=AUTH).status_code == 405


def test_rest_stats_shape(client):
    ingest("p", "note", "n", "body")
    s = client.get("/api/stats", headers=AUTH).json()
    assert s["items_total"] >= 1
    assert "by_type" in s and "embed_model" in s and "chunks" in s


def test_rest_supersede_and_link_and_review(client):
    old = ingest("p", "gotcha", "old gotcha", "was wrong")
    # supersede with a new body
    r = client.post(
        f"/api/items/{old}/supersede",
        headers=AUTH,
        json={"reason": "corrected", "new_body": "the corrected wisdom"},
    )
    assert r.status_code == 200
    new_id = r.json()["new_item_id"]
    assert isinstance(new_id, int)

    bug = ingest("p", "bug", "bug", "a bug body")
    fix = ingest("p", "note", "fix", "a fix body")
    lr = client.post(
        "/api/links",
        headers=AUTH,
        json={"src_item_id": fix, "rel": "fixes", "dst_item_id": bug},
    )
    assert lr.status_code == 201 and isinstance(lr.json()["relation_id"], int)

    # a bad relation → clean 400, not 500
    bad = client.post(
        "/api/links",
        headers=AUTH,
        json={"src_item_id": fix, "rel": "not-a-rel", "dst_item_id": bug},
    )
    assert bad.status_code == 400 and "error" in bad.json()


# --------------------------------------------------------------------------
# POST /api/enrol — a machine exchanges a one-time code for its own credential.
# --------------------------------------------------------------------------


def test_enrol_redeems_a_code_for_the_machines_own_credential(client, monkeypatch):
    from kbcore import enrolment, scope

    with scope.use_principal(scope.OWNER):
        issued = enrolment.create_code()
    r = client.post(
        "/api/enrol",
        json={"machine": "laptop"},
        headers={"Authorization": f"Bearer {issued.code}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"] == TEST_TOKEN  # an owner code yields the owner credential
    assert body["is_owner"] is True


def test_enrol_is_single_use_over_http(client):
    from kbcore import enrolment, scope

    with scope.use_principal(scope.OWNER):
        issued = enrolment.create_code()
    h = {"Authorization": f"Bearer {issued.code}"}
    assert client.post("/api/enrol", json={}, headers=h).status_code == 200
    second = client.post("/api/enrol", json={}, headers=h)
    assert second.status_code >= 400, "a redeemed code was accepted twice"


def test_a_real_bearer_cannot_use_the_enrol_route(client):
    """A credential that already works must not be able to mint a second one.

    The route is reachable only by the zero-authority enrolling identity; anyone
    else gets the same unknown-path 404 the rest of the surface uses, so the
    route's existence is not an oracle either.
    """
    r = client.post("/api/enrol", json={"machine": "x"}, headers=AUTH)
    assert r.status_code == 404


def test_enrol_rejects_an_unknown_code(client):
    from kbcore import enrolment

    r = client.post(
        "/api/enrol",
        json={},
        headers={"Authorization": f"Bearer {enrolment.mint_code()}"},
    )
    assert r.status_code >= 400
    assert TEST_TOKEN not in r.text


def test_enrol_route_is_not_reachable_without_any_credential(client):
    assert client.post("/api/enrol", json={}).status_code == 401


# --------------------------------------------------------------------------
# GET /api/setup/{name} — a new machine fetches the hooks instead of a model
# reproducing ~23k tokens of shell into the caller's context.
# --------------------------------------------------------------------------


def test_setup_scripts_are_served_verbatim(client):
    from pathlib import Path

    for name in ("install.sh", "flush-session-to-kb.sh", "kb-session-brief.sh", "kb-mcp-call.sh"):
        r = client.get(f"/api/setup/{name}", headers=AUTH)
        assert r.status_code == 200, name
        on_disk = (
            Path(__file__).resolve().parents[1] / "src" / "kbmcp" / "setup_assets" / name
        ).read_text()
        assert r.text == on_disk, f"{name} was altered in transit"


def test_an_enrolment_code_may_fetch_the_scripts_without_spending_itself(client):
    """A brand-new machine holds nothing but the code, so it must be able to
    download the installer — and doing so must NOT redeem the code, or the
    bootstrap would consume its own credential before using it."""
    from kbcore import enrolment, scope

    with scope.use_principal(scope.OWNER):
        issued = enrolment.create_code()
    h = {"Authorization": f"Bearer {issued.code}"}
    assert client.get("/api/setup/install.sh", headers=h).status_code == 200
    # Still redeemable afterwards.
    assert client.post("/api/enrol", json={}, headers=h).status_code == 200


def test_the_setup_route_is_not_a_file_read_primitive(client):
    for bad in (
        "../../../etc/passwd",
        "..%2f..%2fetc%2fpasswd",
        ".env",
        "SKILL.md",
        "server.py",
    ):
        r = client.get(f"/api/setup/{bad}", headers=AUTH)
        assert r.status_code in (404, 400), f"{bad!r} was served"
        assert "root:" not in r.text


def test_setup_scripts_need_a_credential(client):
    assert client.get("/api/setup/install.sh").status_code == 401

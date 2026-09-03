"""Wave H (review #8 H15 tail): the five REST routes no test exercised anywhere —
GET /api/timeline, GET /api/similar/{id}, GET /api/trace/{id}, GET /api/review and
the owner-gated write POST /api/review/{id}/resolve — plus the ``hook_version``
field on POST /api/ingest-session.

In-process ASGI (Starlette TestClient) against kb_test; Ollama mocked by conftest.
The owner is kb-mcp's bearer; the non-owner is a MINTED ``kbg_`` grantee, so the
owner gate is exercised through the real bearer path, not a scope monkeypatch.
"""

from __future__ import annotations

import inspect

import pytest
from starlette.testclient import TestClient

from kbcore import ingest_queue, principals, review
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


def _grantee_auth(name: str = "tail-grantee") -> dict[str, str]:
    """A real per-project-style grantee credential (reviewer role, no grants)."""
    _pid, token = principals.create_principal(name, kind="token", role="reviewer")
    assert token
    return {"Authorization": f"Bearer {token}"}


def _queued_unverified(
    project: str = "rq", title: str = "an unverified extraction"
) -> tuple[int, int]:
    item_id = ingest(project, "gotcha", title, f"{title} body", trust="unverified")
    assert item_id is not None
    qid = review.queue_review(item_id, "unverified_extraction", {"quote": "grounding"})
    return item_id, qid


def _open_qids(client) -> set[int]:
    r = client.get("/api/review", headers=AUTH)
    assert r.status_code == 200, r.text
    return {e["queue_id"] for e in r.json()}


# --- GET /api/timeline --------------------------------------------------------------


def test_timeline_lists_a_projects_items_oldest_first_and_honours_limit(client):
    ids = [ingest("tl", "note", f"step {i}", f"timeline body {i}") for i in range(3)]

    r = client.get("/api/timeline", params={"project": "tl"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert [it["item_id"] for it in r.json()] == ids

    recent = client.get("/api/timeline", params={"project": "tl", "limit": 2}, headers=AUTH)
    assert [it["item_id"] for it in recent.json()] == ids[1:], "the MOST RECENT 2, still ascending"

    future = client.get(
        "/api/timeline", params={"project": "tl", "from": "2099-01-01"}, headers=AUTH
    )
    assert future.status_code == 200 and future.json() == []

    assert client.get("/api/timeline", headers=AUTH).status_code == 422, "project is required"
    assert client.get("/api/timeline", params={"project": "tl"}).status_code == 401


# --- GET /api/similar/{id} ----------------------------------------------------------


def test_similar_returns_neighbours_never_the_anchor_and_404s_cleanly(client):
    a = ingest("sim", "bug", "SPI mode wrong", "the DRV8706 reads zeros in SPI mode 0")
    b = ingest("sim", "note", "SPI fix", "set SPI mode 1 on the DRV8706")
    c = ingest("sim", "note", "unrelated", "a totally different topic about lunch")

    r = client.get(f"/api/similar/{a}", params={"k": 2}, headers=AUTH)
    assert r.status_code == 200, r.text
    hits = r.json()
    assert 1 <= len(hits) <= 2
    assert a not in {h["item_id"] for h in hits}, "never the anchor itself"
    assert {h["item_id"] for h in hits} <= {b, c}
    assert all(isinstance(h["score"], float) for h in hits)

    missing = client.get("/api/similar/999999", headers=AUTH)
    assert missing.status_code == 404
    assert "error" in missing.json() and "traceback" not in missing.text.lower()


# --- GET /api/trace/{id} ------------------------------------------------------------


def test_trace_walks_the_typed_edges_around_a_node(client):
    bug = ingest("tr", "bug", "a bug", "bug body")
    fix = ingest("tr", "note", "a fix", "fix body")
    linked = client.post(
        "/api/links", headers=AUTH, json={"src_item_id": fix, "rel": "fixes", "dst_item_id": bug}
    )
    assert linked.status_code == 201

    r = client.get(f"/api/trace/{bug}", params={"kind": "item", "depth": 1}, headers=AUTH)
    assert r.status_code == 200, r.text
    tree = r.json()
    assert tree["id"] == bug and tree["kind"] == "item"
    assert any(e["rel"] == "fixes" and e["node"]["id"] == fix for e in tree["edges"])

    assert client.get("/api/trace/999999", headers=AUTH).status_code == 404
    bad_kind = client.get(f"/api/trace/{bug}", params={"kind": "galaxy"}, headers=AUTH)
    assert bad_kind.status_code == 400 and "error" in bad_kind.json()


# --- GET /api/review (owner-only) --------------------------------------------------


def test_the_review_queue_is_owner_only_on_the_rest_surface(client):
    _item_id, qid = _queued_unverified(title="owner-eyes-only title")

    assert qid in _open_qids(client)

    denied = client.get("/api/review", headers=_grantee_auth())
    assert denied.status_code == 400, denied.text
    assert "error" in denied.json()
    assert "owner-eyes-only" not in denied.text, "a refusal must carry no queue content"

    assert client.get("/api/review").status_code == 401


# --- POST /api/review/{id}/resolve (owner-gated WRITE) -------------------------------


def test_resolve_promote_flips_trust_and_closes_the_row(client):
    item_id, qid = _queued_unverified()

    r = client.post(f"/api/review/{qid}/resolve", headers=AUTH, json={"action": "promote"})
    assert r.status_code == 200, r.text
    assert r.json() == {"item_id": item_id, "action": "promote"}

    assert client.get(f"/api/items/{item_id}", headers=AUTH).json()["trust"] == "verified"
    assert qid not in _open_qids(client)


def test_resolve_reject_archives_the_item_never_deletes_it(client):
    item_id, qid = _queued_unverified()

    r = client.post(f"/api/review/{qid}/resolve", headers=AUTH, json={"action": "reject"})
    assert r.status_code == 200, r.text
    assert r.json() == {"item_id": item_id, "action": "reject"}

    detail = client.get(f"/api/items/{item_id}", headers=AUTH)
    assert detail.status_code == 200, "Invariant 5: a rejected item is a status flip, not a delete"
    assert detail.json()["status"] == "archived"
    assert qid not in _open_qids(client)


def test_resolve_is_owner_only_and_a_refusal_changes_nothing(client):
    item_id, qid = _queued_unverified()

    denied = client.post(
        f"/api/review/{qid}/resolve", headers=_grantee_auth(), json={"action": "promote"}
    )
    assert denied.status_code == 400, denied.text
    assert "error" in denied.json()

    assert client.get(f"/api/items/{item_id}", headers=AUTH).json()["trust"] == "unverified", (
        "V4: trust escalation from a non-owner must be impossible on every surface"
    )
    assert qid in _open_qids(client), "the row is still open"
    assert client.post(f"/api/review/{qid}/resolve", json={"action": "promote"}).status_code == 401


def test_resolve_rejects_bad_requests_cleanly(client):
    item_id, qid = _queued_unverified()

    unknown_action = client.post(
        f"/api/review/{qid}/resolve", headers=AUTH, json={"action": "explode"}
    )
    assert unknown_action.status_code == 400 and "error" in unknown_action.json()

    no_target = client.post(
        f"/api/review/{qid}/resolve", headers=AUTH, json={"action": "supersede"}
    )
    assert no_target.status_code == 400 and "new_item_id" in no_target.json()["error"]

    missing_row = client.post("/api/review/999999/resolve", headers=AUTH, json={"action": "reject"})
    assert missing_row.status_code == 404 and "traceback" not in missing_row.text.lower()

    assert qid in _open_qids(client), "none of the refusals resolved the row"
    assert client.get(f"/api/items/{item_id}", headers=AUTH).json()["trust"] == "unverified"


# --- POST /api/ingest-session: hook_version ----------------------------------------


def _submit(client, **extra) -> dict:
    payload = {"project": "hv", "transcript": "chunk\n", "session_uri": "session:hv", **extra}
    r = client.post("/api/ingest-session", headers=AUTH, json=payload)
    assert r.status_code == 202, r.text
    return r.json()


def test_ingest_session_accepts_the_hook_version_field(client):
    body = _submit(client, hook_version="beef1234cafe")
    assert isinstance(body["queue_id"], int) and body["duplicate"] is False


def test_hook_version_is_a_closed_shape(client):
    r = client.post(
        "/api/ingest-session",
        headers=AUTH,
        json={"project": "hv", "transcript": "chunk\n", "hook_version": "x" * 65},
    )
    assert r.status_code == 422, "a 64-char label is the ceiling (a stamp is 12 hex)"


_ENQUEUE_THREADS_HOOK_VERSION = "hook_version" in inspect.signature(ingest_queue.enqueue).parameters


@pytest.mark.skipif(
    not _ENQUEUE_THREADS_HOOK_VERSION,
    reason=(
        "H15 contract pending: ingest_queue.enqueue(hook_version=) stamping "
        "meta['hook_version'] (ingest_queue.py is pkg-pipeline's) plus the one-line "
        "pass-through in kbrest.server.api_ingest_session — this test arms itself when "
        "the kwarg lands"
    ),
)
def test_hook_version_lands_in_the_queue_rows_meta(client):
    from kbcore import db

    qid = _submit(client, hook_version="beef1234cafe")["queue_id"]
    with db.connect() as conn:
        meta = conn.execute("SELECT meta FROM ingest_queue WHERE id = %s", (qid,)).fetchone()[0]
    assert meta["hook_version"] == "beef1234cafe"

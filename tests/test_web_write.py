"""A11-3 Branch B — the CONFINED web-write surface (kbweb.views_write) behavior.

The kbcore chokepoints are proven in test_web_write_core.py (trust/quarantine/
lifecycle) and test_a11_3_write_holes.py; this file pins the WEB-LAYER contract the
amendment adds: capability zone walls (byte-404 for the under-capped), a live-
identity CSRF binding, a closed request shape, and a UNIFORM no-oracle error
contract — plus cookie hardening and the stored-XSS escape. Real DB in kb_test;
Ollama mocked by conftest; TestClient over https so the __Host-/Secure cookies
round-trip.
"""

from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from kbcore import db, principals, review, scope
from kbcore.config import settings
from kbcore.ingest import ingest
from kbcore.items import get_item
from kbcore.search import search
from tests.test_web_caps import GHOST_PATH, assert_ghost_identical

TEST_TOKEN = "kbtest-" + "0" * 32
BASE = "https://testserver"


@pytest.fixture()
def app(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbweb.server import build_app

    return build_app()


@pytest.fixture()
def world(app):
    a = ingest("proj-a", "note", "seeded note", "the widget uses SPI mode 1")
    nda = ingest("nda-hidden", "note", "hidden secret", "withheld NDA body")
    return {"a": a, "nda": nda}


def _make(app, role: str, name: str, projects: dict[str, bool]) -> TestClient:
    _pid, token = principals.create_principal(name, kind="token", role=role)
    for project, rw in projects.items():
        principals.grant(name, project, rw=rw)
    c = TestClient(app, base_url=BASE)
    assert c.post("/login", data={"token": token}).status_code == 200
    return c


def _owner(app) -> TestClient:
    c = TestClient(app, base_url=BASE)
    assert c.post("/login", data={"token": TEST_TOKEN}).status_code == 200
    return c


def _csrf(client: TestClient, path: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert m, f"no CSRF token minted on {path}"
    return m.group(1)


def _pending_of(item_title_substr: str) -> tuple[int, int]:
    """(item_id, queue_id) of the newest web-contribution row (test convenience)."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT rq.item_id, rq.id FROM review_queue rq JOIN items i ON i.id = rq.item_id "
            "WHERE rq.reason = 'web_contribution' AND i.title LIKE %s ORDER BY rq.id DESC LIMIT 1",
            (f"%{item_title_substr}%",),
        ).fetchone()
    assert row is not None, "no web_contribution row found"
    return int(row[0]), int(row[1])


def _search_ids(query: str) -> list[int]:
    return [h.item_id for h in search(query, hops=0, tool=None)]


# --- 1. the ghost matrix (zone walls, findings 22) ----------------------------


WRITE_PATHS = ("/save", "/supersede/1", "/link/1", "/resolve/1")


def test_undercapped_write_routes_are_ghost_identical(app, world):
    """AC: a guest AND a reader POSTing (or probing any method of) each write route
    get bytes identical to /no-such-path — the Layer-1 zone wall answers before
    routing, so the write surface's very existence is invisible."""
    for role in ("guest", "reader"):
        c = _make(app, role, f"probe-{role}", {"proj-a": False})
        assert c.get(GHOST_PATH).status_code == 404
        for path in WRITE_PATHS:
            assert_ghost_identical(c, path)


def test_contributor_lacks_resolve_zone(app, world):
    """A contributor holds CAP_SAVE but NOT CAP_REVIEW_RESOLVE: the /resolve zone is
    byte-404 for them, while /save is reachable (200 form)."""
    c = _make(app, "contributor", "c-noresolve", {"proj-a": True})
    assert_ghost_identical(c, "/resolve/1")
    assert c.get("/save").status_code == 200


def test_capped_withheld_vs_missing_id_are_byte_identical(app, world):
    """AC: a CAPPED contributor with valid CSRF hitting a WITHHELD-project id vs a
    NONEXISTENT id gets byte-identical responses (no existence / NDA-name oracle)."""
    c = _make(app, "contributor", "c-oracle", {"proj-a": True})
    csrf = _csrf(c, "/save")
    for path_fmt in ("/supersede/{}", "/link/{}"):
        withheld = c.post(
            path_fmt.format(world["nda"]),
            data={
                "csrf": csrf,
                "new_body": "x",
                "reason": "y",
                "rel": "mentions",
                "dst_id": world["a"],
            },
            follow_redirects=False,
        )
        missing = c.post(
            path_fmt.format(9_999_999),
            data={
                "csrf": csrf,
                "new_body": "x",
                "reason": "y",
                "rel": "mentions",
                "dst_id": world["a"],
            },
            follow_redirects=False,
        )
        assert withheld.status_code == missing.status_code == 404, path_fmt
        assert withheld.content == missing.content, f"{path_fmt}: withheld body != missing body"
        assert "nda-hidden" not in withheld.text, "NDA project name leaked"


# --- 2. the contributor save lifecycle (findings 5/6/7) -----------------------


def test_contributor_save_lands_pending_unverified_queued_and_absent(app):
    ingest("proj-a", "note", "anchor", "anchor body")
    c = _make(app, "contributor", "c-save", {"proj-a": True})
    csrf = _csrf(c, "/save")
    r = c.post(
        "/save",
        data={
            "csrf": csrf,
            "project": "proj-a",
            "type": "gotcha",
            "title": "flaky deploy zonkitude",
            "body": "body about zonkitude",
            "tags": "tool:pytest",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/save?saved=1"
    item_id, qid = _pending_of("zonkitude")
    assert get_item(item_id).status == "pending"
    with db.connect() as conn:
        trust = conn.execute(
            "SELECT s.trust FROM sources s JOIN items i ON i.source_id=s.id WHERE i.id=%s",
            (item_id,),
        ).fetchone()[0]
    assert trust == "unverified"
    assert not review.list_open_review() == [], "a review row exists"  # owner sees it
    # ABSENT from search until an owner promotes (no injection channel)
    assert item_id not in _search_ids("zonkitude")
    review.promote(qid)
    assert item_id in _search_ids("zonkitude")  # now visible


def test_forged_trust_or_kind_field_is_uniform_400(app):
    ingest("proj-a", "note", "anchor", "anchor body")
    c = _make(app, "contributor", "c-forge", {"proj-a": True})
    csrf = _csrf(c, "/save")
    for forged in ({"trust": "verified"}, {"kind": "git"}, {"status": "active"}, {"uri": "x"}):
        r = c.post(
            "/save",
            data={
                "csrf": csrf,
                "project": "proj-a",
                "type": "note",
                "title": "forged attempt",
                "body": "b",
                **forged,
            },
            follow_redirects=False,
        )
        assert r.status_code == 400, f"forged {forged} must be a uniform 400"
    with db.connect() as conn:
        n = conn.execute("SELECT count(*) FROM items WHERE title = 'forged attempt'").fetchone()[0]
    assert n == 0, "no forged item may be created"


def test_forged_consolidation_type_rejected(app):
    ingest("proj-a", "note", "anchor", "anchor body")
    c = _make(app, "contributor", "c-type", {"proj-a": True})
    csrf = _csrf(c, "/save")
    r = c.post(
        "/save",
        data={
            "csrf": csrf,
            "project": "proj-a",
            "type": "summary",
            "title": "fake summary",
            "body": "b",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400  # WEB_ITEM_TYPES excludes summary/report


def test_save_to_withheld_project_is_uniform_no_name(app):
    ingest("proj-a", "note", "anchor", "anchor body")
    ingest("secret-proj", "note", "secret", "secret body")  # exists, not granted rw
    c = _make(app, "contributor", "c-withheld", {"proj-a": True})
    csrf = _csrf(c, "/save")
    r = c.post(
        "/save",
        data={
            "csrf": csrf,
            "project": "secret-proj",
            "type": "note",
            "title": "sneak",
            "body": "b",
        },
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert "secret-proj" not in r.text, "the project name must never appear in the rejection"
    with db.connect() as conn:
        n = conn.execute("SELECT count(*) FROM items WHERE title = 'sneak'").fetchone()[0]
    assert n == 0


# --- 3. CSRF bound to the LIVE identity (findings 13/19/25) --------------------


def test_missing_csrf_is_403_no_write(app):
    ingest("proj-a", "note", "anchor", "anchor body")
    c = _make(app, "contributor", "c-nocsrf", {"proj-a": True})
    r = c.post(
        "/save",
        data={"project": "proj-a", "type": "note", "title": "nocsrf", "body": "b"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM items WHERE title='nocsrf'").fetchone()[0] == 0


def test_cross_identity_csrf_token_is_rejected(app):
    """AC (finding 13): a CSRF token minted on grantee A's form, replayed under
    grantee B's session, must 403 and write nothing — the token binds to the LIVE
    identity, so A's token authorizes neither B nor the owner."""
    ingest("proj-a", "note", "anchor", "anchor body")
    a = _make(app, "contributor", "alice", {"proj-a": True})
    b = _make(app, "contributor", "bob", {"proj-a": True})
    a_csrf = _csrf(a, "/save")
    r = b.post(
        "/save",
        data={"csrf": a_csrf, "project": "proj-a", "type": "note", "title": "crossid", "body": "b"},
        follow_redirects=False,
    )
    assert r.status_code == 403, "grantee A's CSRF must not authorize grantee B"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM items WHERE title='crossid'").fetchone()[0] == 0
    # and B's own freshly-minted token DOES work (guards the inverse silent breakage)
    b_csrf = _csrf(b, "/save")
    r = b.post(
        "/save",
        data={"csrf": b_csrf, "project": "proj-a", "type": "note", "title": "ownid", "body": "b"},
        follow_redirects=False,
    )
    assert r.status_code == 303


# --- 4. reviewer: reject/dismiss only, NEVER promote (findings 3/9/12/15) ------


def test_reviewer_can_reject_and_dismiss_granted_rows(app):
    ingest("proj-a", "note", "anchor", "anchor body")
    contrib = _make(app, "contributor", "rev-contrib", {"proj-a": True})
    csrf = _csrf(contrib, "/save")
    contrib.post(
        "/save",
        data={
            "csrf": csrf,
            "project": "proj-a",
            "type": "note",
            "title": "to reject wibble",
            "body": "b",
        },
        follow_redirects=False,
    )
    item_id, qid = _pending_of("wibble")
    rv = _make(app, "reviewer", "rev1", {"proj-a": True})
    rcsrf = _csrf(rv, "/review")
    r = rv.post(f"/resolve/{qid}", data={"csrf": rcsrf, "action": "reject"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/review"
    assert get_item(item_id).status == "archived"


def test_reviewer_cannot_promote_via_any_web_path(app):
    ingest("proj-a", "note", "anchor", "anchor body")
    contrib = _make(app, "contributor", "np-contrib", {"proj-a": True})
    csrf = _csrf(contrib, "/save")
    contrib.post(
        "/save",
        data={
            "csrf": csrf,
            "project": "proj-a",
            "type": "note",
            "title": "no promote flarn",
            "body": "b",
        },
        follow_redirects=False,
    )
    item_id, qid = _pending_of("flarn")
    rv = _make(app, "reviewer", "rev2", {"proj-a": True})
    rcsrf = _csrf(rv, "/review")
    # action=promote is not a web action → uniform 400 (unknown action), no escalation
    r = rv.post(
        f"/resolve/{qid}", data={"csrf": rcsrf, "action": "promote"}, follow_redirects=False
    )
    assert r.status_code == 400
    assert get_item(item_id).status == "pending", "the item must NOT be promoted"
    with db.connect() as conn:
        trust = conn.execute(
            "SELECT s.trust FROM sources s JOIN items i ON i.source_id=s.id WHERE i.id=%s",
            (item_id,),
        ).fetchone()[0]
    assert trust == "unverified", "trust must never escalate from the web"


def test_reviewer_read_only_grant_cannot_resolve(app):
    """The composition contract: CAP_REVIEW_RESOLVE ANDs an rw grant. A reviewer with
    a READ-ONLY grant lists the row but cannot resolve it (uniform 403)."""
    ingest("proj-a", "note", "anchor", "anchor body")
    contrib = _make(app, "contributor", "ro-contrib", {"proj-a": True})
    csrf = _csrf(contrib, "/save")
    contrib.post(
        "/save",
        data={
            "csrf": csrf,
            "project": "proj-a",
            "type": "note",
            "title": "readonly glomp",
            "body": "b",
        },
        follow_redirects=False,
    )
    _item_id, qid = _pending_of("glomp")
    rv = _make(app, "reviewer", "rev-ro", {"proj-a": False})  # READ-only grant
    rcsrf = _csrf(rv, "/review")
    r = rv.post(f"/resolve/{qid}", data={"csrf": rcsrf, "action": "reject"}, follow_redirects=False)
    assert r.status_code == 403


def test_reviewer_review_detail_hides_withheld_project_ids(app):
    """AC: a reviewer's /review shows their granted rows' safe fields but NEVER a
    withheld project's title/id from a contradiction detail. Pinned at BOTH the web
    page (title absent) AND the kbcore scrub that MCP kb_review_list inherits (the
    withheld b_id + evidence entry are stripped from the returned detail)."""
    granted = ingest("proj-a", "note", "granted contra item", "granted body", trust="unverified")
    withheld = ingest("nda-secret", "note", "NDA-CONTRA-TITLE", "withheld body")
    review.queue_review(
        granted,
        "contradiction",
        {
            "in_favor_a": 1,
            "in_favor_b": 1,
            "weighted_a": 1,
            "weighted_b": 1,
            "a_id": granted,
            "b_id": withheld,
            "evidence": [
                {
                    "item_id": granted,
                    "title": "granted contra item",
                    "side": "a",
                    "trust": "unverified",
                },
                {
                    "item_id": withheld,
                    "title": "NDA-CONTRA-TITLE",
                    "side": "b",
                    "trust": "verified",
                },
            ],
        },
    )
    rv = _make(app, "reviewer", "rev-scrub", {"proj-a": True})
    page = rv.get("/review")
    assert page.status_code == 200
    assert "granted contra item" in page.text
    assert "NDA-CONTRA-TITLE" not in page.text, "withheld title leaked past the row gate"
    assert f'href="/item/{withheld}"' not in page.text, "a withheld item link leaked"
    # the kbcore scrub (inherited by MCP kb_review_list): resolve the reviewer and
    # inspect the returned detail directly — no withheld id/title survives.
    reviewer = principals.resolve_name("rev-scrub")
    with scope.use_principal(reviewer):
        entry = next(e for e in review.list_open_review() if e.reason == "contradiction")
    # A11-3 FIX 4 tightened the old blocklist to an ALLOWLIST: a contradiction detail
    # carries only cross-project id refs + free-text evidence, NONE of which is on the
    # reviewer allowlist, so the ENTIRE contradiction body is dropped for a non-owner
    # (the former blocklist let the readable a_id + granted evidence entry through). No
    # withheld id/title can ride the JSONB past the scope-gated row/item JOIN.
    assert "a_id" not in entry.detail, "contradiction ids are not allowlisted (dropped)"
    assert "b_id" not in entry.detail, "the withheld b_id must be stripped"
    assert "evidence" not in entry.detail, "the free-text evidence array is dropped whole"
    assert "in_favor_a" not in entry.detail and "weighted_a" not in entry.detail


def test_review_detail_scrub_is_an_allowlist(app):
    """FIX 4 (A11-3 deliverable 4): the non-owner detail scrub is an ALLOWLIST, not a
    blocklist — a contradiction/triage row referencing a WITHHELD project shows a
    reviewer NONE of that project's ids/titles/free-text, and a brand-new UNKNOWN
    detail key is dropped for the reviewer while the OWNER still sees verbatim detail
    (the distinguishing property a blocklist could never guarantee)."""
    granted = ingest("proj-a", "note", "granted triage item", "granted body", trust="unverified")
    withheld = ingest("nda-secret", "note", "Q-SECRET-TITLE", "withheld body")
    review.queue_review(
        granted,
        "contradiction",
        {
            "rejected_count": 2,  # allowlisted, non-id → survives
            "triage": {
                "action": "reject_duplicate",  # allowlisted → survives
                "dup_of": withheld,  # id ref into the WITHHELD project → dropped
                "rationale": "Q-SECRET-RATIONALE",  # free text → dropped
                "model": "qwen3",  # non-allowlisted triage key → dropped
            },
            "a_id": granted,  # not allowlisted → dropped (even though readable)
            "b_id": withheld,  # withheld + not allowlisted → dropped
            "evidence": [{"item_id": withheld, "title": "Q-SECRET-TITLE"}],  # dropped whole
            "brand_new_future_key": "FUTURE-LEAK",  # unknown key → dropped for reviewer
        },
    )
    reviewer = _make(app, "reviewer", "rev-allow", {"proj-a": True})  # grant on P, not Q
    assert reviewer.get("/review").status_code == 200  # the reviewer may list the row
    rp = principals.resolve_name("rev-allow")
    with scope.use_principal(rp):
        d = next(e for e in review.list_open_review() if e.item_id == granted).detail
    # allowlisted, non-id keys survive
    assert d.get("rejected_count") == 2
    assert d.get("triage", {}).get("action") == "reject_duplicate"
    # every trace of the withheld project Q is gone, plus all free-text and unknown keys
    assert "Q-SECRET-TITLE" not in str(d)
    assert "Q-SECRET-RATIONALE" not in str(d), "free-text rationale must be dropped"
    assert "dup_of" not in d.get("triage", {}), "a withheld id ref is dropped"
    assert "model" not in d.get("triage", {}), "a non-allowlisted triage key is dropped"
    assert "a_id" not in d and "b_id" not in d and "evidence" not in d
    assert "brand_new_future_key" not in d, "an unknown/future key is dropped for a reviewer"
    # the OWNER still sees the verbatim detail (scrub skipped) — behavior unchanged
    owner_detail = next(e for e in review.list_open_review() if e.item_id == granted).detail
    assert owner_detail.get("brand_new_future_key") == "FUTURE-LEAK"
    assert owner_detail.get("a_id") == granted
    assert owner_detail.get("triage", {}).get("rationale") == "Q-SECRET-RATIONALE"


def test_availability_error_is_constant_503_no_reflection(app, monkeypatch):
    """FIX 2 (A11-3 availability-error reflection): an EmbedderUnavailable raised deep
    in ingest's embed leg during /save must NOT reach server._bad_request (which
    interpolates the raw exception message into a 400 body). The confined write surface
    maps any residual availability KBError to a CONSTANT 503 reflecting no state."""
    from kbcore.errors import EmbedderUnavailable

    ingest("proj-a", "note", "anchor", "anchor body")
    secret_msg = "ollama unreachable at 10.0.0.5:11434 model qwen3-embed"

    def _boom(texts):
        raise EmbedderUnavailable(secret_msg)

    monkeypatch.setattr("kbcore.embed.embed_texts", _boom)
    c = _make(app, "contributor", "c-avail", {"proj-a": True})
    csrf = _csrf(c, "/save")  # GET renders the form (no embed) so the token still mints
    r = c.post(
        "/save",
        data={
            "csrf": csrf,
            "project": "proj-a",
            "type": "note",
            "title": "availwibble",
            "body": "b",
        },
        follow_redirects=False,
    )
    assert r.status_code == 503, "a residual availability KBError must map to 503, not 400"
    assert secret_msg not in r.text, "the raw exception message must never be reflected"
    assert "10.0.0.5" not in r.text and "ollama" not in r.text.lower()
    with db.connect() as conn:
        n = conn.execute("SELECT count(*) FROM items WHERE title='availwibble'").fetchone()[0]
    assert n == 0, "the failed save must persist nothing (transaction rolled back)"


def test_duplicate_save_is_byte_identical_to_fresh_save(app):
    """FIX 3 (A11-3 global-dedupe cross-project oracle): a /save whose content already
    exists in a DIFFERENT, WITHHELD project (global content-hash dedupe) must be
    INDISTINGUISHABLE from a fresh unique save — same status, body, and Location — so a
    contributor cannot detect that identical content exists somewhere they cannot read."""
    # identical content seeded in a project the contributor cannot read; kind/uri match
    # the web save's hardwired (manual, '') dedupe namespace, so the hashes collide.
    dup_title, dup_body = "cross-project echo vornak", "echo body vornak"
    ingest("nda-withheld", "note", dup_title, dup_body, kind="manual", uri="")
    c = _make(app, "contributor", "c-dup", {"proj-a": True})
    csrf = _csrf(c, "/save")
    dup = c.post(
        "/save",
        data={
            "csrf": csrf,
            "project": "proj-a",
            "type": "note",
            "title": dup_title,
            "body": dup_body,
        },
        follow_redirects=False,
    )
    fresh = c.post(
        "/save",
        data={
            "csrf": csrf,
            "project": "proj-a",
            "type": "note",
            "title": "unique fresh vornak2",
            "body": "fresh body vornak2",
        },
        follow_redirects=False,
    )
    assert dup.status_code == fresh.status_code == 303, "duplicate must not be a distinct 200 page"
    assert dup.headers["location"] == fresh.headers["location"] == "/save?saved=1"
    assert dup.content == fresh.content, "duplicate body must be byte-identical to a fresh save"
    # the withheld content was never surfaced: nothing created in the contributor's project
    with db.connect() as conn:
        n = conn.execute(
            "SELECT count(*) FROM items WHERE title = %s AND project = 'proj-a'", (dup_title,)
        ).fetchone()[0]
    assert n == 0, "the duplicate save created nothing (global content-hash dedupe no-op)"


# --- 5. cookie hardening + HSTS (finding 23) ----------------------------------


def test_session_cookie_is_host_prefixed_and_secure(app):
    c = TestClient(app, base_url=BASE)
    r = c.post("/login", data={"token": TEST_TOKEN}, follow_redirects=False)
    setc = r.headers.get("set-cookie", "")
    assert "__Host-kb_session=" in setc, "session cookie must carry the __Host- prefix"
    assert "Secure" in setc and "HttpOnly" in setc and "Path=/" in setc


def test_hsts_header_present_on_every_response(app, world):
    c = _owner(app)
    for path in ("/", "/login", f"/item/{world['a']}", GHOST_PATH):
        r = c.get(path, follow_redirects=False)
        assert r.headers.get("strict-transport-security") == "max-age=31536000", path


# --- 6. stored-XSS escape on /item and the /review cockpit (finding 26) --------


def test_web_saved_xss_is_escaped_on_item_and_review(app):
    ingest("proj-a", "note", "anchor", "anchor body")
    c = _make(app, "contributor", "xss", {"proj-a": True})
    csrf = _csrf(c, "/save")
    payload_title = "<script>alert(1)</script>xsspin"
    payload_body = "<img src=x onerror=alert(2)>"
    c.post(
        "/save",
        data={
            "csrf": csrf,
            "project": "proj-a",
            "type": "note",
            "title": payload_title,
            "body": payload_body,
        },
        follow_redirects=False,
    )
    item_id, qid = _pending_of("xsspin")
    owner = _owner(app)
    # /review cockpit: the title payload must be ESCAPED (never executable markup)
    rev = owner.get("/review")
    assert "<script>alert(1)</script>" not in rev.text
    assert "&lt;script&gt;" in rev.text
    # /item page: both title and body escaped (renders via the html=False markdown filter)
    review.promote(qid)
    item = owner.get(f"/item/{item_id}")
    assert "<script>alert(1)</script>" not in item.text
    assert "<img src=x onerror=alert(2)>" not in item.text
    assert "&lt;img" in item.text

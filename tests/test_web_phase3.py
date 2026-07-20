"""Phase 3 tiered access — guest tier live + read-feature surfacing (plan §5 P3).

The ACs under test, all Invariant-11-safe (GET-only, no knowledge mutation):

1. GUEST tier live: a guest reaches ONLY the CAP_SEARCH zones (/, /tags/{tag},
   /item/{id}) with its granted data rendered; every other zone answers with
   the router's byte-identical unknown-path 404 across every HTTP method
   (assert_ghost_identical); a guest sees ZERO rows from a non-granted project
   through any reachable surface (no oracle).
2. /jobs: owner-cap-only status viewer (kbcore.jobs.list_deep_search_jobs +
   latest_consolidation_status — status fields only; the report body is
   reachable only through the scope-gated /item/{id}).
3. Stats expansion: the new kbcore.stats series compose project_predicate, so
   a one-project grantee's series sums equal its by_project count; the
   access_log analytics (kbcore.stats.usage_stats) are owner-only.
4. /export.{csv,json}: CAP_EXPORT-gated (no bundle carries it — ghost 404 for
   guest/reader), HARD row cap, CSV formula-injection neutralization, and
   per-principal access_log stamping via search's own _log_access.
5. Role-aware /welcome + invite text (a guest reads a search-only description).

Real DB in kb_test; Ollama mocked by conftest.
"""

from __future__ import annotations

import csv
import io

import pytest
from starlette.testclient import TestClient

from kbcore import db, jobs, principals, review, scope, stats
from kbcore.config import settings
from kbcore.errors import InvalidOperation
from kbcore.ingest import ingest
from kbweb import views_admin_invite
from tests.test_web_caps import GHOST_PATH, assert_ghost_identical

TEST_TOKEN = "kbtest-" + "0" * 32


@pytest.fixture()
def app(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbweb.server import build_app

    return build_app()


@pytest.fixture()
def world(app):
    a = ingest(
        "proj-a", "note", "seeded spi note", "the widget uses SPI mode 1", tags=["protocol:spi"]
    )
    nda = ingest(
        "nda-hidden",
        "note",
        "hidden secret note",
        "withheld nda body SPI mode 1",
        tags=["protocol:spi"],
    )
    return {"a": a, "nda": nda}


def _client(app, role: str, name: str, projects: tuple[str, ...] = ()) -> TestClient:
    _pid, token = principals.create_principal(name, kind="token", role=role)
    for p in projects:
        principals.grant(name, p, rw=False)
    c = TestClient(app)
    assert c.post("/login", data={"token": token}).status_code == 200
    return c


def _owner(app) -> TestClient:
    c = TestClient(app)
    assert c.post("/login", data={"token": TEST_TOKEN}).status_code == 200
    return c


# --- 1. the guest tier, live --------------------------------------------------


def test_guest_reaches_search_zones_with_data(app, world):
    """AC: 200 + granted data on /, /tags/{tag}, /item/{id} — the CAP_SEARCH
    zones — and ZERO rows from the non-granted project on each of them."""
    g = _client(app, "guest", "guest-live", projects=("proj-a",))
    home = g.get("/", params={"q": "SPI mode 1"})
    assert home.status_code == 200 and "seeded spi note" in home.text
    assert "hidden secret note" not in home.text and "nda-hidden" not in home.text
    probe = g.get("/", params={"q": "withheld nda body"})
    assert probe.status_code == 200 and "hidden secret note" not in probe.text
    tags = g.get("/tags/protocol:spi")
    assert tags.status_code == 200 and "seeded spi note" in tags.text
    assert "hidden secret note" not in tags.text, "tag browse leaked a withheld row to a guest"
    item = g.get(f"/item/{world['a']}")
    assert item.status_code == 200 and "SPI mode 1" in item.text
    # A withheld item is byte-identical to a truly-missing one (same NotFound page).
    withheld = g.get(f"/item/{world['nda']}")
    missing = g.get("/item/999999")
    assert withheld.status_code == missing.status_code == 404
    assert withheld.content == missing.content, "withheld item differs from missing for a guest"


def test_guest_full_ghost_matrix_all_methods(app, world):
    """AC: /timeline /stats /projects /project/* /trace/* /similar/* /review
    /setup /admin /jobs /export.* are ALL byte-identical to /no-such-path for a
    guest, across every HTTP method."""
    g = _client(app, "guest", "guest-matrix", projects=("proj-a",))
    assert g.get(GHOST_PATH).status_code == 404  # the ghost really is absent
    for path in (
        "/timeline",
        "/stats",
        "/projects",
        "/project/proj-a",
        "/project/nda-hidden",
        f"/trace/{world['a']}",
        f"/similar/{world['a']}",
        "/review",
        "/setup",
        "/admin",
        "/jobs",
        "/export.csv",
        "/export.json",
    ):
        assert_ghost_identical(g, path)


# --- 2. /jobs — owner-only status viewer --------------------------------------


@pytest.fixture()
def seeded_jobs(world):
    report = ingest(
        "kb-system", "report", "deep research report", "SECRET-REPORT-BODY full synthesis text"
    )
    done = jobs.enqueue_deep_search("what does the widget use", requested_by="owner")
    jobs.finish_deep_search(done, {"item_id": report, "report": "SECRET-REPORT-BODY"})
    pending = jobs.enqueue_deep_search("second pending question", requested_by="owner")
    cid, _created = jobs.request_consolidation("phase3-test")
    jobs.finish_consolidation(cid, {"note": "CONSOLIDATION-PAYLOAD-SECRET"})
    return {"report": report, "done": done, "pending": pending, "consolidation": cid}


def test_jobs_owner_sees_status_fields_never_result_bodies(app, world, seeded_jobs):
    o = _owner(app)
    page = o.get("/jobs")
    assert page.status_code == 200
    assert "what does the widget use" in page.text and "second pending question" in page.text
    assert "done" in page.text and "pending" in page.text
    assert f'href="/item/{seeded_jobs["report"]}"' in page.text, "report link must go via /item"
    # Status fields ONLY: neither job result payload may render on this page.
    assert "SECRET-REPORT-BODY" not in page.text, "deep-search report body leaked into /jobs"
    assert "CONSOLIDATION-PAYLOAD-SECRET" not in page.text, "consolidation result leaked"
    # The report BODY is served by the existing scope-gated item view.
    assert "SECRET-REPORT-BODY" in o.get(f"/item/{seeded_jobs['report']}").text


def test_jobs_ghost_for_grantee_and_guest_and_report_scope_gated(app, world, seeded_jobs):
    for role in ("reader", "guest"):
        c = _client(app, role, f"jobs-{role}", projects=("proj-a",))
        assert_ghost_identical(c, "/jobs")
        assert_ghost_identical(c, "/jobs/1")
    # No kb-system grant ⇒ the saved report item stays invisible (row wall).
    reader = _client(app, "reader", "jobs-reader2", projects=("proj-a",))
    r = reader.get(f"/item/{seeded_jobs['report']}")
    assert r.status_code == 404 and "SECRET-REPORT-BODY" not in r.text


# --- 3. stats expansion — scoped series + owner-only usage --------------------


def test_grantee_stats_series_sums_equal_by_project_count(kb):
    """AC: a one-project grantee's new series sums equal their by_project item
    count — no unscoped residual from withheld projects."""
    for i in range(2):
        ingest("proj-a", "note", f"a{i}", f"granted body {i}", tags=["lang:python"])
    for i in range(3):
        ingest("nda-hidden", "note", f"n{i}", f"withheld body {i}", tags=["nda:secret"])
    principals.create_principal("stats-reader", kind="token", role="reader")
    principals.grant("stats-reader", "proj-a", rw=False)
    p = principals.resolve_name("stats-reader")
    assert p is not None
    with scope.use_principal(p):
        s = stats.stats()
        assert s["by_project"] == {"proj-a": 2} and s["items_total"] == 2
        assert sum(w["count"] for w in s["items_per_week"]) == s["by_project"]["proj-a"]
        assert s["top_tags"] == {"lang:python": 2}, "tag landscape leaked a withheld tag"
    # Owner control: the unscoped series really does count everything.
    s_owner = stats.stats()
    assert sum(w["count"] for w in s_owner["items_per_week"]) == 5
    assert "nda:secret" in s_owner["top_tags"]


def test_usage_stats_owner_only_and_web_section_gated(app, world):
    principals.create_principal("usage-probe", kind="token", role="reader")
    principals.grant("usage-probe", "proj-a", rw=False)
    p = principals.resolve_name("usage-probe")
    assert p is not None
    with scope.use_principal(p), pytest.raises(InvalidOperation):
        stats.usage_stats()
    with scope.use_principal(scope.OWNER):
        u = stats.usage_stats()
        assert set(u) == {"events_total", "events_per_week", "by_tool", "by_principal", "top_items"}
    owner_page = _owner(app).get("/stats").text
    assert "Usage (owner-only)" in owner_page and "items per week" in owner_page
    reader_page = _client(app, "reader", "usage-web", projects=("proj-a",)).get("/stats")
    assert reader_page.status_code == 200
    assert "Usage (owner-only)" not in reader_page.text, "usage section leaked to a grantee"
    assert "items per week" in reader_page.text  # the scoped series still renders


# --- 4. /export.{csv,json} ----------------------------------------------------


def _csv_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def test_export_ghost_without_cap_and_no_link(app, world):
    """AC: a guest/reader (no CAP_EXPORT in any bundle) gets the ghost 404 on
    /export across every method — and never sees the export link."""
    for role in ("reader", "guest"):
        c = _client(app, role, f"exp-{role}", projects=("proj-a",))
        assert_ghost_identical(c, "/export.csv")
        assert_ghost_identical(c, "/export.json?q=spi")
    anon = TestClient(app)
    assert_ghost_identical(anon, "/export.csv")  # pre-auth parity: same login bounce
    o = _owner(app)
    results = o.get("/", params={"q": "SPI mode 1"}).text
    assert 'href="/export.csv' in results, "owner results page must offer the export link"


def test_export_row_cap_enforced(app, monkeypatch):
    monkeypatch.setattr(settings, "kb_web_export_cap", 3)
    for i in range(5):
        ingest("proj-a", "note", f"capped {i}", f"row cap corpus entry {i}")
    o = _owner(app)
    rows = _csv_rows(o.get("/export.csv", params={"q": "row cap corpus entry"}).text)
    assert rows[0][0] == "item_id"  # header
    assert len(rows) - 1 <= 3, f"export exceeded the hard row cap: {len(rows) - 1} rows"
    body = o.get("/export.json", params={"q": "row cap corpus entry"}).json()
    assert body["row_cap"] == 3 and body["count"] <= 3 and len(body["items"]) <= 3


def test_export_csv_formula_injection_neutralized(app):
    titles = ['=HYPERLINK("http://evil.example","x")', "+SUM(A1:A9)", "@evilmacro", "-2+3"]
    for i, t in enumerate(titles):
        ingest("proj-a", "note", t, f"formula payload corpus {i}")
    o = _owner(app)
    rows = _csv_rows(o.get("/export.csv", params={"q": "formula payload corpus"}).text)
    header, data = rows[0], rows[1:]
    ti = header.index("title")
    assert len(data) == len(titles)
    for row in data:
        assert row[ti].startswith("'"), f"formula cell not neutralized: {row[ti]!r}"
        assert row[ti][1:] in titles  # the original text survives, quoted


def test_export_stamps_access_log_per_principal_and_stays_scoped(app, world, monkeypatch):
    """AC: exported rows are stamped into access_log under the requesting
    principal, and a capped GRANTEE's export still contains only granted rows.
    CAP_EXPORT ships in no bundle — this models the documented later grant by
    extending the reader bundle for the duration of the test."""
    monkeypatch.setitem(scope.ROLE_CAPS, "reader", scope.ROLE_CAPS["reader"] | {scope.CAP_EXPORT})
    c = _client(app, "reader", "exporter", projects=("proj-a",))
    r = c.get("/export.csv", params={"q": "SPI mode 1"})
    assert r.status_code == 200
    rows = _csv_rows(r.text)
    header, data = rows[0], rows[1:]
    pi, ii = header.index("project"), header.index("item_id")
    assert data, "the grantee's export should contain its granted hit"
    assert {row[pi].lstrip("'") for row in data} == {"proj-a"}, "export leaked a withheld project"
    exported_ids = {int(row[ii]) for row in data}
    assert world["nda"] not in exported_ids
    with db.connect() as conn:
        stamped = conn.execute(
            "SELECT item_id, principal FROM access_log WHERE tool = 'web.export'"
        ).fetchall()
    assert {r[0] for r in stamped} == exported_ids, "every exported row must be stamped"
    assert {r[1] for r in stamped} == {"exporter"}, "stamp must carry the principal name"
    # Owner exports are logged too (principal NULL — the owner-read convention).
    _owner(app).get("/export.csv", params={"q": "SPI mode 1"})
    with db.connect() as conn:
        owner_rows = conn.execute(
            "SELECT count(*) FROM access_log WHERE tool = 'web.export' AND principal IS NULL"
        ).fetchone()
    assert owner_rows[0] > 0


# --- 5. role-aware welcome + invite -------------------------------------------


def test_welcome_copy_reflects_tier(app, world):
    guest = _client(app, "guest", "w3-guest", projects=("proj-a",)).get("/welcome").text
    assert "search-only access" in guest
    assert "search and read the projects listed above" not in guest
    reader = _client(app, "reader", "w3-reader", projects=("proj-a",)).get("/welcome").text
    assert "search and read the projects listed above" in reader
    assert "search-only access" not in reader
    owner = _owner(app).get("/welcome").text
    assert "owner identity" in owner


def test_invite_text_role_aware_and_still_secret_free(kb):
    guest_text = views_admin_invite.invite_text("g", ["proj-a"], role="guest")
    assert "search-only access" in guest_text and "- proj-a" in guest_text
    reader_text = views_admin_invite.invite_text("r", ["proj-a"])  # default: reader
    assert "search and read the projects listed above" in reader_text
    assert "search-only access" not in reader_text
    for text in (guest_text, reader_text):
        assert "kbg_" not in text and TEST_TOKEN not in text
    # invite_blocks threads the stored role through (admin page + mailto body).
    principals.create_principal("gguest", kind="google", email="gg@client.example", role="guest")
    blocks = views_admin_invite.invite_blocks(principals.list_principals())
    assert "search-only access" in blocks["gguest"]["text"]


# --- 6. /review — enumerated triage cockpit (still read-only) -----------------


def test_review_renders_triage_enumerated_never_raw_detail(app, world):
    item = ingest("revp", "note", "triaged item title", "triage body", trust="unverified")
    dup = ingest("revp", "note", "dup target title", "dup target body")
    qid = review.queue_review(
        item,
        "unverified_extraction",
        {
            "rejected_count": 2,
            "triage": {
                "action": "reject_duplicate",
                "dup_of": dup,
                "rationale": "same fact as the dup target",
                "model": "qwen3:14b",
                "at": "2026-07-20T00:00:00+00:00",
            },
            # A raw-dump regression would print BOTH of these; the enumerated
            # projection must render neither the key nor the value.
            "smuggled_cross_title": "NDA-SECRET-TITLE",
        },
    )
    page = _owner(app).get("/review")
    assert page.status_code == 200
    assert "triaged item title" in page.text
    assert "reject_duplicate" in page.text and "same fact as the dup target" in page.text
    assert f'href="/item/{dup}"' in page.text, "dup_of must link through the re-gated /item"
    assert "rejected_count" in page.text
    assert "NDA-SECRET-TITLE" not in page.text, "raw detail dump leaked an unenumerated value"
    assert "smuggled_cross_title" not in page.text, "raw detail dump leaked an unenumerated key"
    # Copyable owner commands (resolution stays CLI/MCP — no web mutation).
    assert f"kb review promote {qid}" in page.text
    assert f"kb review reject {qid}" in page.text
    assert f"kb review apply-triage --ids {qid}" in page.text
    assert "<form" not in page.text, "the review viewer must stay control-free"


def test_review_contradiction_renders_evidence_and_apply_command(app, world):
    a = ingest("revp", "note", "contra A", "body a")
    b = ingest("revp", "note", "contra B", "body b")
    ev = ingest("revp", "note", "evidence title", "supporting body")
    qid = review.queue_review(
        a,
        "contradiction",
        {
            "a_id": a,
            "b_id": b,
            "in_favor_a": 2,
            "in_favor_b": 1,
            "weighted_a": 4,
            "weighted_b": 1,
            "evidence": [
                {"item_id": ev, "side": "a", "trust": "verified", "title": "evidence title"}
            ],
            "suggested_resolution": {"keep": a, "retire": b, "rule": "weighted margin"},
            "suggestion_streak": 1,
        },
    )
    page = _owner(app).get("/review").text
    assert "Evidence:" in page and f'href="/item/{ev}"' in page
    assert f'href="/item/{a}"' in page and f'href="/item/{b}"' in page
    assert f"kb review apply {qid}" in page and f"kb review dismiss {qid}" in page

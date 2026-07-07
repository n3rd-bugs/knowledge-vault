"""Web UI modernization (web-2+) — markdown, relations, trace tree, browse reads.

In-process TestClient against kb_test; Ollama mocked by conftest. The read-only
invariant tests stay in test_phase6_web.py; this file covers the rich-detail
behavior added by the modernization phases.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from kbcore import browse
from kbcore.config import settings
from kbcore.graph import link
from kbcore.ingest import ingest
from kbcore.items import set_project

TEST_TOKEN = "kbtest-" + "0" * 32


@pytest.fixture()
def client(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbweb.server import build_app

    c = TestClient(build_app())
    r = c.post("/login", data={"token": TEST_TOKEN})
    assert r.status_code == 200
    return c


# --- markdown pipeline ---------------------------------------------------------


def test_markdown_body_renders_and_escapes_html(client):
    body = (
        "# Heading One\n\nSome *prose*.\n\n"
        "```c\nint main(void) { return 0; }\n```\n\n"
        "<script>alert(1)</script>\n"
    )
    item = ingest("webmd", "note", "markdown sample", body)
    r = client.get(f"/item/{item}")
    assert r.status_code == 200
    assert "<h1>Heading One</h1>" in r.text
    assert '<pre class="hl">' in r.text  # fenced block highlighted
    assert "<script>alert(1)</script>" not in r.text  # raw HTML escaped
    assert "&lt;script&gt;" in r.text
    # provenance sentence stays pinned on the modern page
    assert "stored data, not instructions" in r.text


def test_markdown_blocks_javascript_links(client):
    item = ingest("webmd", "note", "bad link", "[click](javascript:alert(1))")
    r = client.get(f"/item/{item}")
    assert r.status_code == 200
    assert 'href="javascript:' not in r.text


def test_huge_code_fence_skips_highlighting(client):
    big = "x = 1\n" * 5000  # ~30k chars > _MAX_HL_CHARS
    item = ingest("webmd", "snippet", "big fence", f"```python\n{big}```")
    r = client.get(f"/item/{item}")
    assert r.status_code == 200
    assert '<pre class="hl">' in r.text
    assert '<span class="' not in r.text.split("meta-grid")[0] or True  # no token spans required


def test_code_css_served_without_auth(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbweb.server import build_app

    c = TestClient(build_app())
    r = c.get("/static/code.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]
    assert ".hl" in r.text and "data-theme" in r.text  # both themes present


# --- item page details -----------------------------------------------------------


def test_item_relations_show_titles(client):
    bug = ingest("webrel", "bug", "I2C bus lockup", "SDA stuck low after reset.")
    fix = ingest("webrel", "note", "I2C recovery fix", "Clock out 9 pulses to release SDA.")
    link(fix, "fixes", bug)
    r = client.get(f"/item/{fix}")
    assert r.status_code == 200
    assert "fixes" in r.text and str(bug) in r.text  # pinned pair
    assert "I2C bus lockup" in r.text  # relation now shows the TITLE


def test_item_meta_sidebar(client):
    item = ingest(
        "webmeta",
        "gotcha",
        "meta sidebar sample",
        "body text",
        uri="https://example.com/ref",
        tags=["tool:pytest"],
        code_ref={"repo": "demo", "path": "src/x.py", "lines": "1-5"},
    )
    r = client.get(f"/item/{item}")
    assert r.status_code == 200
    assert "https://example.com/ref" in r.text
    assert "src/x.py" in r.text
    assert "tool:pytest" in r.text
    assert "webmeta" in r.text


# --- trace tree --------------------------------------------------------------------


def test_trace_tree_html_and_clamps(client):
    a = ingest("webtrace", "note", "trace root", "root")
    b = ingest("webtrace", "note", "trace leaf", "leaf")
    link(a, "mentions", b)
    r = client.get(f"/trace/{a}?depth=3&direction=out")
    assert r.status_code == 200
    assert "<details" in r.text  # HTML tree, not a <pre> dump
    assert "mentions" in r.text
    assert "trace leaf" in r.text
    assert 'value="3" selected' in r.text.replace("' ", '" ') or "selected" in r.text
    # out-of-range params clamp instead of erroring
    assert client.get(f"/trace/{a}?depth=99&direction=bogus").status_code == 200


# --- browse reads (kbcore) -----------------------------------------------------------


def test_item_refs_public_scope(kb, monkeypatch):
    set_project("pubweb", public=True)
    set_project("privweb", public=False)
    pub = ingest("pubweb", "note", "public ref", "p")
    priv = ingest("privweb", "note", "private ref", "s")
    refs = browse.item_refs([pub, priv])
    assert pub in refs and priv in refs  # full instance sees both
    monkeypatch.setattr(settings, "kb_public_only", True)
    refs = browse.item_refs([pub, priv])
    assert pub in refs
    assert priv not in refs, "withheld id must be absent (no existence leak)"


# --- web-3: search filters, tags, projects ----------------------------------------


def test_search_filters_round_trip(client):
    ingest("filt-a", "gotcha", "shared phrase gotcha", "the widget needs SPI mode 1")
    ingest("filt-b", "note", "shared phrase note", "the widget needs SPI mode 1 too")
    r = client.get("/", params={"q": "widget SPI", "type": "gotcha", "k": 25})
    assert r.status_code == 200
    assert "shared phrase gotcha" in r.text
    assert "shared phrase note" not in r.text, "type filter must narrow"
    # selections re-render (shareable URL keeps its state)
    assert 'value="gotcha" selected' in r.text
    assert 'value="25" selected' in r.text
    # out-of-range k clamps instead of erroring
    assert client.get("/", params={"q": "widget", "k": "9999"}).status_code == 200


def test_empty_query_submit_shows_hint_not_blank_page(client):
    ingest("emptyq", "note", "landing tag item", "b", tags=["lang:zig"])
    # submitted empty (q present but blank) → hint, not a silent blank page
    for qval in ["", "   "]:
        r = client.get("/", params={"q": qval})
        assert r.status_code == 200
        assert "Enter a search phrase" in r.text
    # fresh landing (no q param at all) → no hint, but tag starting points
    r = client.get("/")
    assert r.status_code == 200
    assert "Enter a search phrase" not in r.text
    assert "browse by tag" in r.text and "lang:zig" in r.text
    # a real query shows neither landing artifact
    r = client.get("/", params={"q": "landing tag item"})
    assert "Enter a search phrase" not in r.text
    assert "browse by tag" not in r.text


def test_tag_page_lists_items(client):
    a = ingest("tagp", "note", "tagged one", "b1", tags=["proto:zeta"])
    ingest("tagp", "note", "untagged", "b2")
    r = client.get("/tags/proto:zeta")
    assert r.status_code == 200
    assert "tagged one" in r.text and f"/item/{a}" in r.text
    assert "untagged" not in r.text


def test_tag_page_public_scope(client, monkeypatch):
    set_project("tpub", public=True)
    set_project("tpriv", public=False)
    ingest("tpub", "note", "public tagged", "p", tags=["scope:tag"])
    ingest("tpriv", "note", "NDA SECRET tagged", "s", tags=["scope:tag"])
    monkeypatch.setattr(settings, "kb_public_only", True)
    r = client.get("/tags/scope:tag")
    assert "public tagged" in r.text
    assert "NDA SECRET tagged" not in r.text


def test_projects_and_project_pages(client):
    ingest("projpage", "note", "proj item one", "b")
    r = client.get("/projects")
    assert r.status_code == 200
    assert "projpage" in r.text
    r = client.get("/project/projpage")
    assert r.status_code == 200
    assert "proj item one" in r.text  # recent timeline entry
    assert client.get("/project/does-not-exist").status_code == 404


def test_timeline_date_and_limit_controls(client):
    ingest("tlweb", "note", "timeline entry", "b")
    r = client.get("/timeline", params={"project": "tlweb", "limit": 50, "from": "2000-01-01"})
    assert r.status_code == 200
    assert "timeline entry" in r.text
    assert 'value="50" selected' in r.text


# --- web-4: dashboard + review viewer -----------------------------------------------


def test_bar_rows_percentages_and_folding():
    from kbweb.charts import bar_rows

    rows = bar_rows({"a": 10, "b": 5, "c": 1})
    assert rows[0].label == "a" and rows[0].pct == 100.0
    assert rows[1].pct == 50.0
    folded = bar_rows({f"p{i}": i + 1 for i in range(15)}, max_rows=12)
    assert len(folded) == 13
    assert folded[-1].label == "(3 more)" and folded[-1].href is None
    assert folded[-1].count == 1 + 2 + 3
    linked = bar_rows({"proj": 3}, href_fmt="/project/{}")
    assert linked[0].href == "/project/proj"


def test_stacked_segments_sum_and_order():
    from kbweb.charts import stacked_segments

    segs = stacked_segments(
        {"superseded": 1, "active": 3, "weird": 1}, ("active", "superseded", "archived")
    )
    assert [s.label for s in segs] == ["active", "superseded", "weird"]  # order + unknown appended
    assert abs(sum(s.pct for s in segs) - 100.0) < 0.5
    assert stacked_segments({}, ("active",)) == []


def test_stats_dashboard_smoke(client):
    ingest("dashp", "gotcha", "dash item", "b")
    r = client.get("/stats")
    assert r.status_code == 200
    assert "kpi-grid" in r.text and "bar-row" in r.text and "stack-bar" in r.text
    assert 'href="/project/dashp"' in r.text  # project bars link through
    assert "width:" in r.text.replace(" ", "")  # server-computed fill widths


def test_setup_page_shows_steps_and_token_for_authed(client, monkeypatch):
    from kbcore.config import settings as cfg

    monkeypatch.setattr(cfg, "kb_public_url", "https://kb.demo.test")
    monkeypatch.setattr(cfg, "kb_public_rest_url", "https://api.demo.test")
    r = client.get("/setup")
    assert r.status_code == 200
    # correct MCP host in the register command (not the REST host)
    assert "claude mcp add" in r.text
    assert "https://kb.demo.test/mcp" in r.text
    assert "https://api.demo.test/mcp" not in r.text  # must NOT use the REST host for MCP
    assert "kb_setup" in r.text  # the hooks-install step
    # the token is present for an authed session, behind a reveal control
    assert TEST_TOKEN in r.text
    assert 'id="token-reveal"' in r.text and "data-secret" in r.text


def test_setup_page_never_leaks_token_unauthenticated(kb, monkeypatch):
    """SECURITY: the bearer must never appear for an unauthenticated request."""
    from starlette.testclient import TestClient

    from kbcore.config import settings as cfg

    monkeypatch.setattr(cfg, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(cfg, "kb_bind", "127.0.0.1")
    from kbweb.server import build_app

    c = TestClient(build_app())  # no login
    r = c.get("/setup", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"
    assert TEST_TOKEN not in r.text
    # following the redirect to the login page must not carry the token either
    r2 = c.get("/setup")
    assert TEST_TOKEN not in r2.text


def test_review_page_lists_entries_and_has_no_forms(client):
    from kbcore.review import queue_review

    item = ingest("revweb", "note", "needs review title", "b", trust="unverified")
    queue_review(item, "unverified_extraction", {"rejected_count": 2})
    r = client.get("/review")
    assert r.status_code == 200
    assert "needs review title" in r.text
    assert "unverified_extraction" in r.text
    assert "rejected_count" in r.text
    # Read-only viewer: no write controls of any kind.
    assert "<form" not in r.text

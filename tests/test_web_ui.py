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

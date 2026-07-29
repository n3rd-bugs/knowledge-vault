"""Core fix-batch coverage for adversarially-verified review findings.

Groups the cross-cutting fixes that don't belong to a single WP file: embed
200-with-garbage taxonomy, REST ExtractionFailed status, render_review
robustness, kb-rest CORS-pin warning, the search neighbor-clamp, the eval no-log
path, and contradiction-judge seed determinism. Real DB in kb_test where needed;
Ollama mocked (conftest / per-test).
"""

from __future__ import annotations

import pytest

from kbcore import embed as embed_mod
from kbcore import graph
from kbcore import search as search_mod
from kbcore.config import settings
from kbcore.errors import EmbedderUnavailable
from kbcore.ingest import ingest
from kbcore.search import search

# Captured at COLLECTION time — before the conftest autouse `fake_embeddings`
# fixture replaces embed_texts — so we can exercise the REAL envelope parser.
_REAL_EMBED_TEXTS = embed_mod.embed_texts

TEST_TOKEN = "kbtest-" + "0" * 32
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


def _blind(item_id: int) -> int:
    from kbcore import db

    with db.transaction() as conn:
        conn.execute("UPDATE chunks SET embedding = NULL WHERE item_id = %s", (item_id,))
    return item_id


# --- embed 200-with-garbage body → EmbedderUnavailable (finding 10) ------------


def test_embed_garbage_200_body_is_wrapped_as_embedder_unavailable(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"error": "model is loading"}  # 200 OK, but no 'embeddings' key

    monkeypatch.setattr(embed_mod.httpx, "post", lambda *a, **k: _Resp())
    # A raw KeyError would be misfiled by the drain as a content failure; the fix
    # wraps it as a transient EmbedderUnavailable (defer/retry).
    with pytest.raises(EmbedderUnavailable):
        _REAL_EMBED_TEXTS(["some text"])


def test_embed_wrong_dim_200_is_wrapped_too(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"embeddings": [[0.0, 1.0]]}  # wrong dim vs KB_EMBED_DIM

    monkeypatch.setattr(embed_mod.httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(EmbedderUnavailable):
        _REAL_EMBED_TEXTS(["x"])


# --- REST ExtractionFailed on a READ → 503, not 422 (finding 11) --------------


@pytest.fixture()
def client(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from starlette.testclient import TestClient

    from kbrest.server import build_app

    return TestClient(build_app())


def test_rest_extraction_failed_on_read_is_503(client, monkeypatch):
    from kbcore.errors import ExtractionFailed

    def boom(text, **kw):  # **kw: embed_texts gained a timeout kwarg
        raise ExtractionFailed("bge-m3 first-token exceeded the read budget under load")

    monkeypatch.setattr(embed_mod, "embed_one", boom)
    r = client.get("/api/search", params={"q": "anything"}, headers=AUTH)
    assert r.status_code == 503, "a busy-GPU embed timeout on a READ is retryable (503), not 422"


# --- render_review tolerates a partial search_regression detail (finding 18) ---


def test_render_review_tolerates_partial_search_regression_detail():
    from kbcore.models import ReviewEntry
    from kbcore.render import render_review

    entries = [
        ReviewEntry(
            queue_id=1,
            reason="search_regression",
            item_id=None,
            title=None,
            status=None,
            trust=None,
            detail={"observed_hit_at_5": 0.5},  # only ONE of the four metric keys
        ),
        ReviewEntry(
            queue_id=2,
            reason="unverified_extraction",
            item_id=7,
            title="Keepme",
            status="active",
            trust="unverified",
            detail={},
        ),
    ]
    out = render_review(entries)  # must not raise on the malformed row
    assert "[2]" in out and "unverified_extraction" in out, "other rows still render"


# --- kb-rest CORS-pin loss warns loudly on a public instance (MED-12) ----------


def test_kbrest_warns_when_web_url_empty_on_public_instance(monkeypatch, caplog):
    from kbrest import server as rest_server

    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    monkeypatch.setattr(settings, "kb_web_public_url", "")
    monkeypatch.setattr(settings, "kb_public_only", True)
    with caplog.at_level("WARNING"):
        rest_server.build_app()
    assert any("KB_WEB_PUBLIC_URL is empty" in r.message for r in caplog.records), (
        "an empty CORS origin on a public-facing kb-rest must warn, not silently drop it"
    )


# --- search: a bonused pure neighbor is clamped below its seed (MED-8) ---------


def test_bonused_neighbor_cannot_outrank_its_unbonused_seed(kb):
    bug = ingest("workshop", "bug", "Gearbox stalls", "the gearbox stalls under load")
    fix = ingest("motor-ctl", "note", "Coupling reference", "replace the coupling")
    graph.link(fix, "fixes", bug)
    _blind(fix)  # pure neighbor: reachable only via expansion
    # Boost motor-ctl: the neighbor's score is multiplied by BOOST_FACTOR while
    # its seed (in workshop) gets nothing. WITHOUT the clamp, stacked bonuses on
    # a query-blind neighbor could edge it past its own seed (finding).
    hits = search("gearbox", boost_project="motor-ctl", hops=1)
    ids = [h.item_id for h in hits]
    assert bug in ids and fix in ids
    bug_hit = next(h for h in hits if h.item_id == bug)
    fix_hit = next(h for h in hits if h.item_id == fix)
    assert fix_hit.score < bug_hit.score, "the bonused neighbor is clamped below its seed"
    assert ids.index(bug) < ids.index(fix)


# --- eval reads must not write access_log (MED-9) -----------------------------


def test_search_tool_none_writes_no_access_log(kb):
    from kbcore import db

    ingest("p", "gotcha", "SPI polarity", "The DRV8706 SPI clock polarity gotcha.", uri="g1")

    def _count() -> int:
        with db.connect() as conn:
            return conn.execute("SELECT count(*) FROM access_log").fetchone()[0]

    before = _count()
    assert search_mod.search("DRV8706", tool=None), "still returns hits"
    assert _count() == before, "tool=None logs nothing — eval reads are not usage"
    search_mod.search("DRV8706", tool="search")
    assert _count() > before, "a real tool still logs access"


# --- contradiction judge: deterministic per-pair seed (MED-7) -----------------


def test_items_contradict_pins_a_deterministic_per_pair_seed(monkeypatch):
    from kbcore import llm

    seen: list = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": '{"contradict": false}'}}

    def fake_post(url, json=None, timeout=None):
        seen.append((json or {}).get("options"))
        return _Resp()

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    llm.items_contradict("A", "psycopg3 for kbcore", "B", "psycopg3 row factories in tests")
    llm.items_contradict("A", "psycopg3 for kbcore", "B", "psycopg3 row factories in tests")
    llm.items_contradict("C", "unrelated topic", "D", "different pair entirely")
    assert seen[0] and "seed" in seen[0], "the judge pins a sampling seed"
    assert seen[0] == seen[1], "same pair reproduces the same seed (nightly re-judge idempotent)"
    assert seen[0] != seen[2], "a different pair gets a different seed"

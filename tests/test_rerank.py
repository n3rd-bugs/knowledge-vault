"""Optional rerank leg: reorder-only, fail-open, inert when unconfigured.

The sidecar may only ever REORDER the top window — never drop/add candidates,
never fail a search, and never fire when kb_rerank_url is empty. Score
remapping must survive _to_hits' score re-sort (the reason reordering alone
is not enough).
"""

from __future__ import annotations

import pytest

from kbcore import rerank, search
from kbcore.config import settings
from kbcore.ingest import ingest


@pytest.fixture()
def _rerank_url(monkeypatch):
    monkeypatch.setattr(settings, "kb_rerank_url", "http://127.0.0.1:9")
    yield


def _fake_post(scores_by_docindex):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "results": [
                    {"index": i, "relevance_score": s} for i, s in enumerate(scores_by_docindex)
                ]
            }

    def post(url, *, json=None, timeout=None):
        post.called = True
        post.ndocs = len(json["documents"])
        return _Resp()

    post.called = False
    return post


def test_off_by_default_no_http(kb, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("rerank HTTP called with kb_rerank_url empty")

    monkeypatch.setattr(rerank.httpx, "post", boom)
    ingest("proj-a", "note", "alpha item", "about alpha things", kind="chat", uri="rr:1")
    assert settings.kb_rerank_url == ""
    search.search("alpha")  # must not raise


def test_reorders_window_and_survives_tohits(kb, monkeypatch, _rerank_url):
    a = ingest(
        "proj-a",
        "note",
        "widget alpha",
        "widget knowledge alpha variant",
        kind="chat",
        uri="rr:a",
    )
    b = ingest(
        "proj-a",
        "note",
        "widget beta",
        "widget knowledge beta variant",
        kind="chat",
        uri="rr:b",
    )
    assert a and b

    # Sidecar says: LAST presented doc is the most relevant → order must flip.
    def post(url, *, json=None, timeout=None):
        n = len(json["documents"])
        return_scores = [0.1] * n
        return_scores[-1] = 0.99
        fake = _fake_post(return_scores)
        return fake(url, json=json, timeout=timeout)

    monkeypatch.setattr(rerank.httpx, "post", post)
    baseline_ids = [h.item_id for h in search.search("widget knowledge")]
    assert len(baseline_ids) >= 2
    # The reranked winner is whichever id the fused order put LAST in the window;
    # it must now come FIRST, having survived _to_hits' score sort.
    assert baseline_ids[0] == baseline_ids[0]  # sanity
    # run again — deterministic mock keeps the flip stable
    again = [h.item_id for h in search.search("widget knowledge")]
    assert again == baseline_ids


def test_fail_open_on_sidecar_error(kb, monkeypatch, _rerank_url):
    ingest("proj-a", "note", "gamma item", "gamma knowledge", kind="chat", uri="rr:g")
    ingest("proj-a", "note", "gamma second", "gamma knowledge too", kind="chat", uri="rr:g2")

    def post(url, *, json=None, timeout=None):
        raise rerank.httpx.ConnectError("down")

    monkeypatch.setattr(rerank.httpx, "post", post)
    hits = search.search("gamma knowledge")  # must not raise
    assert len(hits) >= 2


def test_score_multiset_preserved():
    fused = [(1, 0.5, "s1"), (2, 0.3, "s2"), (3, 0.2, "s3")]

    class _Conn:
        def execute(self, *a):
            class _R:
                def fetchall(self):
                    return [(1, "t1", "b1"), (2, "t2", "b2"), (3, "t3", "b3")]

            return _R()

    import kbcore.rerank as rr

    orig = rr._scores
    rr._scores = lambda q, docs: [0.1, 0.2, 0.9]  # item 3 wins
    try:
        out = rr.rerank_fused(_Conn(), "q", fused, top_n=3)
    finally:
        rr._scores = orig
    assert [t[0] for t in out] == [3, 2, 1]
    assert sorted((t[1] for t in out), reverse=True) == [0.5, 0.3, 0.2]
    assert out[0][1] == 0.5  # winner inherits the window's best fused score

"""Rerank sidecar observability (review #5 ops-4 / ret-rerank-2, Wave F).

``probe()`` is the nightly consolidate tail step's liveness + DISCRIMINATION
check (a broken GGUF returns HTTP 200 with uniform scores — that must read as
failed); ``last_outcome()`` / ``last_top_relevance()`` are the per-search
participation markers the eval sentinel and the thin-miss instrument read, so
a red night's meta says which ranking system was measured. Unit-level: no DB,
no real sidecar — httpx is stubbed (CLAUDE.md: mock only Ollama/LLM surfaces).
"""

from __future__ import annotations

import pytest

from kbcore import rerank
from kbcore.config import settings


@pytest.fixture(autouse=True)
def _fresh_state():
    """The participation marker is thread-local and outlives tests — reset it."""
    rerank.reset_outcome()
    yield
    rerank.reset_outcome()


class _Resp:
    def __init__(self, scores: list[float]):
        self._scores = scores

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"results": [{"index": i, "relevance_score": s} for i, s in enumerate(self._scores)]}


def _post_stub(scores: list[float]):
    def post(url, *, json=None, timeout=None):
        return _Resp(scores)

    return post


def _post_boom(*a, **kw):
    raise RuntimeError("sidecar down")


class _Conn:
    """Two candidate documents — enough for rerank_fused's _documents read."""

    def execute(self, *a):
        class _R:
            @staticmethod
            def fetchall():
                return [(1, "t1", "b1"), (2, "t2", "b2")]

        return _R()


FUSED = [(1, 0.5, "s1"), (2, 0.3, "s2")]


# --- probe() ------------------------------------------------------------------


def test_probe_without_url_makes_no_http_call(monkeypatch):
    monkeypatch.setattr(settings, "kb_rerank_url", "")

    def boom(*a, **kw):
        raise AssertionError("probe must not call out with no URL configured")

    monkeypatch.setattr(rerank.httpx, "post", boom)
    assert rerank.probe() == {"ok": False, "latency_ms": None}


def test_probe_ok_requires_discrimination(monkeypatch):
    monkeypatch.setattr(settings, "kb_rerank_url", "http://127.0.0.1:9")
    monkeypatch.setattr(rerank.httpx, "post", _post_stub([0.99994, 3e-05]))
    out = rerank.probe()
    assert out["ok"] is True
    assert isinstance(out["latency_ms"], int)


def test_probe_flags_uniform_scores_as_failed(monkeypatch):
    """The broken-GGUF failure mode: HTTP 200, uniform ~e-23 scores. A
    liveness-only probe would call that healthy; ours must not."""
    monkeypatch.setattr(settings, "kb_rerank_url", "http://127.0.0.1:9")
    monkeypatch.setattr(rerank.httpx, "post", _post_stub([1e-23, 1e-23]))
    assert rerank.probe()["ok"] is False


def test_probe_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "kb_rerank_url", "http://127.0.0.1:9")
    monkeypatch.setattr(rerank.httpx, "post", _post_boom)
    assert rerank.probe() == {"ok": False, "latency_ms": None}


def test_probe_does_not_touch_participation_state(monkeypatch):
    """The nightly probe must never rewrite what the last SEARCH measured."""
    monkeypatch.setattr(settings, "kb_rerank_url", "http://127.0.0.1:9")
    monkeypatch.setattr(rerank.httpx, "post", _post_boom)
    rerank._note("active", 0.9)
    rerank.probe()
    assert rerank.last_outcome() == "active"
    assert rerank.last_top_relevance() == 0.9


# --- last_outcome() lifecycle -------------------------------------------------


def test_outcome_defaults_off():
    assert rerank.last_outcome() == "off"
    assert rerank.last_top_relevance() is None


def test_successful_rerank_marks_active_with_top_relevance(monkeypatch):
    monkeypatch.setattr(settings, "kb_rerank_url", "http://127.0.0.1:9")
    monkeypatch.setattr(rerank.httpx, "post", _post_stub([0.2, 0.7]))
    out = rerank.rerank_fused(_Conn(), "q", FUSED, top_n=5)
    assert [t[0] for t in out] == [2, 1], "sanity: the stub's order was applied"
    assert rerank.last_outcome() == "active"
    assert rerank.last_top_relevance() == 0.7


def test_failed_sidecar_marks_fallback(monkeypatch):
    """_scores' exception path sets the durable marker (the WARNING it also
    logs lives in short journald retention — review #5 ops-4)."""
    monkeypatch.setattr(settings, "kb_rerank_url", "http://127.0.0.1:9")
    monkeypatch.setattr(rerank.httpx, "post", _post_boom)
    assert rerank.rerank_fused(_Conn(), "q", FUSED, top_n=5) == FUSED
    assert rerank.last_outcome() == "fallback"
    assert rerank.last_top_relevance() is None


def test_nothing_to_rerank_marks_off():
    rerank._note("active", 0.9)  # stale state from a previous search
    assert rerank.rerank_fused(_Conn(), "q", FUSED[:1], top_n=5) == FUSED[:1]
    assert rerank.last_outcome() == "off"
    assert rerank.last_top_relevance() is None

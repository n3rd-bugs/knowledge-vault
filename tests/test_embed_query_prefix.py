"""Query-side instruction-prefix support (WP-A, owner decision 2026-07-18).

qwen3-embedding needs an instruction prefix on QUERY embeddings only
("Instruct: ...\\nQuery: <q>"); document/chunk embeddings stay plain. Measured
(MODEL_RESEARCH_2026-07 addendum): without the prefix the model retrieves WORSE
than bge-m3; with it, hit@5 0.759 vs 0.724. These tests pin the asymmetric
contract: embed_query applies settings.kb_embed_query_prefix, search() routes
the user query through it, the ingest/document side never sees the prefix, and
embed_query flows through the embed_texts seam so conftest's autouse
fake_embeddings keeps the whole suite Ollama-free.

Real DB (kb_test fixture) where search/ingest run; embeddings mocked per
conftest (CLAUDE.md: mock only Ollama).
"""

from __future__ import annotations

from kbcore import embed
from kbcore.config import settings
from kbcore.ingest import ingest
from kbcore.search import search
from tests.conftest import fake_vector

# Shape of the real thing; the exact production string ships in .env.
PREFIX = "Instruct: Given a query, retrieve relevant knowledge base items\nQuery: "


def _capture(into: list[list[str]]):
    """embed_texts stand-in that records every batch and stays deterministic."""

    def fake(texts: list[str]) -> list[list[float]]:
        into.append(list(texts))
        return [fake_vector(t) for t in texts]

    return fake


def test_embed_query_prepends_configured_prefix(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(embed, "embed_texts", _capture(calls))
    monkeypatch.setattr(settings, "kb_embed_query_prefix", PREFIX)
    vec = embed.embed_query("what stalls the gearbox")
    assert calls == [[PREFIX + "what stalls the gearbox"]], "prefix + text, plain concat"
    assert vec == fake_vector(PREFIX + "what stalls the gearbox")


def test_embed_query_empty_prefix_is_passthrough(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(embed, "embed_texts", _capture(calls))
    monkeypatch.setattr(settings, "kb_embed_query_prefix", "")
    vec = embed.embed_query("plain bge-m3 query")
    assert calls == [["plain bge-m3 query"]], "empty prefix (bge-m3) leaves the query untouched"
    assert vec == fake_vector("plain bge-m3 query")


def test_search_prefixes_query_but_ingest_stays_plain(kb, monkeypatch):
    """The asymmetric contract end-to-end: with a non-empty prefix configured,
    ingest's document/chunk embeds are unprefixed while search()'s query embed
    carries the prefix — exactly once, never the raw query."""
    seen: list[str] = []
    calls: list[list[str]] = []

    def flatten(texts: list[str]) -> list[list[float]]:
        seen.extend(texts)
        calls.append(list(texts))
        return [fake_vector(t) for t in texts]

    monkeypatch.setattr(embed, "embed_texts", flatten)
    monkeypatch.setattr(settings, "kb_embed_query_prefix", PREFIX)

    ingest("workshop", "bug", "Gearbox stalls", "the gearbox stalls under heavy load")
    assert seen, "ingest embedded at least one document chunk"
    assert all(not t.startswith(PREFIX) for t in seen), (
        "document-side embeds must NEVER carry the query prefix (asymmetric contract)"
    )

    seen.clear()
    search("gearbox stalls", tool=None)
    assert seen.count(PREFIX + "gearbox stalls") == 1, (
        "search embeds the user query exactly once, WITH the configured prefix"
    )
    assert "gearbox stalls" not in seen, "the raw unprefixed query is never embedded"


def test_embed_query_routes_through_the_embed_texts_seam(monkeypatch):
    """Design requirement, not an accident: conftest's autouse fake_embeddings
    monkeypatches ONLY embed.embed_texts, and the whole suite relies on that
    single seam staying Ollama-free. embed_query must therefore resolve through
    the module-level embed_texts attribute — any raw HTTP here means it grew
    its own transport and escaped the seam."""

    def no_http(*args, **kwargs):
        raise AssertionError("embed_query bypassed the embed_texts seam (raw HTTP call)")

    monkeypatch.setattr(embed.httpx, "post", no_http)
    # Only the autouse fake_embeddings stands between embed_query and no_http.
    monkeypatch.setattr(settings, "kb_embed_query_prefix", "")
    assert embed.embed_query("hello") == fake_vector("hello")
    monkeypatch.setattr(settings, "kb_embed_query_prefix", PREFIX)
    assert embed.embed_query("hello") == fake_vector(PREFIX + "hello")

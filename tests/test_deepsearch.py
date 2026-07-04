"""Deep search (kbcore.deepsearch) + the llm chat plumbing it relies on.

The LLM is always mocked (plan/synthesize monkeypatched, or httpx faked);
retrieval runs against the real kb_test DB with conftest's fake embeddings —
the keyword leg carries relevance deterministically.
"""

from __future__ import annotations

from kbcore import llm
from kbcore.config import settings


class _FakeResp:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"message": {"content": self._content}}


def test_chat_keep_alive_and_model_passthrough(monkeypatch):
    captured: list[dict] = []

    def fake_post(url, json=None, timeout=None):
        captured.append(json)
        return _FakeResp('{"ok": true}')

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    # defaults: extract model, keep_alive 0 (Invariant 8)
    llm.chat([{"role": "user", "content": "x"}], json_format=True)
    assert captured[-1]["model"] == settings.kb_extract_model
    assert captured[-1]["keep_alive"] == 0
    assert captured[-1]["format"] == "json"
    # overrides propagate
    llm.chat([{"role": "user", "content": "x"}], model="other:1b", keep_alive=120)
    assert captured[-1]["model"] == "other:1b"
    assert captured[-1]["keep_alive"] == 120
    assert "format" not in captured[-1]


def test_deep_model_falls_back_to_extract_model(monkeypatch):
    monkeypatch.setattr(settings, "kb_deep_model", "")
    assert settings.deep_model == settings.kb_extract_model
    monkeypatch.setattr(settings, "kb_deep_model", "big:70b")
    assert settings.deep_model == "big:70b"


# --- pipeline (plan/synthesize mocked; retrieval real over kb_test) -----------------


import pytest  # noqa: E402

from kbcore import db, deepsearch  # noqa: E402
from kbcore.graph import link  # noqa: E402
from kbcore.ingest import ingest  # noqa: E402


@pytest.fixture()
def corpus(kb, monkeypatch):
    """Keyword-rich items so the keyword leg retrieves deterministically."""
    a = ingest("dsproj", "spec", "modbus rtu framing", "modbus rtu uses crc16 framing", uri="a")
    b = ingest("dsproj", "gotcha", "modbus timing gotcha", "modbus needs 3.5 char gaps", uri="b")
    c = ingest("dsproj", "note", "unrelated cooking note", "how to bake bread", uri="c")
    monkeypatch.setattr(
        deepsearch, "plan_queries", lambda q: ["modbus rtu framing", "modbus timing"]
    )
    monkeypatch.setattr(
        deepsearch,
        "synthesize",
        lambda q, docs, conflicts: f"Answer citing [item {docs[0]['id']}]. ({len(docs)} docs)",
    )
    return {"a": a, "b": b, "c": c}


def test_deep_search_saves_report_with_sources(corpus):
    res = deepsearch.deep_search("how does modbus framing work?")
    assert corpus["a"] in res.sources and corpus["b"] in res.sources
    # hybrid search returns k-best (the vector leg scores everything), so the
    # off-topic item may appear — but never above the keyword-relevant ones.
    if corpus["c"] in res.sources:
        assert res.sources.index(corpus["c"]) > res.sources.index(corpus["a"])
    assert res.item_id is not None
    assert "Sources:" in res.report
    with db.connect() as conn:
        row = conn.execute(
            "SELECT i.type, i.project, i.title, i.meta, s.trust, s.uri "
            "FROM items i JOIN sources s ON s.id = i.source_id WHERE i.id = %s",
            (res.item_id,),
        ).fetchone()
    typ, project, title, meta, trust, uri = row
    assert typ == "report" and project == "kb-system" and trust == "unverified"
    assert uri.startswith("deepsearch:")
    assert title == "Deep search: how does modbus framing work?"
    assert meta["deep_search"] == "how does modbus framing work?"
    assert set(meta["sources"]) == set(res.sources)
    assert meta["model"] == settings.deep_model


def test_retrieval_dedupes_and_excludes_reports(corpus, monkeypatch):
    # the same item matches both sub-queries -> appears once
    res = deepsearch.deep_search("modbus", save=False)
    assert len(res.sources) == len(set(res.sources))
    # a previous deep-search REPORT matching the query is never retrieved
    rep = ingest(
        "dsproj", "report", "old modbus report", "modbus rtu crc16 framing report", uri="r"
    )
    res2 = deepsearch.deep_search("modbus", save=False)
    assert rep not in res2.sources, "type='report' excluded (echo chamber)"


def test_context_caps_enforced_and_logged(corpus, monkeypatch, caplog):
    captured: dict = {}

    def spy_synth(q, docs, conflicts):
        captured["docs"] = docs
        return "ok"

    monkeypatch.setattr(deepsearch, "synthesize", spy_synth)
    monkeypatch.setattr(deepsearch, "BODY_CHAR_CAP", 10)
    monkeypatch.setattr(deepsearch, "CONTEXT_CHAR_CAP", 40)
    with caplog.at_level("INFO", logger="kbcore.deepsearch"):
        deepsearch.deep_search("modbus", save=False)
    for d in captured["docs"]:
        assert len(d["body"]) <= 10 + len("\n[... truncated]")
    assert any("truncated" in r.message or "dropped" in r.message for r in caplog.records)


def test_plan_falls_back_to_question_on_bad_json(kb, monkeypatch):
    monkeypatch.setattr(deepsearch.llm, "chat", lambda *a, **k: "not json at all")
    assert deepsearch.plan_queries("my question") == ["my question"]


def test_no_save_and_empty_retrieval(corpus, monkeypatch):
    res = deepsearch.deep_search("modbus", save=False)
    assert res.item_id is None
    with db.connect() as conn:
        n = conn.execute("SELECT count(*) FROM items WHERE type='report'").fetchone()[0]
    assert n == 0, "no report row when save=False"
    # empty retrieval: synthesize must never be called, nothing saved. The
    # vector leg scores every item, so force the empty branch directly.
    monkeypatch.setattr(deepsearch, "_retrieve", lambda *a: [])
    monkeypatch.setattr(
        deepsearch,
        "synthesize",
        lambda *a: (_ for _ in ()).throw(AssertionError("synthesize called on empty")),
    )
    res2 = deepsearch.deep_search("nothing matches this")
    assert res2.item_id is None and res2.sources == []
    assert "No KB items matched" in res2.report


def test_conflicts_passed_to_synthesize(corpus, monkeypatch):
    link(corpus["a"], "contradicts", corpus["b"])
    captured: dict = {}

    def spy_synth(q, docs, conflicts):
        captured["conflicts"] = conflicts
        return "ok"

    monkeypatch.setattr(deepsearch, "synthesize", spy_synth)
    deepsearch.deep_search("modbus", save=False)
    assert (corpus["a"], corpus["b"]) in captured["conflicts"]


# --- surfaces: CLI + MCP -------------------------------------------------------------


def test_cli_deep_search_smoke(corpus):
    from typer.testing import CliRunner

    from kbcli.main import app

    result = CliRunner().invoke(app, ["deep-search", "modbus framing", "--no-save"])
    assert result.exit_code == 0, result.output
    assert "machine-synthesized from stored data" in result.output  # provenance header
    assert "Answer citing" in result.output
    assert "(not saved)" in result.output


def test_mcp_kb_deep_search_registered(corpus):
    import asyncio

    from fastmcp import Client, FastMCP

    from kbmcp import tools_deepsearch

    mcp = FastMCP(name="kb-test")
    tools_deepsearch.register(mcp)

    async def run():
        async with Client(mcp) as client:
            tools = await client.list_tools()
            assert any(t.name == "kb_deep_search" for t in tools)
            res = await client.call_tool(
                "kb_deep_search", {"question": "modbus framing", "save": False}
            )
            return res.content[0].text

    text = asyncio.run(run())
    assert "machine-synthesized" in text and "Answer citing" in text

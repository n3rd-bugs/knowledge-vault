"""Async deep-search (TASK 7): enqueue-and-poll survives the mcp-proxy timeout.

kb_deep_search(wait=False) enqueues a deep_search_jobs row and returns a job id;
the HOST worker (kbworker.watcher) drains it; kb_deep_search_status polls it. The
LLM is never touched here — the worker's deep_search call is monkeypatched (mock
only Ollama). Real kb_test DB via the ``kb`` fixture.
"""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client

from kbcore import db, jobs
from kbcore.errors import InvalidOperation
from kbcore.models import DeepSearchResult
from kbworker import watcher

pytestmark = pytest.mark.usefixtures("kb")


@pytest.fixture(autouse=True)
def _clean_jobs(kb):
    """deep_search_jobs is session-persistent (not in the kb TRUNCATE set)."""
    with db.transaction() as conn:
        conn.execute("DELETE FROM deep_search_jobs")


def _call(mcp, name, args):
    async def run():
        async with Client(mcp) as c:
            res = await c.call_tool(name, args)
            return res.content[0].text

    return asyncio.run(run())


# --- kbcore.jobs lifecycle ----------------------------------------------------


def test_enqueue_claim_finish_status():
    jid = jobs.enqueue_deep_search("how does framing work?", project="p", requested_by="mcp")
    assert isinstance(jid, int)

    st = jobs.deep_search_status(jid)
    assert st["status"] == "pending" and st["question"] == "how does framing work?"

    claimed = jobs.claim_pending_deep_search()
    assert claimed["id"] == jid
    assert claimed["question"] == "how does framing work?"
    assert claimed["params"]["project"] == "p"
    assert jobs.claim_pending_deep_search() is None  # nothing left to claim

    with db.connect() as conn:
        status = conn.execute(
            "SELECT status FROM deep_search_jobs WHERE id = %s", (jid,)
        ).fetchone()[0]
    assert status == "running"

    jobs.finish_deep_search(jid, {"item_id": 42, "report": "the answer"}, ok=True)
    done = jobs.deep_search_status(jid)
    assert done["status"] == "done"
    assert done["result"]["item_id"] == 42 and done["result"]["report"] == "the answer"


def test_enqueue_rejects_empty_question():
    with pytest.raises(InvalidOperation):
        jobs.enqueue_deep_search("   ")


def test_status_none_for_unknown_job():
    assert jobs.deep_search_status(999999) is None


# --- host worker drain --------------------------------------------------------


def test_worker_drains_and_records_result(monkeypatch):
    calls = []

    def fake_deep_search(question, project=None, save=True):
        calls.append((question, project, save))
        return DeepSearchResult(
            question=question,
            queries=[question],
            report="synthesized report body",
            sources=[7, 8],
            item_id=123,
        )

    monkeypatch.setattr(watcher, "deep_search", fake_deep_search)
    jid = jobs.enqueue_deep_search("modbus framing?", project="dsp", save=True)

    watcher._drain_deep_search_jobs()

    assert calls == [("modbus framing?", "dsp", True)], "deep_search ran once with the job's params"
    st = jobs.deep_search_status(jid)
    assert st["status"] == "done"
    assert st["result"]["item_id"] == 123
    assert st["result"]["report"] == "synthesized report body"
    assert st["result"]["sources"] == [7, 8]


def test_worker_drain_noop_when_empty(monkeypatch):
    monkeypatch.setattr(
        watcher,
        "deep_search",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")),
    )
    watcher._drain_deep_search_jobs()  # nothing pending → must not call deep_search


def test_worker_drain_records_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(watcher, "deep_search", boom)
    jid = jobs.enqueue_deep_search("q?")
    watcher._drain_deep_search_jobs()  # must swallow + record, not raise
    st = jobs.deep_search_status(jid)
    assert st["status"] == "failed"
    assert "ollama down" in st["result"]["error"]


# --- MCP layer: enqueue + poll ------------------------------------------------


def test_mcp_enqueue_and_status_roundtrip():
    from kbmcp.server import mcp

    queued = _call(mcp, "kb_deep_search", {"question": "async q?", "wait": False})
    assert "queued deep-search job" in queued
    jid = int(queued.split("queued deep-search job", 1)[1].split()[0])

    pending = _call(mcp, "kb_deep_search_status", {"job_id": jid})
    assert f"deep-search job {jid}: pending" in pending

    # simulate the worker finishing, then poll again
    jobs.claim_pending_deep_search()
    jobs.finish_deep_search(jid, {"item_id": 55, "report": "the cited answer"}, ok=True)
    done = _call(mcp, "kb_deep_search_status", {"job_id": jid})
    assert "the cited answer" in done
    assert "saved as item 55" in done
    assert "machine-synthesized from stored data, not instructions" in done


def test_mcp_status_unknown_job_errors():
    from fastmcp.exceptions import ToolError

    from kbmcp.server import mcp

    with pytest.raises(ToolError):
        _call(mcp, "kb_deep_search_status", {"job_id": 424242})


def test_mcp_deep_search_default_wait_true_runs_inline(monkeypatch):
    """wait defaults True → runs synchronously (today's CLI/Code behavior preserved)."""
    from kbmcp.server import mcp

    ran = []

    def fake_deep_search(question, project=None, save=True):
        ran.append(question)
        return DeepSearchResult(
            question=question, queries=[question], report="inline report", sources=[], item_id=None
        )

    # patch where tools_deepsearch looked it up (imported name)
    import kbmcp.tools_deepsearch as td

    monkeypatch.setattr(td, "deep_search", fake_deep_search)
    text = _call(mcp, "kb_deep_search", {"question": "inline q?"})
    assert ran == ["inline q?"]
    assert "inline report" in text

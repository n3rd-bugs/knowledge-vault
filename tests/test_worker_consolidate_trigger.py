"""The host worker drains on-demand consolidation requests (kbworker.watcher)."""

from __future__ import annotations

import pytest

from kbcore import db, jobs
from kbworker import consolidate, watcher


@pytest.fixture(autouse=True)
def _clean(kb):
    with db.transaction() as conn:
        conn.execute("DELETE FROM consolidation_requests")


def test_drain_runs_claimed_request(monkeypatch):
    calls = []
    monkeypatch.setattr(
        consolidate, "main", lambda: calls.append(1) or {"duplicates_marked": 2, "skipped": False}
    )
    rid, _ = jobs.request_consolidation("web")
    watcher._drain_consolidation_requests()
    assert calls == [1], "consolidate.main ran exactly once"
    st = jobs.latest_consolidation_status()
    assert st["id"] == rid and st["status"] == "done"
    assert st["result"]["duplicates_marked"] == 2


def test_drain_noop_when_nothing_pending(monkeypatch):
    monkeypatch.setattr(
        consolidate, "main", lambda: (_ for _ in ()).throw(AssertionError("should not run"))
    )
    watcher._drain_consolidation_requests()  # no pending row → must not call main


def test_drain_records_failure(monkeypatch):
    def boom():
        raise RuntimeError("ollama down")

    monkeypatch.setattr(consolidate, "main", boom)
    rid, _ = jobs.request_consolidation("cli")
    watcher._drain_consolidation_requests()  # must swallow + record, not raise
    st = jobs.latest_consolidation_status()
    assert st["id"] == rid and st["status"] == "failed"
    assert "ollama down" in st["result"]["error"]

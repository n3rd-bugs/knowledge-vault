"""The host worker drains on-demand consolidation requests (kbworker.watcher)."""

from __future__ import annotations

import logging

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


def test_the_stored_result_and_log_line_drop_query_keyed_dicts(monkeypatch, caplog):
    """Review #10 F16: kb_consolidate / the CLI / GET /api/consolidate never carry them."""
    from kbworker import eval_cases

    q = "synthetic prose question kept out of job results"
    counts = {"skipped": False, "eval_hit_at_5": 0.9, "eval_case_ranks": {q: 1}}
    counts.update({k: {q: 1} for k in eval_cases.QUERY_KEYED if k != "eval_case_fused_error"})
    counts["eval_case_fused_error"] = [q]
    monkeypatch.setattr(consolidate, "main", lambda: counts)
    jobs.request_consolidation("rest")
    with caplog.at_level(logging.INFO, logger="kbworker.watcher"):
        watcher._drain_consolidation_requests()
    stored = jobs.latest_consolidation_status()["result"]
    assert stored == {"skipped": False, "eval_hit_at_5": 0.9}
    assert q not in caplog.text and "eval_case_" not in caplog.text
    assert counts["eval_case_ranks"] == {q: 1}, "the run's own counts are not mutated"

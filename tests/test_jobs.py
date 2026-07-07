"""kbcore.jobs — the on-demand consolidation request/claim/finish lifecycle."""

from __future__ import annotations

import pytest

from kbcore import db, jobs


@pytest.fixture(autouse=True)
def _clean(kb):
    """The control table is session-persistent; isolate each test."""
    with db.transaction() as conn:
        conn.execute("DELETE FROM consolidation_requests")


def test_request_dedupes_against_active(kb):
    rid, created = jobs.request_consolidation("cli")
    assert created is True
    rid2, created2 = jobs.request_consolidation("web")
    assert created2 is False and rid2 == rid, "a second request folds into the pending one"


def test_claim_is_atomic_and_advances_status(kb):
    rid, _ = jobs.request_consolidation("rest")
    claimed = jobs.claim_pending_consolidation()
    assert claimed == rid
    with db.connect() as conn:
        status, started = conn.execute(
            "SELECT status, started_at FROM consolidation_requests WHERE id = %s", (rid,)
        ).fetchone()
    assert status == "running" and started is not None
    assert jobs.claim_pending_consolidation() is None, "nothing left to claim"


def test_request_after_running_still_dedupes(kb):
    rid, _ = jobs.request_consolidation("cli")
    jobs.claim_pending_consolidation()  # -> running
    rid2, created = jobs.request_consolidation("web")
    assert created is False and rid2 == rid, "a running job also blocks a new request"


def test_finish_records_result(kb):
    rid, _ = jobs.request_consolidation("cli")
    jobs.claim_pending_consolidation()
    jobs.finish_consolidation(rid, {"duplicates_marked": 3}, ok=True)
    st = jobs.latest_consolidation_status()
    assert st["id"] == rid and st["status"] == "done"
    assert st["result"]["duplicates_marked"] == 3
    assert st["finished_at"] is not None
    # a fresh request is now allowed again (the previous one is done)
    _, created = jobs.request_consolidation("cli")
    assert created is True


def test_finish_failure_marks_failed(kb):
    rid, _ = jobs.request_consolidation("cli")
    jobs.claim_pending_consolidation()
    jobs.finish_consolidation(rid, {"error": "boom"}, ok=False)
    assert jobs.latest_consolidation_status()["status"] == "failed"


def test_status_none_when_empty(kb):
    assert jobs.latest_consolidation_status() is None

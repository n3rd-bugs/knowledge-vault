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


# --------------------------------------------------------------------------
# Stale-run recovery. A run that died mid-flight left its request 'running'
# forever, and because request_consolidation dedupes against
# ('pending','running') that one orphan silently swallowed every later request.
# Observed in production: a container redeploy killed a run and 308 queue rows
# (227 MB) sat untouched for 13 hours while each request returned the dead id.
# --------------------------------------------------------------------------


def _age_running(hours: int) -> None:
    from kbcore.db import transaction

    with transaction() as conn:
        conn.execute(
            "UPDATE consolidation_requests SET started_at = now() - make_interval(hours => %s) "
            "WHERE status = 'running'",
            (hours,),
        )


def test_a_dead_run_does_not_block_consolidation_forever(kb):
    from kbcore import jobs

    first, created = jobs.request_consolidation("test")
    assert created
    assert jobs.claim_pending_consolidation() == first  # now 'running', and never finishes
    _age_running(jobs.STALE_RUN_HOURS + 1)

    second, created2 = jobs.request_consolidation("test")
    assert created2, "a dead run still blocked a new request"
    assert second != first
    assert jobs.latest_consolidation_status()["status"] == "pending"


def test_a_LIVE_run_still_dedupes(kb):
    """The reaper must not undo the anti-spam guarantee: a run that is genuinely
    in flight still absorbs further requests."""
    from kbcore import jobs

    first, _ = jobs.request_consolidation("test")
    jobs.claim_pending_consolidation()
    _age_running(1)  # young enough to be alive

    second, created = jobs.request_consolidation("test")
    assert not created and second == first, "a live run was reaped and duplicated"


def test_the_reaped_row_records_why(kb):
    """A silently-vanished run teaches nobody anything."""
    from kbcore import jobs
    from kbcore.db import connect

    rid, _ = jobs.request_consolidation("test")
    jobs.claim_pending_consolidation()
    _age_running(jobs.STALE_RUN_HOURS + 1)
    jobs.request_consolidation("test")

    with connect() as conn:
        status, result = conn.execute(
            "SELECT status, result FROM consolidation_requests WHERE id = %s", (rid,)
        ).fetchone()
    assert status == "failed"
    assert result and result.get("reaped") is True
    assert "died mid-run" in result.get("reason", "")

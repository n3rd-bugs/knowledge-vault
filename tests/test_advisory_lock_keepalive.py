"""A held advisory lock survives a Postgres restart, or says that it was lost (KB 61595).

2026-11-07 15:00: an apt upgrade restarted docker 9 minutes into a consolidation pass. Postgres
ended the lock's connection and the lock with it, the pass ran on unlocked for 2 h 51 min and
then failed at its final unlock. Real DB (kb_test): the lock's backend is ended with
pg_terminate_backend, which is what a Postgres fast shutdown does to it.
"""

from __future__ import annotations

import threading
import time

import psycopg
import pytest

from kbcore import db, db_lock, evidence
from kbcore.consolidation import record_contradiction
from kbcore.errors import LockLost
from kbcore.ingest import ingest
from kbworker import consolidate, evidence_step, run_report

KEY = 0x6C6B  # test-only ('lk'); schema-scoped like every key
STAMP = evidence.STREAK_STAMP


@pytest.fixture(autouse=True)
def _slow_keepalive(monkeypatch):
    """The thread waits an hour: these tests call ``check()`` themselves, except one."""
    monkeypatch.setattr(db_lock, "LOCK_KEEPALIVE_SECS", 3600.0)


def _end(hold: db_lock.LockHold) -> None:
    """End the hold's backend and wait (PG14+) until it has exited, as a restart would."""
    pid = hold.conn.info.backend_pid
    conn = db.connect()
    conn.autocommit = True
    try:
        assert conn.execute("SELECT pg_terminate_backend(%s, 5000)", (pid,)).fetchone()[0]
    finally:
        conn.close()


def _free() -> bool:
    """True when another session could take the lock right now."""
    with db.advisory_lock(KEY) as probe:
        return bool(probe)


@pytest.fixture()
def other(kb):
    """A second session that can take the lock while the hold's connection is down."""
    conn = db.connect()
    conn.autocommit = True
    yield conn
    conn.execute("SELECT pg_advisory_unlock_all()")
    conn.close()


def _take(conn: psycopg.Connection, hold: db_lock.LockHold) -> None:
    assert conn.execute("SELECT pg_try_advisory_lock(%s)", (hold.scoped,)).fetchone()[0]


def _lose(hold: db_lock.LockHold, other: psycopg.Connection) -> None:
    """Another session takes the lock in the gap and keeps it across two probes."""
    _end(hold)
    _take(other, hold)
    hold.check()
    hold.check()
    assert hold.lost


def test_a_hold_is_truthy_only_when_taken(kb):
    with db.advisory_lock(KEY) as first:
        assert first and first.acquired and first.held
        with db.advisory_lock(KEY) as second:
            assert not second
    assert _free()


def test_a_live_connection_is_left_alone(kb):
    with db.advisory_lock(KEY) as hold:
        pid = hold.conn.info.backend_pid
        hold.check()
        assert hold.conn.info.backend_pid == pid and hold.retaken == 0 and not hold.lost


def test_the_lock_is_taken_again_after_its_connection_ends(kb):
    with db.advisory_lock(KEY) as hold:
        old = hold.conn.info.backend_pid
        _end(hold)
        hold.check()
        assert hold.retaken == 1 and hold.held and not hold.lost
        assert hold.conn.info.backend_pid != old
        assert not _free(), "the hold has the lock again"
        db.check_lock()  # does not raise
        assert db.lock_retaken() == 1
    assert _free(), "and releases it at the end"


def test_one_refusal_is_not_a_loss_and_a_cleared_one_is_taken_again(kb, other):
    with db.advisory_lock(KEY) as hold:
        _end(hold)
        _take(other, hold)  # a try-and-release probe in the gap, say
        hold.check()
        assert not hold.held and not hold.lost and hold.refusals == 1
        db.check_lock()  # one refusal: not lost yet
        other.execute("SELECT pg_advisory_unlock(%s)", (hold.scoped,))
        hold.check()
        assert hold.held and hold.retaken == 1 and hold.refusals == 0
        assert not _free()
    assert _free()


def test_a_lock_another_session_keeps_is_lost(kb, other):
    with db.advisory_lock(KEY) as hold:
        _lose(hold, other)
        assert hold.retaken == 0
        with pytest.raises(LockLost):
            db.check_lock()
        assert db.lock_lost()
    assert not _free(), "release must not touch the other session's lock"


def test_a_failed_query_on_a_live_session_keeps_the_lock(kb):
    """No statement_timeout here today, but a cancelled probe must not close the lock's own
    session: the old code would have re-taken against itself and dropped the lock."""

    class Cancelling:
        def __init__(self, conn):
            self.conn, self.closed, self.broken = conn, conn.closed, conn.broken

        def execute(self, *a, **k):
            raise psycopg.errors.QueryCanceled("canceling statement due to user request")

    with db.advisory_lock(KEY) as hold:
        real = hold.conn
        hold.conn = Cancelling(real)
        hold.check()
        hold.conn = real
        assert hold.held and hold.retaken == 0 and not hold.lost
        assert not _free(), "the lock went with a session that was never down"
    assert _free()


def test_release_tolerates_a_connection_that_already_ended(kb, caplog):
    with db.advisory_lock(KEY) as hold:
        _end(hold)
    assert "ended with its connection" in caplog.text
    assert _free()


def test_a_keepalive_that_cannot_start_still_releases_the_lock(kb, monkeypatch):
    def boom(self):
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as m:
        m.setattr(threading.Thread, "start", boom)
        with pytest.raises(RuntimeError, match="start new thread"):
            with db.advisory_lock(KEY):
                pass  # never reached: keep_alive raises first
    assert _free(), "the lock outlived a hold whose keepalive never started"


def test_the_keepalive_thread_takes_it_back_on_its_own(kb, monkeypatch):
    monkeypatch.setattr(db_lock, "LOCK_KEEPALIVE_SECS", 0.05)
    with db.advisory_lock(KEY) as hold:
        _end(hold)
        deadline = time.monotonic() + 10
        while hold.retaken == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert hold.retaken == 1
    assert _free()


def test_a_pass_stops_at_its_next_step_once_its_lock_is_lost(kb, other):
    ran: list[int] = []
    with db.advisory_lock(KEY) as hold:
        _lose(hold, other)
        with pytest.raises(LockLost):
            run_report._run_step({}, "step", lambda: ran.append(1) or 1)
    assert ran == [], "the step ran beside the new holder"


def test_the_stand_down_guard_says_so_inside_a_step(kb, other, monkeypatch):
    """The drain's between-cohort guard and every stop-gated loop ask should_stop()."""
    monkeypatch.setattr(consolidate, "_past_deadline", lambda: False)  # hermetic: no clock,
    monkeypatch.setattr(consolidate, "_on_battery", lambda: False)  # no power database
    with db.advisory_lock(KEY) as hold:
        consolidate.reset_stop_cache()
        assert consolidate.should_stop() is None
        _lose(hold, other)
        consolidate.reset_stop_cache()
        assert consolidate.should_stop() == "lock lost"
    consolidate.reset_stop_cache()


def test_a_lost_lock_stops_evidence_phase_two_before_it_retires_anything(kb, monkeypatch):
    """Phase 2 writes the retirements and asks neither stop() nor the clock, so it asks the
    lock itself; a row it skips stays due for the next pass."""
    keep = ingest("lk", "qa", "mode 0 is current", "use mode 0", uri="lk-keep", trust="unverified")
    retire = ingest("lk", "qa", "mode 1 is current", "use mode 1", uri="lk-ret", trust="unverified")
    assert record_contradiction(keep, retire) is True
    sugg = {"action": "supersede", "keep": keep, "retire": retire, "rule": "evidence: test"}
    with db.connect() as conn:
        qid = conn.execute(
            "SELECT id FROM review_queue WHERE reason = 'contradiction' "
            "AND (detail->>'a_id')::bigint = %s",
            (keep,),
        ).fetchone()[0]
    night_one = "2026-11-06T20:30:00+00:00"
    update = {"suggested_resolution": sugg, "suggestion_streak": 1, STAMP: night_one}
    evidence.update_review_detail(qid, update)
    monkeypatch.setattr(
        evidence,
        "gather_evidence",
        lambda a, b: {"a_id": a, "b_id": b, "suggested_resolution": sugg},
    )
    monkeypatch.setattr(consolidate, "EVIDENCE_PAIR_CAP", 10)
    monkeypatch.setattr(evidence_step, "lock_lost", lambda: True)
    assert consolidate._evidence(pass_at="2026-11-07T20:30:00+00:00") == (1, 0, 0)
    with db.connect() as conn:
        status = conn.execute("SELECT status FROM items WHERE id = %s", (retire,)).fetchone()[0]
        resolved, detail = conn.execute(
            "SELECT resolved, detail FROM review_queue WHERE id = %s", (qid,)
        ).fetchone()
    assert status == "active", "phase 2 retired an item beside the new lock holder"
    assert not resolved and detail["suggestion_streak"] == 2, "the row stays due"


def test_a_lock_lost_in_the_last_working_step_stops_the_pass_before_the_eval(kb, monkeypatch):
    """The eval sentinel is not a _run_step: the pass asks check_lock() itself before it."""
    from kbworker import tail_steps

    evals: list[int] = []

    def topup(counts, stop=None):
        for hold in db._HOLDS:  # the pass's own lock goes to another session mid-step
            hold.lost = True
        return 0

    monkeypatch.setattr(tail_steps, "review_triaged_topup", topup)
    monkeypatch.setattr(consolidate.eval_sentinel, "run_sentinel", lambda counts: evals.append(1))
    with pytest.raises(LockLost):
        consolidate.main()
    assert evals == [], "the eval replayed beside the new holder"


def test_a_retaken_lock_reaches_the_report(kb):
    counts: dict = {}
    with db.advisory_lock(KEY) as hold:
        _end(hold)
        hold.check()
        run_report._run_step(counts, "step", lambda: 7)
    assert counts["step"] == 7 and counts["advisory_lock_retaken"] == 1


def test_a_retake_after_the_last_step_still_reaches_the_report(kb):
    counts: dict = {"run_started_at": "2026-11-07T20:30:00+00:00"}
    with db.advisory_lock(KEY) as hold:
        _end(hold)
        hold.check()
        assert run_report._write_report(counts) > 0
    assert counts["advisory_lock_retaken"] == 1


def test_another_threads_lost_lock_is_not_this_threads_problem(kb, other):
    """check_lock reads only the calling thread's holds (the servers are threaded)."""
    ready, done = threading.Event(), threading.Event()

    def holder():
        with db.advisory_lock(KEY) as hold:
            _lose(hold, other)
            ready.set()
            done.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert ready.wait(10)
        db.check_lock()  # this thread holds nothing: no raise
        assert not db.lock_lost() and db.lock_retaken() == 0
    finally:
        done.set()
        t.join(10)

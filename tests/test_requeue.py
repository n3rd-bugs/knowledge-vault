"""requeue_failed — an owner repair that returns misclassified rows to the drain."""

from __future__ import annotations

import pytest

from kbcore import ingest_queue, scope

pytestmark = pytest.mark.usefixtures("kb")


@pytest.fixture()
def as_owner():
    with scope.use_principal(scope.OWNER):
        yield


def _mk(project: str, body: str) -> int:
    r = ingest_queue.enqueue(project, body, "session")
    assert r.id
    return r.id


def test_a_failed_row_returns_to_pending_with_attempts_reset(kb, as_owner):
    """The attempts were spent against a broken classifier; carrying them
    forward would retire the row again on a pipeline that can now succeed."""
    qid = _mk("proj", "a transient embed timeout condemned this")
    for _ in range(ingest_queue.MAX_ATTEMPTS):
        ingest_queue.mark_failed(qid, "embed ReadTimeout")

    ids = ingest_queue.requeue_failed(reason="classifier fixed")
    assert qid in ids

    assert any(r["id"] == qid for r in ingest_queue.pending(limit=100)), (
        "the requeued row is not drainable again"
    )
    from kbcore.db import connect

    with connect() as c:
        status, attempts = c.execute(
            "SELECT status, attempts FROM ingest_queue WHERE id=%s", (qid,)
        ).fetchone()
    assert (status, attempts) == ("pending", 0)


def test_requeue_never_touches_extracted_or_pending_rows(kb, as_owner):
    """A repair that revived live or finished work would duplicate knowledge."""
    done = _mk("proj", "already extracted")
    ingest_queue.mark_extracted(done, 2, 0)
    live = _mk("proj", "still pending")

    ids = ingest_queue.requeue_failed(reason="repair")
    assert done not in ids and live not in ids


def test_the_reason_is_recorded_on_the_row(kb, as_owner):
    """A status flip with no audit trail is indistinguishable from a bug."""
    qid = _mk("proj", "row with a reason")
    ingest_queue.mark_failed(qid, "embed ReadTimeout")
    ingest_queue.requeue_failed(reason="766a091 reclassified this as transient")

    from kbcore.db import connect

    with connect() as c:
        err = c.execute("SELECT error FROM ingest_queue WHERE id=%s", (qid,)).fetchone()[0]
    assert "requeued:" in err and "766a091" in err
    assert "ReadTimeout" in err, "the original verdict was overwritten"


# --------------------------------------------------- cohort batching bounds


def _job(key: str, kb_size: int) -> dict:
    return {"key": key, "text": "x" * (kb_size * 1000)}


def test_a_cohort_is_bounded_by_job_count():
    from kbworker.consolidate import _batch_jobs

    jobs = [_job(f"j{i}", 1) for i in range(10)]
    assert [len(b) for b in _batch_jobs(jobs, 4)] == [4, 4, 2]


def test_a_cohort_is_also_bounded_by_bytes(monkeypatch):
    """The byte cap is an OOM guard: a cohort holds every job's text at once."""
    from kbcore.config import settings
    from kbworker.consolidate import _batch_jobs

    monkeypatch.setattr(settings, "kb_drain_cohort_bytes", 10_000)
    jobs = [_job(f"j{i}", 4) for i in range(6)]  # 4 kB each, budget fits 2
    batches = _batch_jobs(jobs, 100)  # count cap deliberately far too high
    assert all(sum(len(j["text"]) for j in b) <= 10_000 for b in batches)
    assert [len(b) for b in batches] == [2, 2, 2]


def test_an_oversized_job_is_taken_alone_never_skipped(monkeypatch):
    """Skipping it would return an empty batch and wedge the queue forever on
    one row — a worse failure than briefly exceeding the memory guard."""
    from kbcore.config import settings
    from kbworker.consolidate import _batch_jobs

    monkeypatch.setattr(settings, "kb_drain_cohort_bytes", 5_000)
    jobs = [_job("small-a", 1), _job("huge", 50), _job("small-b", 1)]
    batches = _batch_jobs(jobs, 10)

    assert [j["key"] for b in batches for j in b] == ["small-a", "huge", "small-b"], (
        "a job was dropped"
    )
    assert ["huge"] in [[j["key"] for j in b] for b in batches], (
        "the oversized job was not isolated"
    )


# ------------------------------------------------- deadline / resumability


def test_the_deadline_is_ignored_when_unset(monkeypatch):
    from kbcore.config import settings
    from kbworker.consolidate import _past_deadline

    monkeypatch.setattr(settings, "kb_drain_deadline", "")
    assert _past_deadline() is False


def test_a_malformed_deadline_does_not_stop_the_drain(monkeypatch):
    """A typo in config must not silently halt every run after the first cohort."""
    from kbcore.config import settings
    from kbworker.consolidate import _past_deadline

    for bad in ("noon", "12", "12:xx", "::"):
        monkeypatch.setattr(settings, "kb_drain_deadline", bad)
        assert _past_deadline() is False, f"{bad!r} was not treated as 'no deadline'"


def test_the_deadline_fires_only_after_the_cutoff(monkeypatch):
    from datetime import datetime, timedelta

    from kbcore.config import settings
    from kbworker import consolidate

    now = datetime.now()
    past = (now - timedelta(minutes=5)).strftime("%H:%M")
    future = (now + timedelta(minutes=90)).strftime("%H:%M")

    monkeypatch.setattr(settings, "kb_drain_deadline", past)
    assert consolidate._past_deadline() is True, "a cutoff already passed did not fire"

    # Skip the wrap-around case near midnight, where +90min is tomorrow.
    if (now + timedelta(minutes=90)).date() == now.date():
        monkeypatch.setattr(settings, "kb_drain_deadline", future)
        assert consolidate._past_deadline() is False, "a future cutoff fired early"


def test_stopping_leaves_every_unreached_job_resumable(kb, as_owner):
    """The queue IS the checkpoint: a deferred job must still be 'pending', so the
    next run (nightly or the post-power-cut boot catch-up) picks it up."""
    qid = _mk("proj", "a job the drain never reached")
    rows = ingest_queue.pending(limit=100)
    assert any(r["id"] == qid for r in rows), "an undrained job is not resumable"


# ------------------------------------------- offline tokenizer (no internet)


def test_the_tokenizer_loads_without_network():
    """The pipeline is local-only by design, but tiktoken downloaded cl100k_base
    from Azure on first use. That killed the nightly drain when Persistent=true
    fired it ten seconds after a post-power-cut boot, before DNS was up
    (2026-07-31). The encoding is vendored; this proves it needs no socket."""
    import socket
    import subprocess
    import sys

    code = (
        "import socket\n"
        "class B(socket.socket):\n"
        "    def connect(self,*a,**k): raise OSError('no network')\n"
        "socket.socket = B\n"
        "from kbcore.chunk import _enc\n"
        "assert _enc.n_vocab == 100277\n"
        "print('ok')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"tokenizer needs the network: {r.stderr[-400:]}"
    assert "ok" in r.stdout
    assert socket is not None


def test_the_vendored_encoding_is_present_in_the_repo():
    """A fresh clone must work offline; the cache file is committed, not fetched."""
    from pathlib import Path

    import kbcore.chunk as chunk

    vendored = Path(chunk.__file__).resolve().parents[2] / "vendor" / "tiktoken"
    assert vendored.is_dir(), "vendor/tiktoken is missing"
    blobs = list(vendored.iterdir())
    assert blobs, "vendor/tiktoken is empty — the encoding was not committed"

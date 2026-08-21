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


def test_requeue_rearms_the_deferral_budget_alongside_attempts(kb, as_owner, monkeypatch):
    """Round trip for a deferral-cap escalation (review #3 P2, KB 19663). The
    owner's requeue is a fresh grant of BOTH budgets: without deferrals=0 the
    documented repair was a no-op — the row came back 'pending' still at the cap,
    so its very first re-defer escalated it straight back to 'failed'.

    Deferral burns are bounded per LOCAL day (review #5 pipe-2), so the
    multi-night outage is simulated by advancing the day boundary per defer."""
    from datetime import datetime, timedelta

    from kbcore.db import connect

    tick = {"n": 0}

    def _next_day():
        tick["n"] += 1
        return datetime(2026, 1, 1) + timedelta(days=tick["n"])

    monkeypatch.setattr(ingest_queue, "drain_day_start", _next_day)

    qid = _mk("proj", "a poison window escalated this group")
    escalated = False
    for _ in range(ingest_queue.DEFERRAL_CAP):
        escalated = ingest_queue.mark_deferred(qid, "ExtractorUnavailable: never heals")
    assert escalated is True, "the cap must escalate the row to terminal 'failed'"

    ids = ingest_queue.requeue_failed(reason="misclassification fixed")
    assert qid in ids

    with connect() as c:
        status, attempts, deferrals = c.execute(
            "SELECT status, attempts, deferrals FROM ingest_queue WHERE id=%s", (qid,)
        ).fetchone()
    assert (status, attempts, deferrals) == ("pending", 0, 0), (
        "requeue must re-arm BOTH budgets or the row re-escalates immediately"
    )

    # ...and the row can now defer the FULL cap's worth again before escalating.
    for n in range(ingest_queue.DEFERRAL_CAP - 1):
        assert ingest_queue.mark_deferred(qid, "outage") is False, f"escalated early at {n + 1}"
    assert ingest_queue.mark_deferred(qid, "outage") is True, "the cap still bounds the re-grant"


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
    monkeypatch.setattr(
        settings, "kb_drain_deadline_days", ""
    )  # day-independent: the weekend rule must not flip tests by calendar
    assert _past_deadline() is False


def test_a_malformed_deadline_does_not_stop_the_drain(monkeypatch):
    """A typo in config must not silently halt every run after the first cohort."""
    from kbcore.config import settings
    from kbworker.consolidate import _past_deadline

    for bad in ("noon", "12", "12:xx", "::"):
        monkeypatch.setattr(settings, "kb_drain_deadline", bad)
        monkeypatch.setattr(
            settings, "kb_drain_deadline_days", ""
        )  # day-independent: the weekend rule must not flip tests by calendar
        assert _past_deadline() is False, f"{bad!r} was not treated as 'no deadline'"


def test_the_deadline_fires_only_after_the_cutoff(monkeypatch):
    from datetime import datetime, timedelta

    from kbcore.config import settings
    from kbworker import consolidate

    now = datetime.now()
    past = (now - timedelta(minutes=5)).strftime("%H:%M")
    future = (now + timedelta(minutes=90)).strftime("%H:%M")

    monkeypatch.setattr(settings, "kb_drain_deadline", past)
    monkeypatch.setattr(
        settings, "kb_drain_deadline_days", ""
    )  # day-independent: the weekend rule must not flip tests by calendar
    assert consolidate._past_deadline() is True, "a cutoff already passed did not fire"

    # Skip the wrap-around case near midnight, where +90min is tomorrow.
    if (now + timedelta(minutes=90)).date() == now.date():
        monkeypatch.setattr(settings, "kb_drain_deadline", future)
        monkeypatch.setattr(
            settings, "kb_drain_deadline_days", ""
        )  # day-independent: the weekend rule must not flip tests by calendar
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


# ------------------------------------------------- pause the GPU on battery


def _power_db(tmp_path, volts, age_seconds=0):
    """A stand-in sungrow monitor DB. `volts` may be one value or a series
    (newest first), so the window behaviour can be exercised."""
    import sqlite3
    from datetime import UTC, datetime, timedelta

    series = volts if isinstance(volts, (list, tuple)) else [volts]
    p = tmp_path / "power.sqlite3"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE monitor_inverterreading (grid_voltage REAL, timestamp TEXT)")
    base = datetime.now(UTC) - timedelta(seconds=age_seconds)
    for i, v in enumerate(series):
        c.execute(
            "INSERT INTO monitor_inverterreading VALUES (?,?)",
            (v, (base - timedelta(seconds=i)).isoformat()),
        )
    c.commit()
    c.close()
    return str(p)


def test_a_grid_outage_pauses_the_drain(tmp_path, monkeypatch):
    from kbcore.config import settings
    from kbworker.consolidate import _on_battery

    monkeypatch.setattr(settings, "kb_power_db", _power_db(tmp_path, 0.0))
    assert _on_battery() is True, "a dead grid did not pause the drain"


def test_normal_mains_does_not_pause(tmp_path, monkeypatch):
    """Measured normal range is 213-256V; 230 must never read as an outage."""
    from kbcore.config import settings
    from kbworker.consolidate import _on_battery

    monkeypatch.setattr(settings, "kb_power_db", _power_db(tmp_path, 230.0))
    assert _on_battery() is False


def test_a_stale_reading_fails_SAFE_and_pauses_the_gpu(tmp_path, monkeypatch, caplog):
    """REVERSED 2026-08-04: a configured-but-STALE reading now stands the drain
    down, not proceeds. The old fail-OPEN fired at exactly the wrong moment —
    right after a grid outage the sungrow poller is the thing most likely dead,
    so 'proceed on mains' kept the GPU running into the outage the guard exists
    to catch. On a UPS that cannot ride out a fluctuation, an UNKNOWN house-power
    state is treated as on-battery. (A MISSING db still fails open — feature off,
    a different case: test_a_missing_power_db_fails_open_loudly.)"""
    import logging

    from kbcore.config import settings
    from kbworker.consolidate import _on_battery

    monkeypatch.setattr(settings, "kb_power_db", _power_db(tmp_path, 0.0, age_seconds=99_999))
    with caplog.at_level(logging.WARNING, logger="kbworker.consolidate"):
        assert _on_battery() is True, "a stale reading no longer pauses the GPU"
    assert "STALE" in caplog.text, "the staleness was not surfaced"


def test_a_missing_power_db_fails_open_loudly(monkeypatch, caplog):
    import logging

    from kbcore.config import settings
    from kbworker.consolidate import _on_battery

    monkeypatch.setattr(settings, "kb_power_db", "/nonexistent/nope.sqlite3")
    with caplog.at_level(logging.WARNING, logger="kbworker.consolidate"):
        assert _on_battery() is False
    assert "power check" in caplog.text


def test_the_check_is_disabled_when_unconfigured(monkeypatch):
    from kbcore.config import settings
    from kbworker.consolidate import _on_battery

    monkeypatch.setattr(settings, "kb_power_db", "")
    assert _on_battery() is False


def test_a_transitional_sag_also_pauses(tmp_path, monkeypatch):
    """Owner decision: keep the GPU down through TRANSITIONS, not just hard
    outages. 195V is a sag, not an outage, and must still pause."""
    from kbcore.config import settings
    from kbworker.consolidate import _on_battery

    monkeypatch.setattr(settings, "kb_power_db", _power_db(tmp_path, 195.0))
    assert _on_battery() is True, "a transitional sag did not pause the drain"


def test_any_dip_in_the_window_keeps_it_paused(tmp_path, monkeypatch):
    """Resuming into a flickering supply is worse than staying paused: the newest
    sample is healthy, but the grid dipped moments ago."""
    from kbcore.config import settings
    from kbworker.consolidate import _on_battery

    monkeypatch.setattr(settings, "kb_power_db", _power_db(tmp_path, [230.0, 228.0, 40.0, 231.0]))
    assert _on_battery() is True, "a recent dip did not keep the drain paused"


def test_a_consistently_healthy_window_resumes(tmp_path, monkeypatch):
    from kbcore.config import settings
    from kbworker.consolidate import _on_battery

    monkeypatch.setattr(settings, "kb_power_db", _power_db(tmp_path, [230.0, 229.0, 231.0, 228.0]))
    assert _on_battery() is False, "a healthy grid failed to resume"


def test_the_normal_low_band_is_not_treated_as_an_outage(tmp_path, monkeypatch):
    """212-213V is what this site reads today (6.2% of samples sit 210-220V).
    A threshold that trips on ordinary mains would pause the drain forever."""
    from kbcore.config import settings
    from kbworker.consolidate import _on_battery

    monkeypatch.setattr(settings, "kb_power_db", _power_db(tmp_path, [212.0, 213.0, 211.5]))
    assert _on_battery() is False, "ordinary low-normal mains read as an outage"

"""Review #8 H6 — ``drained_deferred`` is HONEST on all three stop paths.

The contract key (review #6) was written only on the drain-budget stop; the guard
stops (deadline/battery/tail-reserve) and the pre-first-cohort stand-down left it
at 0 while the stop log carried a different UNIT (jobs). Now every stop path
writes the still-pending ROWS that already existed when the run started — a
submission landing mid-run is still pending but was never 'deferred' — and hands
the SAME number to the stop log, so the pause marker, the report and the digest
agree on one figure.

Real DB (kb_test); the extractor is patched (Ollama never called); the state dir
is the autouse tmp one; the run guards are disarmed explicitly (day-independent).
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from kbcore import ingest_queue
from kbcore.config import settings
from kbworker import consolidate, powerguard, sessions


@pytest.fixture()
def _no_stand_down(monkeypatch):
    monkeypatch.setattr(settings, "kb_drain_deadline", "")
    monkeypatch.setattr(settings, "kb_drain_deadline_days", "")
    monkeypatch.setattr(settings, "kb_power_db", "")
    monkeypatch.setattr(settings, "kb_drain_cohort", 1)
    consolidate.reset_stop_cache()
    consolidate._STOP_LOG.clear()
    yield
    consolidate.reset_stop_cache()
    consolidate._STOP_LOG.clear()


def _enqueue(project: str, body: str) -> int:
    res = ingest_queue.enqueue(project, body, kind="session")
    assert res.id is not None
    return res.id


def _late_arrival(project: str) -> int:
    """A submission landing AFTER the run started (a strictly later created_at)."""
    time.sleep(0.01)
    return _enqueue(project, "User: late\nAssistant: late")


def test_queue_depth_before_counts_only_rows_created_before_the_boundary(kb):
    _enqueue("h6-depth", "User: a\nAssistant: a")
    now = datetime.now(UTC)
    assert ingest_queue.queue_depth() == 1, "no boundary: every pending row, as before"
    assert ingest_queue.queue_depth(before=now + timedelta(seconds=5)) == 1
    assert ingest_queue.queue_depth(before=now - timedelta(hours=1)) == 0


def test_a_pre_first_cohort_stand_down_counts_only_rows_that_existed_at_run_start(
    kb, _no_stand_down
):
    """The brief's test: enqueue after run start, stop before the first cohort,
    the late row is excluded — from the contract key AND the stop log."""
    _enqueue("h6-pre", "User: early\nAssistant: early")

    def stop() -> str:
        _late_arrival("h6-pre")  # lands after the run began, before the guard answered
        return "deadline"

    counts: dict = {}
    assert consolidate._drain_step(counts, stop) == 0
    assert consolidate._COHORT_STATS["drained_deferred"] == 1, "the late row is not deferred work"
    assert consolidate._STOP_LOG["queued_drained"] == {"reason": "deadline", "deferred": 1}
    assert ingest_queue.queue_depth() == 2, "...but it IS still pending for the next run"


def test_a_mid_run_guard_stop_writes_the_honest_count_not_only_the_budget_stop(
    kb, _no_stand_down, monkeypatch
):
    """Before H6 only DRAIN_BUDGET_REASON wrote drained_deferred; a battery or
    deadline stop between cohorts left it 0 and logged the JOBS unit."""
    for i in range(3):
        _enqueue("h6-guard", f"User: g{i}\nAssistant: g{i}")
    seen: list[str] = []

    def extract(text, project, uri, **kw):
        seen.append(uri)
        _late_arrival("h6-guard")  # a client submits while the drain is running
        return 1, 0

    monkeypatch.setattr(sessions, "extract_and_ingest", extract)
    monkeypatch.setattr(consolidate, "should_stop", lambda: "battery" if seen else None)

    drained, saved = consolidate._drain_ingest_queue()

    assert (drained, saved) == (1, 1), "the run's first cohort is exempt; the guard stops the next"
    assert consolidate._COHORT_STATS["drained_deferred"] == 2, (
        "still-pending rows that existed at run start — the mid-run arrival excluded"
    )
    assert consolidate._STOP_LOG["queued_drained"] == {"reason": "battery", "deferred": 2}
    assert consolidate._COHORT_STATS["deadline_deferred_jobs"] == 1, "jobs telemetry untouched"
    assert ingest_queue.queue_depth() == 3, "the late row stays pending for the next run"


def test_the_marker_the_report_and_the_digest_key_agree_on_a_battery_stand_down(
    kb, _no_stand_down, monkeypatch
):
    """The point of ONE number: the pause marker's 'deferred' (what the AC-restore
    trigger resumes for), counts['drained_deferred'] (the digest line) and
    counts['stopped'] (the report) are the same figure."""
    monkeypatch.setattr(consolidate.eval_sentinel, "run_sentinel", lambda counts: None)
    for i in range(2):
        _enqueue("h6-marker", f"User: m{i}\nAssistant: m{i}")

    result = consolidate.main(stop=lambda: "battery")

    assert result["skipped"] is False
    assert result["drained_deferred"] == 2
    assert result["stopped"]["queued_drained"] == {"reason": "battery", "deferred": 2}
    marker = json.loads(powerguard.battery_pause_marker_path().read_text(encoding="utf-8"))
    battery_total = sum(
        int(v["deferred"]) for v in result["stopped"].values() if v["reason"] == "battery"
    )
    assert marker["deferred"] == battery_total == result["drained_deferred"]

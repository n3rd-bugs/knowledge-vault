"""The nightly run's stand-down guards: wall clock, grid power, and their reach.

Two guards existed (KB_DRAIN_DEADLINE, the sungrow grid-voltage check) and were
asked at exactly ONE site — between drain cohorts, with the first batch exempt.
Everything after the drain (triage, contradictions, evidence: 4.6 h measured) ran
unbounded, and the run was verified still on the GPU 2h19m past the noon
deadline. The power check had a second hole: sungrow writes NAIVE LOCAL stamps
and they were read as UTC, so "how old is this reading" came out ~5 h too small
and staleness could not fire during the first five hours of a dead poller.

These tests pin the single stop question, its reach into every long loop, the
run report that says WHY a run was short, and the timezone carve-out.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from kbcore import llm, review_triage
from kbcore.config import settings
from kbworker import allocator, consolidate


@pytest.fixture(autouse=True)
def _fresh_guard():
    """The verdict is memoized for a few seconds; no test may inherit one."""
    consolidate.reset_stop_cache()
    yield
    consolidate.reset_stop_cache()


# ------------------------------------------------------------- should_stop()


def test_the_stop_question_names_its_reason(monkeypatch):
    monkeypatch.setattr(consolidate, "_past_deadline", lambda: True)
    monkeypatch.setattr(consolidate, "_on_battery", lambda: False)
    assert consolidate.should_stop() == "deadline"

    consolidate.reset_stop_cache()
    monkeypatch.setattr(consolidate, "_past_deadline", lambda: False)
    monkeypatch.setattr(consolidate, "_on_battery", lambda: True)
    assert consolidate.should_stop() == "battery"

    consolidate.reset_stop_cache()
    monkeypatch.setattr(consolidate, "_on_battery", lambda: False)
    assert consolidate.should_stop() is None


def test_the_deadline_short_circuits_the_power_read(monkeypatch):
    """A per-item check must not open the sungrow DB thousands of times."""
    monkeypatch.setattr(consolidate, "_past_deadline", lambda: True)
    monkeypatch.setattr(
        consolidate, "_on_battery", lambda: pytest.fail("power read after the deadline fired")
    )
    assert consolidate.should_stop() == "deadline"


def test_the_verdict_is_cached_so_a_per_item_check_is_cheap(monkeypatch):
    calls: list[int] = []

    def counted() -> bool:
        calls.append(1)
        return False

    monkeypatch.setattr(consolidate, "_past_deadline", counted)
    monkeypatch.setattr(consolidate, "_on_battery", lambda: False)
    for _ in range(50):
        consolidate.should_stop()
    assert len(calls) == 1, "the guard was re-evaluated on every item"

    consolidate.reset_stop_cache()
    consolidate.should_stop()
    assert len(calls) == 2, "resetting the cache must re-ask"


def test_the_cache_expires(monkeypatch):
    """Cached forever would mean a run that goes on battery never notices."""
    monkeypatch.setattr(consolidate, "_STOP_CACHE_SECONDS", 0.0)
    monkeypatch.setattr(consolidate, "_past_deadline", lambda: False)
    monkeypatch.setattr(consolidate, "_on_battery", lambda: False)
    assert consolidate.should_stop() is None
    monkeypatch.setattr(consolidate, "_on_battery", lambda: True)
    assert consolidate.should_stop() == "battery"


# ------------------------------------------------- reach: the drain (step 0)


def test_the_guard_is_asked_BEFORE_the_first_cohort(monkeypatch):
    """A post-noon boot catch-up (timer is Persistent=true) must defer the whole
    drain to the next nightly, not start the 14B in the middle of the day."""
    monkeypatch.setattr(
        consolidate,
        "_drain_ingest_queue",
        lambda: pytest.fail("the drain started past its deadline"),
    )
    monkeypatch.setattr(consolidate.ensemble, "reset_shadow_counter", lambda: None)
    monkeypatch.setattr(consolidate.ingest_queue, "queue_depth", lambda **kw: 7)
    consolidate._STOP_LOG.clear()

    assert consolidate._drain_step({}, lambda: "deadline") == 0
    assert consolidate._STOP_LOG["queued_drained"] == {"reason": "deadline", "deferred": 7}


def test_an_unguarded_run_still_drains(monkeypatch):
    """An explicitly requested run is not wall-clock-bounded (stop=None)."""
    monkeypatch.setattr(consolidate, "_drain_ingest_queue", lambda: (3, 9))
    monkeypatch.setattr(consolidate.ensemble, "reset_shadow_counter", lambda: None)
    monkeypatch.setattr(consolidate.ensemble, "shadow_survivor_total", lambda: 0)
    counts: dict = {}
    assert consolidate._drain_step(counts, None) == 3
    assert counts["queued_items_saved"] == 9


def test_the_unattended_entry_point_arms_the_guard(monkeypatch):
    """kb-consolidate.service runs `python -m kbworker.consolidate` -> nightly()."""
    seen: dict = {}
    monkeypatch.setattr(consolidate, "main", lambda stop=None: seen.setdefault("stop", stop) or {})
    consolidate.nightly()
    assert seen["stop"] is consolidate.should_stop


# --------------------------------------------- reach: the LLM tail (step 2/3)


def test_triage_stands_down_and_leaves_its_rows_open(kb, monkeypatch):
    from tests.test_review_triage import _unverified

    rows = [_unverified("p", f"guard{i}") for i in range(4)]
    calls: list[int] = []

    def fake_triage(item, similar, usage_count):
        calls.append(item["id"])
        return {"action": "keep", "dup_of": None, "rationale": "ok"}

    monkeypatch.setattr(llm, "triage_extraction", fake_triage)
    monkeypatch.setattr(review_triage, "STOP_CHECK_EVERY", 2)

    # stop only once two rows are done, so the loop is proven to RUN then stop.
    assert review_triage.run_triage(50, should_stop=lambda: "battery" if calls else None) == 2
    assert len(calls) == 2

    remaining = [r["queue_id"] for r in review_triage.pending_triage(10)]
    assert remaining == [q for _i, q in rows[2:]], "deferred rows were not left open + untriaged"
    assert len(rows) == 4


def test_triage_without_a_guard_is_never_stopped(kb, monkeypatch):
    from tests.test_review_triage import _unverified

    [_unverified("p", f"free{i}") for i in range(3)]
    monkeypatch.setattr(
        llm,
        "triage_extraction",
        lambda item, similar, usage_count: {"action": "keep", "dup_of": None, "rationale": "r"},
    )
    assert review_triage.run_triage(50) == 3


def test_the_contradiction_loop_stands_down(monkeypatch):
    pairs = [(1, 2, 0.9), (3, 4, 0.9), (5, 6, 0.9)]
    monkeypatch.setattr(consolidate.consolidation, "contradiction_pairs", lambda *a, **k: pairs)
    monkeypatch.setattr(
        consolidate.consolidation,
        "item_pair_texts",
        lambda a, b: pytest.fail("a pair was judged past the deadline"),
    )
    consolidate._STOP_LOG.clear()

    assert consolidate._contradictions("p", lambda: "deadline") == 0
    assert consolidate._STOP_LOG["contradictions_queued"] == {"reason": "deadline", "deferred": 3}


def test_the_contradiction_loop_runs_to_the_end_without_a_guard(monkeypatch):
    pairs = [(1, 2, 0.9), (3, 4, 0.9)]
    monkeypatch.setattr(consolidate.consolidation, "contradiction_pairs", lambda *a, **k: pairs)
    monkeypatch.setattr(
        consolidate.consolidation, "item_pair_texts", lambda a, b: (("t", "b"),) * 2
    )
    monkeypatch.setattr(consolidate.llm, "items_contradict", lambda *a: False)
    assert consolidate._contradictions("p") == 0


def test_the_evidence_loop_stands_down(monkeypatch):
    rows = [(i, i + 1, i + 2, {}) for i in range(4)]
    monkeypatch.setattr(consolidate.evidence, "open_contradiction_rows", lambda: rows)
    monkeypatch.setattr(
        consolidate.evidence,
        "gather_evidence",
        lambda a, b: pytest.fail("evidence gathered past the deadline"),
    )
    consolidate._STOP_LOG.clear()

    assert consolidate._evidence(lambda: "battery") == (0, 0, 0)
    assert consolidate._STOP_LOG["evidence_refreshed"] == {"reason": "battery", "deferred": 4}


def test_the_summary_loop_stands_down(monkeypatch):
    monkeypatch.setattr(
        consolidate.consolidation, "tags_needing_summary", lambda since: [("p", "t", 5)] * 3
    )
    monkeypatch.setattr(
        consolidate.consolidation,
        "active_items_with_tag",
        lambda p, t: pytest.fail("a summary was written past the deadline"),
    )
    consolidate._STOP_LOG.clear()

    assert consolidate._summaries(None, lambda: "deadline") == 0
    assert consolidate._STOP_LOG["summaries_written"]["deferred"] == 3


def test_the_triage_share_guard_yields_to_the_run_guard_then_to_its_slice():
    """WAVE-2 reach: triage's INNER share of the tail reserve is a stand-down like
    every other tail loop's — the run's own deadline/battery STILL preempts inside
    the share, and the share itself stops triage once spent even while the run
    guard is quiet. Day-independent: the reserve is built by hand with a fixed
    deadline, never read from the calendar."""
    res = allocator.TailReserve(reserve_seconds=180 * 60.0, deadline=datetime(2026, 8, 3, 12, 0))
    clock = {"now": datetime(2026, 8, 3, 9, 30)}  # inside triage's 09:00-10:30 slice
    res._now = lambda: clock["now"]
    run = {"r": None}
    guard = res.share_guard(0.5, lambda: run["r"])

    assert guard() is None
    run["r"] = "battery"
    assert guard() == "battery", "the run's own guard must still preempt within the share"
    run["r"] = None
    clock["now"] = datetime(2026, 8, 3, 11, 0)  # past the 10:30 half-reserve cutoff
    assert guard() == allocator.TRIAGE_SHARE_REASON


def test_a_deferral_is_accumulated_across_projects():
    """contradictions run per project; two stops must not overwrite each other."""
    consolidate._STOP_LOG.clear()
    consolidate._note_stop("contradictions_queued", "deadline", 4)
    consolidate._note_stop("contradictions_queued", "deadline", 6)
    assert consolidate._STOP_LOG["contradictions_queued"]["deferred"] == 10


def test_the_run_report_records_why_it_stopped(kb, monkeypatch):
    """A run that did a third of its work must not look like a broken one."""
    monkeypatch.setattr(consolidate.ingest_queue, "queue_depth", lambda **kw: 11)
    monkeypatch.setattr(consolidate.eval_sentinel, "run_sentinel", lambda counts: None)

    result = consolidate.main(stop=lambda: "battery")
    assert result["skipped"] is False
    assert result["stopped"]["queued_drained"] == {"reason": "battery", "deferred": 11}
    assert result["queued_drained"] == 0


def test_an_unstopped_run_reports_an_empty_stop_log(kb, monkeypatch):
    monkeypatch.setattr(consolidate.eval_sentinel, "run_sentinel", lambda counts: None)
    assert consolidate.main()["stopped"] == {}


# --------------------------------------- the sungrow boundary (Inv. 7 carve-out)


@pytest.fixture()
def karachi():
    """Pin the process to the site's zone: the sungrow stamps are naive LOCAL,
    so a test that only passes in UTC would prove nothing."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Karachi"
    time.tzset()
    yield ZoneInfo("Asia/Karachi")
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


def _naive_local_power_db(tmp_path, zone, volts: float, age_seconds: int) -> str:
    """A stand-in sungrow DB that stores what sungrow really stores: a NAIVE
    local wall-clock string, with no offset anywhere in it."""
    stamp = (datetime.now(zone) - timedelta(seconds=age_seconds)).replace(tzinfo=None)
    assert "+" not in stamp.isoformat(), "the fixture leaked an offset"
    path = tmp_path / "sungrow.sqlite3"
    conn = sqlite3.connect(path)
    # Battery-inverter-only read (monitor_inverter.has_battery = 1, review #8 H2).
    conn.execute("CREATE TABLE monitor_inverter (id INTEGER PRIMARY KEY, has_battery BOOL)")
    conn.execute("INSERT INTO monitor_inverter VALUES (1, 1)")
    conn.execute(
        "CREATE TABLE monitor_inverterreading (grid_voltage REAL, timestamp TEXT, "
        "inverter_id INTEGER, raw_data TEXT)"
    )
    conn.execute(
        "INSERT INTO monitor_inverterreading VALUES (?,?,1,NULL)", (volts, stamp.isoformat())
    )
    conn.commit()
    conn.close()
    return str(path)


def test_a_fresh_naive_local_reading_is_fresh(karachi):
    """Read as UTC, 'now - seen' was about MINUS five hours here."""
    stamp = datetime.now(karachi).replace(tzinfo=None).isoformat()
    age = consolidate._reading_age_seconds(stamp)
    assert age is not None and 0 <= age < settings.kb_power_max_age_seconds, (
        f"a reading taken just now aged {age}s"
    )


def test_a_two_hour_old_naive_local_reading_is_stale(tmp_path, monkeypatch, caplog, karachi):
    """THE bug: within one UTC offset of staleness the check was neutered, so a
    dead poller's last value was trusted as the state of the grid right now. The
    freshness fix detects the staleness; the FAIL-SAFE fix then stands the drain
    down rather than assuming mains at the one moment the state is unknown."""
    monkeypatch.setattr(
        settings, "kb_power_db", _naive_local_power_db(tmp_path, karachi, 0.0, 7_200)
    )
    with caplog.at_level(logging.WARNING, logger="kbworker.consolidate"):
        assert consolidate._on_battery() is True, "a 2h-old reading must fail SAFE, not open"
    assert "STALE" in caplog.text


def test_a_stale_reading_fails_SAFE_on_battery_even_at_normal_voltage(
    tmp_path, monkeypatch, caplog, karachi
):
    """A configured poller that DIED right after a grid outage is the exact case
    the old fail-open handled wrong: the last reading shows a healthy 230V but is
    six hours old, and trusting it kept the GPU running into the outage. A stale
    reading now stands the drain down regardless of the last voltage it saw."""
    monkeypatch.setattr(
        settings, "kb_power_db", _naive_local_power_db(tmp_path, karachi, 230.0, 21_600)
    )
    with caplog.at_level(logging.WARNING, logger="kbworker.consolidate"):
        assert consolidate._on_battery() is True, (
            "a stale reading must be treated as ON BATTERY, not trusted as mains"
        )
    assert "STALE" in caplog.text and "sungrow poller may be down" in caplog.text


def test_a_missing_power_db_fails_OPEN():
    """Not configured on this box: the feature is off, so the drain must run — a
    pipeline that silently never runs is the worse failure when there is no UPS
    telemetry at all (distinct from 'configured but the poller died')."""
    from unittest.mock import patch

    with patch.object(settings, "kb_power_db", ""):
        assert consolidate._on_battery() is False, "an unconfigured power check must fail OPEN"


def test_an_unreadable_power_db_fails_OPEN(tmp_path, monkeypatch, caplog):
    """Configured but the file cannot be opened (missing/corrupt): still fail OPEN.
    Only a reading that IS present but STALE fails safe; an absent DB is the
    'not configured' case, not the 'poller died mid-outage' case."""
    monkeypatch.setattr(settings, "kb_power_db", str(tmp_path / "does-not-exist.sqlite3"))
    with caplog.at_level(logging.WARNING, logger="kbworker.consolidate"):
        assert consolidate._on_battery() is False, "an unreadable power DB must fail OPEN"
    assert "power check unavailable" in caplog.text


def test_a_fresh_naive_local_outage_still_pauses_the_drain(tmp_path, monkeypatch, karachi):
    monkeypatch.setattr(settings, "kb_power_db", _naive_local_power_db(tmp_path, karachi, 0.0, 30))
    assert consolidate._on_battery() is True, "the freshness fix must not blind the outage check"


def test_an_offset_carrying_stamp_is_still_honoured_as_written(karachi):
    """If sungrow ever starts emitting UTC, this boundary needs no change."""
    from datetime import UTC

    aware = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    age = consolidate._reading_age_seconds(aware)
    assert age is not None and 55 <= age <= 120


def test_an_unparseable_stamp_ignores_freshness_rather_than_crashing():
    assert consolidate._reading_age_seconds("not a timestamp") is None
    assert consolidate._reading_age_seconds(None) is None

"""Wave H.1 — instrument truth-telling (review #9a; KB 31857 follow-up).

Instruments only, no ranking / golden / power-constant change:

1. night keying on the run START with a NOON rollover (INS-1) and a window of
   whole nights (INS-6);
2. the weekly's 'expired' line as a flow total + nights at cap (INS-4), eval
   cells from the night's last fallback-free run with an ``fb`` column (INS-5)
   and fallback-free push series (EV-1);
3. the pause marker's ``'at'`` = the FIRST battery stand-down (INS-2), kept
   beside ``_STOP_LOG`` (never inside the per-step dicts), and the RESUME line
   keyed on the paused run's start;
4. the run-duration line counts steps that took time (INS-7);
5. detector hygiene: fallback-free baseline/anchor, a fallback-night trip is a
   labelled MEASUREMENT (EV-2);
6. the rerank sidecar warm-up before the tally is armed (EV-3);
7. per-case ranks / rerank outcomes keyed by query, for both replays (EV-4);
8. the power_rules docstring says what was measured.

Pure renderers are hermetic with a PINNED zone; the baseline/detector and the
whole-night weekly run use the real kb_test DB (conftest's stubs). The
ac_restore.sh resume-line pin lives beside its rig in tests/test_ac_restore.py.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kbcore import db, eval_baseline, eval_harness, rerank
from kbcore import review as review_mod
from kbcore.config import settings
from kbcore.db import connect
from kbcore.ingest import ingest
from kbworker import (
    consolidate,
    digest_lines,
    eval_cases,
    eval_composition,
    eval_regression,
    eval_sentinel,
    eval_trust,
    power_rules,
    powerguard,
    stop_log,
    weekly,
    weekly_instruments,
    weekly_render,
)
from kbworker.digest_lines import night_of
from tests.conftest import DB_UP

needs_db = pytest.mark.skipif(not DB_UP, reason="compose Postgres not reachable")
PKT = timezone(timedelta(hours=5))  # the deployment's zone, pinned (never the box's)
MISS_STATS = {"total": 0, "distinct": 0, "empty": 0, "thin": 0}
NOW = datetime(2026, 9, 6, 1, 4, tzinfo=PKT)  # the Sunday weekly instant; night_of == 09-05


@pytest.fixture(autouse=True)
def _fresh_state():
    """Rerank participation/tally are thread-local and the stop log is a shared
    module object: both outlive tests."""
    rerank.reset_outcome()
    rerank._participation.tally = None
    stop_log._STOP_LOG.clear()
    yield
    rerank.reset_outcome()
    rerank._participation.tally = None
    stop_log._STOP_LOG.clear()


def _rep(at: datetime, run_secs: float | None = None, **meta):
    if run_secs is not None:
        meta["run_secs"] = run_secs
    return (at, meta)


def _body(reports, now=NOW, **kw) -> list[str]:
    return weekly_render._report_body(
        now, reports, MISS_STATS, [], 0, 0, None, 0, [], tz=PKT, **kw
    ).splitlines()


# --- 1. night keying: run START, noon rollover, whole-night window ------------


def test_night_rolls_over_at_noon_so_a_morning_catch_up_joins_its_night():
    assert night_of(datetime(2026, 8, 29, 20, 30, tzinfo=UTC), PKT) == date(2026, 8, 29)
    assert night_of(datetime(2026, 8, 30, 5, 0, tzinfo=UTC), PKT) == date(2026, 8, 29), "10:00"
    assert night_of(datetime(2026, 8, 30, 1, 24, tzinfo=UTC), PKT) == date(2026, 8, 29), "06:24"
    assert night_of(datetime(2026, 8, 30, 6, 59), PKT) == date(2026, 8, 29), "11:59 local"
    assert night_of(datetime(2026, 8, 30, 7, 0), PKT) == date(2026, 8, 30), "noon opens the night"
    assert digest_lines.NIGHT_ROLLOVER_HOUR == 12


def test_nights_key_on_the_run_start_with_a_finish_fallback_for_pre_h7_reports():
    """The Sep 5 shape: the 01:00 nightly overrunning to 06:57, its 09:32
    resume, then the next 01:00 nightly — two rows, not three (INS-1)."""
    nightly = _rep(datetime(2026, 9, 5, 1, 57, tzinfo=UTC), run_secs=21420, queued_drained=18)
    resume = _rep(datetime(2026, 9, 5, 5, 0, tzinfo=UTC), run_secs=1680, queued_drained=1)
    nxt = _rep(datetime(2026, 9, 5, 21, 0, tzinfo=UTC), run_secs=3600, queued_drained=2)
    grouped = weekly_instruments.nights([nightly, resume, nxt], PKT)
    assert [(n, len(r)) for n, r in grouped] == [(date(2026, 9, 4), 2), (date(2026, 9, 5), 1)]
    rows = weekly_instruments.night_rows([nightly, resume, nxt], PKT)
    assert rows[0].startswith("2026-09-04   (2)  ") and rows[0].split()[2] == "19"
    # A run FINISHING at 12:30 local: run_secs keys it on its 10:30 start; a
    # pre-H7 report (no run_secs) keys on its finish — the only honest fallback.
    late = datetime(2026, 9, 5, 7, 30, tzinfo=UTC)
    assert weekly_instruments.run_start(late, {"run_secs": 7200}) == late - timedelta(seconds=7200)
    assert weekly_instruments.run_start(late, {}) == late
    assert weekly_instruments.run_start(late, {"run_secs": 0}) == late
    assert weekly_instruments.run_start(late, {"run_secs": "x"}) == late
    assert [n for n, _ in weekly_instruments.nights([_rep(late, run_secs=7200)], PKT)] == [
        date(2026, 9, 4)
    ]
    assert [n for n, _ in weekly_instruments.nights([_rep(late)], PKT)] == [date(2026, 9, 5)]


def test_window_is_seven_whole_nights_ending_tonights_and_the_header_says_so():
    base = datetime(2026, 8, 28, 20, 30, tzinfo=UTC)  # nine 01:00-local nightlies, 08-28 .. 09-05
    reports = [_rep(base + timedelta(days=i), run_secs=1800, queued_drained=i) for i in range(9)]
    kept = weekly_instruments.window_reports(
        reports, NOW, PKT, nights_back=weekly_render.WINDOW_DAYS
    )
    # Exact tiling (review #9a INS-6 fix): the window ends at the last COMPLETED
    # night — the weekly fires INSIDE Sunday's night, whose later runs roll
    # into next week instead of vanishing — so tonight (09-05) is out too.
    assert kept == reports[1:8], "08-28 (too old) and 09-05 (in progress) drop; order kept"
    assert [night_of(weekly_instruments.run_start(a, m), PKT) for a, m in kept] == [
        date(2026, 8, 29) + timedelta(days=i) for i in range(7)
    ]
    lines = _body(kept)
    assert "window: 7 nights 2026-08-29..2026-09-04; nights reported: 7 (7 runs)" in lines
    assert weekly_instruments.window_reports([], NOW, PKT) == []
    # A report from a night AFTER night_of(now) (not yet a whole night) is excluded too.
    future = _rep(datetime(2026, 9, 6, 8, 0, tzinfo=UTC), queued_drained=1)  # 13:00 local Sep 6
    assert weekly_instruments.window_reports([future], NOW, PKT) == []
    # And next week's window starts exactly where this one ended (nights_through).
    nxt = weekly_instruments.window_reports(
        reports, NOW + timedelta(days=7), PKT, start_after=date(2026, 9, 4)
    )
    assert [night_of(weekly_instruments.run_start(a, m), PKT) for a, m in nxt][:1] == [
        date(2026, 9, 5)
    ], "the in-progress night is reported by NEXT week's weekly"


# --- 2. the weekly's expired line, eval cells, fb column, clean push series -------


def test_expired_stocks_line_is_the_window_total_plus_nights_at_cap():
    reports = [
        _rep(datetime(2026, 8, 30, 20, 30, tzinfo=UTC), review_expired=500),  # night 08-30
        _rep(datetime(2026, 8, 31, 20, 30, tzinfo=UTC), review_expired=500),  # 08-31, paused
        _rep(datetime(2026, 9, 1, 3, 0, tzinfo=UTC), review_expired=500),  # its 08:00 resume
        _rep(datetime(2026, 9, 1, 20, 30, tzinfo=UTC), review_expired=291),  # 09-01
        _rep(datetime(2026, 9, 2, 20, 30, tzinfo=UTC), queued_drained=0),  # 09-02: never expired
    ]
    assert (
        weekly_instruments.expired_line(reports, PKT, 500) == "1,791 total; 2 night(s) at cap 500"
    )
    assert weekly_instruments.expired_line(reports, PKT, None) == "1,791 total"
    assert weekly_instruments.expired_line([reports[-1]], PKT, 500) == "n/a"
    lines = _body(reports, expire_cap=500)
    assert "  expired (window):        1,791 total; 2 night(s) at cap 500" in lines
    assert not any("expired/night" in ln for ln in lines)
    # The Sep 6 weekly's shape: the LAST report is a paused fragment that expired
    # nothing — the old first -> last line read '500 -> 0' under 'stocks'.
    assert weekly_instruments.expired_line(reports[:2] + [reports[-1]], PKT, 500).startswith(
        "1,000 total; 2 night(s) at cap"
    )


def test_eval_cells_come_from_the_nights_last_fallback_free_run_and_fb_counts_the_night():
    stalled = _rep(
        datetime(2026, 9, 5, 1, 57, tzinfo=UTC),
        run_secs=21420,
        eval_hit_at_5=0.9808,
        eval_mrr=0.9103,
        eval_rerank_tally={"active": 29, "fallback": 22, "off": 1},
    )
    clean = _rep(
        datetime(2026, 9, 5, 5, 0, tzinfo=UTC),
        run_secs=1680,
        eval_hit_at_5=0.9808,
        eval_mrr=0.9269,
        eval_rerank_tally={"active": 51, "fallback": 0, "off": 1},
    )
    fragment = _rep(datetime(2026, 9, 5, 7, 0, tzinfo=UTC), run_secs=266, queued_drained=0)
    rows = weekly_instruments.night_rows([stalled, clean, fragment], PKT)
    assert len(rows) == 1, "the 266 s fragment starting 11:55 local is the same night"
    cells = rows[0].split()
    assert cells[6:8] == ["0.981", "0.927"], (
        "the last fallback==0 run WITH an mrr, not the last run"
    )
    assert cells[-1] == "22", "fb = the night's fallback count, whichever run the cells came from"
    only_stalled = weekly_instruments.night_rows([stalled], PKT)[0].split()
    assert only_stalled[6:8] == ["0.981", "0.910"] and only_stalled[-1] == "22", (
        "no clean run: the stalled numbers render, marked"
    )
    untallied = _rep(datetime(2026, 8, 20, 20, 30, tzinfo=UTC), eval_mrr=0.93, eval_hit_at_5=1.0)
    assert weekly_instruments.night_rows([untallied], PKT)[0].split()[-1] == "-", "no tally, no fb"
    lines = _body([stalled, clean, fragment])
    header = next(ln for ln in lines if ln.startswith("night "))
    assert header.split()[-1] == "fb" and weekly_instruments.FB_NOTE in lines
    # The push's PRIMARY series read fallback-free reports only (EV-1); the
    # composition lines are not filtered (clean by construction).
    high = _rep(
        datetime(2026, 9, 4, 0, 45, tzinfo=UTC),
        eval_hit_at_5=1.0,
        eval_mrr=0.9423,
        eval_top5_unverified_share=0.5,
        eval_rerank_tally={"active": 36, "fallback": 15, "off": 1},
    )
    push = weekly_render._push_lines(
        NOW, [high, stalled, clean], MISS_STATS, [], 0, None, 1, tz=PKT
    )
    assert "eval mrr: 0.927" in push and "eval hit@5: 0.981" in push
    assert "top5 unverified: 0.500" in push
    assert weekly_instruments.clean_reports([high, stalled, clean, untallied]) == [clean, untallied]


# --- 3. the stand-down instant beside _STOP_LOG, the marker and the RESUME line ----


def test_resume_line_keys_the_night_on_the_paused_runs_start_and_shows_the_stand_down():
    prior = {
        "at": "2026-09-04T23:12:55+00:00",  # 04:12 local: the first ON BATTERY
        "deferred": 71,
        "started": "2026-09-04T20:00:00+00:00",  # the 01:00 nightly
        "recorded_at": "2026-09-05T01:57:16+00:00",  # 06:57: what 'at' used to be
    }
    line = digest_lines._resume_line({"resumed_from_pause": prior}, tz=PKT)
    assert line == "RESUME of 2026-09-04 (paused 04:12 local, 71 deferred)"
    after_dawn = {**prior, "at": "2026-09-05T01:24:01+00:00"}
    assert digest_lines._resume_line({"resumed_from_pause": after_dawn}, tz=PKT) == (
        "RESUME of 2026-09-04 (paused 06:24 local, 71 deferred)"
    ), "a stand-down after 06:00 still belongs to the night the run started"
    legacy = {"at": "2026-09-05T01:57:16+00:00", "deferred": 71}  # a pre-H.1 marker
    assert digest_lines._resume_line({"resumed_from_pause": legacy}, tz=PKT) == (
        "RESUME of 2026-09-04 (paused 06:57 local, 71 deferred)"
    ), "no 'started': the night comes from 'at' under the noon rule"
    unparseable = {"at": "x", "started": "2026-09-04T20:00:00+00:00"}
    assert digest_lines._resume_line({"resumed_from_pause": unparseable}, tz=PKT) is None
    bad_start = {**prior, "started": "not-a-time"}
    assert digest_lines._resume_line({"resumed_from_pause": bad_start}, tz=PKT) == line


def test_stop_log_stamps_the_run_start_and_the_first_battery_stand_down_only():
    before = datetime.now(UTC)
    consolidate._STOP_LOG.clear()
    assert consolidate._STOP_LOG is stop_log._STOP_LOG, "ONE shared object through the seam"
    assert before <= stop_log.run_started_at() <= datetime.now(UTC)
    assert stop_log.battery_stand_down_at() is None
    consolidate._note_stop("queued_drained", "deadline", 4)
    assert stop_log.battery_stand_down_at() is None, "a deadline cut is not the grid's business"
    consolidate._note_stop("contradictions_queued", "battery", 13)
    first = stop_log.battery_stand_down_at()
    assert first is not None and first.tzinfo is not None
    consolidate._note_stop("evidence_refreshed", "battery", 20)
    consolidate._note_stop("contradictions_queued", "battery", 2)
    assert stop_log.battery_stand_down_at() == first, "later stand-downs keep the FIRST instant"
    assert consolidate._STOP_LOG == {
        "queued_drained": {"reason": "deadline", "deferred": 4},
        "contradictions_queued": {"reason": "battery", "deferred": 15},
        "evidence_refreshed": {"reason": "battery", "deferred": 20},
    }, "the per-step dicts are untouched (never a stamp inside them)"
    payload = stop_log.pause_payload(33)
    assert payload["at"] == first.isoformat() and payload["deferred"] == 33
    assert payload["started"] == stop_log.run_started_at().isoformat()
    assert datetime.fromisoformat(payload["recorded_at"]) >= first
    consolidate._STOP_LOG.clear()
    assert stop_log.battery_stand_down_at() is None and consolidate._STOP_LOG == {}
    bare = stop_log.pause_payload(1)
    assert "started" not in bare and datetime.fromisoformat(bare["at"]) >= first, (
        "no stand-down noted this run: 'at' falls back to now, and no 'started'"
    )


def test_record_battery_pause_writes_the_stand_down_instant_not_the_write_time(tmp_path):
    consolidate._STOP_LOG.clear()
    consolidate._note_stop("queued_drained", "battery", 41)
    stood_down = stop_log.battery_stand_down_at()
    marker = tmp_path / "battery-pause.json"
    counts = {"stopped": {k: dict(v) for k, v in consolidate._STOP_LOG.items()}}
    powerguard.record_battery_pause(counts, marker=marker)
    data = json.loads(marker.read_text())
    assert data["at"] == stood_down.isoformat() and data["deferred"] == 41
    assert datetime.fromisoformat(data["recorded_at"]) >= stood_down
    assert data["started"] == stop_log.run_started_at().isoformat()


@needs_db
def test_mains_marker_and_resume_line_carry_the_stand_down_instant(kb, monkeypatch, tmp_path):
    monkeypatch.setattr(consolidate.ingest_queue, "queue_depth", lambda *a, **k: 41)
    monkeypatch.setattr(consolidate.eval_sentinel, "run_sentinel", lambda counts: None)
    state_dir = tmp_path / "state"
    monkeypatch.setattr(settings, "kb_state_dir", state_dir)
    paused = consolidate.main(stop=lambda: "battery")
    data = json.loads((state_dir / "battery-pause.json").read_text())
    started, at, recorded = (
        datetime.fromisoformat(data[k]) for k in ("started", "at", "recorded_at")
    )
    assert started <= at <= recorded
    assert paused["resumed_from_pause"] is None and paused["stopped"]
    resumed = consolidate.main()
    assert resumed["resumed_from_pause"]["at"] == data["at"]
    assert resumed["resumed_from_pause"]["started"] == data["started"]
    line = digest_lines._resume_line(resumed, tz=PKT)
    assert line.startswith(
        f"RESUME of {night_of(started, PKT).isoformat()} "
        f"(paused {at.astimezone(PKT).strftime('%H:%M')} local, "
    )


# --- 5. detector hygiene: clean baseline, measurement trips ------------------------


def _report(**meta) -> int:
    return ingest(
        "kb-system",
        "report",
        "Consolidation report",
        "Nightly consolidation report\n\n- test",
        kind="chat",
        uri=f"report:{uuid.uuid4()}",
        trust="verified",
        meta={"consolidation": True, **meta},
    )


@needs_db
def test_clean_baseline_skips_fallback_reports_but_not_untallied_ones(kb, caplog):
    caplog.set_level(logging.INFO, logger="kbcore.eval_baseline")
    old = _report(
        eval_status="ok",
        eval_hit_at_5=0.98,
        eval_mrr=0.9269,
        eval_rerank_tally={"active": 51, "fallback": 0, "off": 1},
    )
    _report(
        eval_status="ok",
        eval_hit_at_5=0.98,
        eval_mrr=0.9423,
        eval_rerank_tally={"active": 36, "fallback": 15, "off": 1},
    )
    _report(eval_status="error")
    prior = eval_baseline.previous_eval_metrics()
    assert prior == {"hit_at_5": 0.98, "mrr": 0.9269, "report_id": old, "skipped_fallback": 1}
    assert "skipping report" in caplog.text and "rerank fallback 15" in caplog.text
    legacy = _report(eval_status="ok", eval_hit_at_5=0.97, eval_mrr=0.93)  # pre-H7a: no tally
    assert eval_baseline.previous_eval_metrics()["report_id"] == legacy, "no tally = eligible"


@needs_db
def test_only_fallback_reports_means_no_baseline_yet(kb):
    _report(
        eval_status="ok",
        eval_hit_at_5=0.9,
        eval_mrr=0.9,
        eval_rerank_tally={"active": 1, "fallback": 2, "off": 0},
    )
    assert eval_baseline.previous_eval_metrics() is None


def test_rerank_fallbacks_reads_the_tally_defensively():
    assert eval_baseline.rerank_fallbacks({"eval_rerank_tally": {"fallback": 3}}) == 3
    assert eval_baseline.rerank_fallbacks({"eval_rerank_tally": {"fallback": True}}) == 0
    assert eval_baseline.rerank_fallbacks({"eval_rerank_tally": {"fallback": -1}}) == 0
    assert eval_baseline.rerank_fallbacks({"eval_rerank_tally": "x"}) == 0
    assert eval_baseline.rerank_fallbacks({}) == 0 and eval_baseline.rerank_fallbacks(None) == 0


@needs_db
def test_a_trip_on_a_fallback_night_is_a_measurement_not_a_regression(kb, caplog):
    _report(
        eval_status="ok",
        eval_hit_at_5=0.95,
        eval_mrr=0.93,
        eval_rerank_tally={"active": 52, "fallback": 0, "off": 0},
    )
    counts = {
        "eval_regression": False,
        "eval_rerank_tally": {"active": 37, "fallback": 15, "off": 0},
    }
    with caplog.at_level(logging.WARNING, logger="kbworker.eval_sentinel"):
        eval_sentinel._detect_regression(counts, 0.70, 0.60)  # below the floor AND > epsilon down
    assert counts["eval_regression"] is False, "not a red night"
    assert counts["eval_regression_measurement"] == {
        "rerank_fallback": 15,
        "hit_at_5": 0.7,
        "mrr": 0.6,
        "below_floor": True,
        "dropped": True,
    }
    assert "measurement (rerank fallback 15)" in caplog.text
    assert "REGRESSION:" not in caplog.text
    assert review_mod.open_review_by_reason(eval_regression.REVIEW_REASON) is None, "no row"
    # The same numbers on a fallback-free night are a regression, as ever, and
    # the anchor is the CLEAN prior.
    clean = {"eval_regression": False, "eval_rerank_tally": {"active": 52, "fallback": 0, "off": 0}}
    eval_sentinel._detect_regression(clean, 0.70, 0.60)
    assert clean["eval_regression"] is True and "eval_regression_measurement" not in clean
    _qid, detail = review_mod.open_review_by_reason(eval_regression.REVIEW_REASON)
    assert detail["baseline_mrr"] == 0.93 and detail["baseline_hit_at_5"] == 0.95


@needs_db
def test_a_fallback_night_never_auto_resolves_an_open_regression_row(kb, caplog):
    review_mod.queue_review(
        None, eval_regression.REVIEW_REASON, {"baseline_hit_at_5": 0.9, "baseline_mrr": 0.9}
    )
    stalled = {
        "eval_regression": False,
        "eval_rerank_tally": {"active": 30, "fallback": 22, "off": 0},
    }
    with caplog.at_level(logging.INFO, logger="kbworker.eval_sentinel"):
        eval_sentinel._detect_regression(stalled, 0.95, 0.95)
    assert review_mod.open_review_by_reason(eval_regression.REVIEW_REASON) is not None
    assert "cannot prove recovery" in caplog.text
    clean = {"eval_regression": False, "eval_rerank_tally": {"active": 52, "fallback": 0, "off": 0}}
    eval_sentinel._detect_regression(clean, 0.95, 0.95)
    assert review_mod.open_review_by_reason(eval_regression.REVIEW_REASON) is None


# --- 6. the sidecar warm-up ----------------------------------------------------------


def test_warm_up_probes_until_two_fast_answers_and_is_bounded(monkeypatch):
    monkeypatch.setattr(settings, "kb_rerank_url", "http://sidecar.test")
    answers = iter(
        [(True, 900), (True, 400), (True, 200), (False, None), (True, 100), (True, 120), (True, 50)]
    )
    seen: list = []

    def fake_probe():
        ok, ms = next(answers)
        seen.append(ms)
        return {"ok": ok, "latency_ms": ms}

    monkeypatch.setattr(rerank, "probe", fake_probe)
    out = rerank.warm_up()
    assert out["ok"] is True and out["calls"] == 6, "the streak restarts after a slow/failed probe"
    assert seen == [900, 400, 200, None, 100, 120]
    assert isinstance(out["secs"], float)
    monkeypatch.setattr(rerank, "probe", lambda: {"ok": False, "latency_ms": None})
    capped = rerank.warm_up(max_calls=5)
    assert (capped["calls"], capped["ok"]) == (5, False), "a dead sidecar costs max_calls probes"
    clock = iter(range(0, 100_000, 25))
    monkeypatch.setattr(rerank, "time", SimpleNamespace(monotonic=lambda: float(next(clock))))
    timed = rerank.warm_up(max_secs=60.0)
    assert timed == {"calls": 2, "secs": 100.0, "ok": False}, "the wall-clock cap stops the loop"


def test_warm_up_without_a_sidecar_makes_no_call(monkeypatch):
    monkeypatch.setattr(settings, "kb_rerank_url", "")
    monkeypatch.setattr(rerank, "probe", lambda: pytest.fail("no HTTP without a URL"))
    assert rerank.warm_up() == {"calls": 0, "secs": 0.0, "ok": False}
    assert settings.kb_rerank_timeout == 2.0, (
        "the warm-up rides on probe(); the timeout is untouched"
    )


def _quiet(monkeypatch) -> None:
    monkeypatch.setattr(eval_sentinel, "TRUST_METRIC_ENABLED", False)
    monkeypatch.setattr(eval_sentinel, "_detect_regression", lambda counts, h, m: None)


def _golden(monkeypatch, cases) -> None:
    monkeypatch.setattr(eval_sentinel.eval_harness, "load_golden", lambda golden_path=None: cases)


def test_sentinel_warms_the_sidecar_before_arming_the_tally_and_fails_open(monkeypatch):
    _quiet(monkeypatch)
    _golden(monkeypatch, [{"q": "q0", "expect_substr": ["x"]}, {"q": "q1", "expect_substr": ["x"]}])
    monkeypatch.setattr(settings, "kb_rerank_url", "http://sidecar.test")
    order: list[str] = []

    def fake_warm_up():
        order.append("warm")
        rerank._note("fallback")  # a warm-up probe outcome must never reach the tally
        return {"calls": 3, "secs": 1.2, "ok": True}

    real_reset = rerank.reset_tally

    def spy_reset():
        order.append("arm")
        real_reset()

    monkeypatch.setattr(rerank, "warm_up", fake_warm_up)
    monkeypatch.setattr(rerank, "reset_tally", spy_reset)
    monkeypatch.setattr(
        eval_sentinel.eval_harness, "evaluate", lambda golden=None, golden_path=None: ([], 1.0, 0.9)
    )
    counts: dict = {}
    eval_sentinel.run_sentinel(counts)
    assert order == ["warm", "arm"]
    assert counts["eval_rerank_warmup"] == {"calls": 3, "secs": 1.2, "ok": True}
    assert counts["eval_rerank_tally"] == {"active": 0, "fallback": 0, "off": 2}
    # No sidecar configured: no key, no call.
    monkeypatch.setattr(settings, "kb_rerank_url", "")
    monkeypatch.setattr(rerank, "warm_up", lambda: pytest.fail("must not warm without a sidecar"))
    counts = {}
    eval_sentinel.run_sentinel(counts)
    assert counts["eval_status"] == "ok" and "eval_rerank_warmup" not in counts
    # A broken warm-up costs its key only; the tally is still armed.
    monkeypatch.setattr(settings, "kb_rerank_url", "http://sidecar.test")

    def boom():
        raise RuntimeError("sidecar exploded")

    monkeypatch.setattr(rerank, "warm_up", boom)
    counts = {}
    eval_sentinel.run_sentinel(counts)
    assert counts["eval_status"] == "ok" and counts["eval_mrr"] == 0.9
    assert "eval_rerank_warmup" not in counts
    assert counts["eval_rerank_tally"] == {"active": 0, "fallback": 0, "off": 2}


# --- 7. per-case telemetry keyed by query -----------------------------------------


def _case(query, rank, rerank_outcome=None, chain=False):
    kw = {"rerank": rerank_outcome} if rerank_outcome is not None else {}
    return eval_harness.CaseResult(query, rank, rank is not None, [], chain, **kw)


def test_per_case_keys_are_keyed_by_query_with_duplicates_suffixed():
    results = [
        _case("drv_spi_write", 1, "active"),
        _case("drv_spi_write", None, "fallback"),
        _case("drv_spi_write", 2, "off"),
        _case("wireguard", 5),  # rerank defaults to 'off'
        SimpleNamespace(query="odd", rank=True, rerank="bogus"),
        ("bare", 1),
    ]
    keys = eval_cases.per_case_keys(results)
    assert keys == {
        "eval_case_ranks": {
            "drv_spi_write": 1,
            "drv_spi_write #2": None,
            "drv_spi_write #3": 2,
            "wireguard": 5,
            "odd": None,
        },
        "eval_case_rerank": {
            "drv_spi_write": "active",
            "drv_spi_write #2": "fallback",
            "drv_spi_write #3": "off",
            "wireguard": "off",
            "odd": "off",
        },
    }
    assert eval_cases.rerank_tally(keys["eval_case_rerank"]) == {
        "active": 1,
        "fallback": 1,
        "off": 3,
    }
    assert eval_cases.per_case_keys([("q", 1)]) == {"eval_case_ranks": {}, "eval_case_rerank": {}}
    assert all(k.isascii() for k in keys["eval_case_ranks"])


def test_sentinel_persists_the_primary_replays_per_case_keys(monkeypatch):
    _quiet(monkeypatch)
    _golden(monkeypatch, [{"q": "a", "expect_substr": ["x"]}, {"q": "b", "expect_substr": ["x"]}])
    results = [_case("a", 1, "active"), _case("b", None, "fallback")]
    monkeypatch.setattr(
        eval_sentinel.eval_harness,
        "evaluate",
        lambda golden=None, golden_path=None: (results, 0.5, 0.5),
    )
    counts: dict = {}
    eval_sentinel.run_sentinel(counts)
    assert counts["eval_case_ranks"] == {"a": 1, "b": None}
    assert counts["eval_case_rerank"] == {"a": "active", "b": "fallback"}
    monkeypatch.setattr(  # the stubbed rig's bare tuples cost nothing
        eval_sentinel.eval_harness,
        "evaluate",
        lambda golden=None, golden_path=None: ([("a", 1)], 1.0, 1.0),
    )
    counts = {}
    eval_sentinel.run_sentinel(counts)
    assert counts["eval_status"] == "ok"
    assert counts["eval_case_ranks"] == {} and counts["eval_case_rerank"] == {}


def _stub_hit(item_id: int, title: str):
    return SimpleNamespace(
        item_id=item_id, title=title, snippet="", type="note", trust="unverified"
    )


def test_composition_replay_records_its_own_per_case_rerank_and_tally(monkeypatch):
    def fake_search(q, k, tool):
        rerank._note("active" if q == "a" else "fallback", 0.5)
        return [_stub_hit(1, "x")]

    monkeypatch.setattr(eval_sentinel, "search", fake_search)
    monkeypatch.setattr(eval_trust, "verified_lift", lambda share, n: {})
    golden = [
        {"q": "a", "expect_substr": ["x"]},
        {"q": "b", "expect_substr": ["x"]},
        {"q": "a", "expect_substr": ["y"]},
    ]
    result = eval_composition._trust_composition(golden)
    assert result["eval_case_rerank_composition"] == {
        "a": "active",
        "b": "fallback",
        "a #2": "active",
    }
    assert result["eval_rerank_tally_composition"] == {"active": 2, "fallback": 1, "off": 0}
    assert result["eval_top5_results"] == 3, "the shares are untouched"


def test_harness_records_the_rerank_outcome_search_noted_for_each_case(monkeypatch):
    def fallback_search(*a, **k):
        rerank._note("fallback")
        return []

    monkeypatch.setattr(eval_harness, "search", fallback_search)
    results, _h, _m = eval_harness.evaluate(golden=[{"q": "anything", "forbid_item_id": 1}])
    assert results[0].rerank == "fallback" and results[0].rank is None and results[0].passed
    monkeypatch.setattr(eval_harness, "search", lambda *a, **k: (rerank.reset_outcome(), [])[1])
    results, _h, _m = eval_harness.evaluate(golden=[{"q": "anything", "forbid_item_id": 1}])
    assert results[0].rerank == "off"


@needs_db
def test_harness_reads_off_end_to_end_without_a_sidecar(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_rerank_url", "")
    ingest("p", "gotcha", "SPI polarity fix", "The DRV8706 SPI polarity gotcha.", uri="g1")
    golden = [{"q": "DRV8706 SPI polarity", "expect_substr": ["DRV8706"]}]
    results, hit, _m = eval_harness.evaluate(golden=golden)
    assert hit == 1.0 and results[0].rerank == "off", "search() noted 'off': the leg did not run"


# --- 8. the weekly run trims to whole nights (DB) + the power_rules docstring ----


@needs_db
def test_the_weekly_run_trims_to_whole_nights(kb, monkeypatch):
    monkeypatch.setattr(weekly.notify, "push_digest", lambda lines, **kw: True)
    tonight = night_of(datetime.now(UTC))  # the box's zone, exactly like weekly._run

    def _local(night: date, hour: int) -> datetime:  # an aware LOCAL instant on that date
        return datetime(night.year, night.month, night.day, hour).astimezone()

    _report(queued_drained=7)  # created now: tonight — IN PROGRESS, rolls to next week
    edge = _report(queued_drained=100)  # an afternoon run six nights back
    out = _report(queued_drained=1000)  # seven nights back: the window's FIRST night
    with db.transaction() as conn:
        conn.execute(
            "UPDATE items SET created_at = %s WHERE id = %s",
            (_local(tonight - timedelta(days=6), 13), edge),
        )
        conn.execute(
            "UPDATE items SET created_at = %s WHERE id = %s",
            (_local(tonight - timedelta(days=7), 13), out),
        )
    assert len(weekly._nightly_reports(weekly.WINDOW_DAYS + 1)) == 3, "the fetch is window + 1"
    counts: dict = {}
    assert weekly._run(counts) is True
    with connect() as conn:
        body, meta = conn.execute(
            "SELECT body, meta FROM items WHERE id = %s", (counts["weekly_report_item_id"],)
        ).fetchone()
    assert meta["nights"] == 2 and meta["local_nights"] == 2
    lines = body.splitlines()
    first, last = tonight - timedelta(days=7), tonight - timedelta(days=1)
    assert (
        f"window: 7 nights {first.isoformat()}..{last.isoformat()}; nights reported: 2 (2 runs)"
        in lines
    )
    assert meta["nights_through"] == last.isoformat(), "next week tiles after this night"
    rows = [ln for ln in lines if ln[:4].isdigit()]
    assert [ln.split()[2] for ln in rows] == ["1000", "100"], "tonight has no row yet"
    assert "expired (window):        n/a" in body


def test_power_rules_docstring_carries_the_measured_distribution_and_no_constant_moved():
    doc = power_rules.__doc__ or ""
    assert "Real off-grid runs are 32-282 rows long" not in doc
    assert "no overlap in the measured history" not in doc, "the first cut's claim is retracted"
    assert "23 off-grid-signature runs" in doc and "15 of them only 3-13 rows" in doc
    assert "12 one/two-row grid-tied matches" in doc and "ONE row (2 vs 3)" in doc
    assert "not confirmed outages" in doc and "running_state" in doc
    assert power_rules.OFFGRID_CONSECUTIVE_ROWS == 3, "a docstring edit moves no constant"
    assert settings.kb_power_window == 5 or isinstance(settings.kb_power_window, int)

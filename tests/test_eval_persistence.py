"""The E2 per-case PERSISTENCE trip (review #9 KB 40402, amended at review #10).

A golden case is PERSISTENT when it failed on the 3 most recent consecutive
same-epoch fallback-free NIGHTS including tonight AND passed on at least one
earlier such night. Nights are the noon-rollover night of the run's START, a
night's LAST fallback-free run counts, fallback runs neither count nor reset a
streak, other epochs are ignored, and a case that never passed can never arm.
Output names cases by golden INDEX and satisfier id only. Any persistent case
trips ``eval_regression`` and blocks the auto-resolve until it passes; a pass
after a red streak is a first-pass NOTICE.

The fold is hermetic over synthetic nights (zone pinned to PKT); the history
read and the detector run on the real kb_test DB with report items whose
``created_at`` is set explicitly. Every query text is SYNTHETIC.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from kbcore import db, eval_baseline, eval_epoch, eval_harness
from kbcore import review as review_mod
from kbcore.ingest import ingest
from kbworker import digest, eval_persistence, eval_regression, eval_sentinel, run_report
from tests.conftest import DB_UP

needs_db = pytest.mark.skipif(not DB_UP, reason="compose Postgres not reachable")
PKT = timezone(timedelta(hours=5))  # the deployment's zone, pinned (never the box's)
E2 = "e2e2e2e2e2e2"
E1 = "e1e1e1e1e1e1"
A, B = "alpha prose case for the trip", "beta prose case for the trip"  # synthetic
SAT_A, SAT_B = 11, 21


def at(day: int, hour: int = 20) -> datetime:
    """A run finishing at ``hour`` UTC on Nov ``day`` — 20:00 UTC is 01:00 PKT, night ``day``."""
    return datetime(2026, 11, day, hour, 0, tzinfo=UTC)


def verdicts(a: bool, b: bool = True, sat_a: int | None = SAT_A) -> dict:
    """One run's per-case keys, as kbworker.eval_cases.replay_keys mints them."""
    return {
        "eval_case_passed": {A: a, B: b},
        "eval_case_satisfier": {A: sat_a if a else None, B: SAT_B if b else None},
    }


def trips(history: list, tonight: dict, day: int) -> dict:
    return eval_persistence.case_trips(tonight, history, now=at(day), tz=PKT)


# --- 1. the fold (hermetic) ----------------------------------------------------------


def test_fires_on_the_third_failing_night_not_the_second():
    history = [(at(1), verdicts(True)), (at(2), verdicts(False))]
    assert trips(history, verdicts(False), 3)["eval_case_persistent"] == [], "2 red nights"
    history.append((at(3), verdicts(False)))
    out = trips(history, verdicts(False), 4)
    assert out["eval_case_persistent"] == [
        {"case": 1, "satisfier": SAT_A, "since": "2026-11-02", "nights": 3}
    ], "the satisfier of the LAST passing night; since = the streak's first red night"
    assert out["eval_case_first_pass"] == []
    history.append((at(4), verdicts(False)))
    assert trips(history, verdicts(False), 5)["eval_case_persistent"][0]["nights"] == 4


def test_never_fires_for_a_case_that_never_passed():
    history = [(at(d), verdicts(False)) for d in range(1, 6)]
    out = trips(history, verdicts(False), 6)
    assert out == {"eval_case_persistent": [], "eval_case_first_pass": []}, "a known-red is free"


def test_first_pass_after_a_red_streak_is_a_notice_even_for_a_never_passed_case():
    history = [(at(d), verdicts(False)) for d in range(1, 4)]
    out = trips(history, verdicts(True, sat_a=12), 4)
    assert out["eval_case_first_pass"] == [{"case": 1, "satisfier": 12}]
    assert out["eval_case_persistent"] == []
    short = [(at(1), verdicts(True)), (at(2), verdicts(False)), (at(3), verdicts(False))]
    assert trips(short, verdicts(True), 4)["eval_case_first_pass"] == [], "2 red nights: no notice"


def test_several_runs_in_one_night_the_last_clean_run_counts():
    # Night 11-02 ran twice (01:00 and 04:00 local): red, then green -> green.
    history = [
        (at(1), verdicts(True)),
        (at(2), verdicts(False)),
        (at(2, 23), verdicts(True, sat_a=13)),
        (at(3), verdicts(False)),
        (at(4), verdicts(False)),
    ]
    out = trips(history, verdicts(False), 5)["eval_case_persistent"]
    assert out == [{"case": 1, "satisfier": 13, "since": "2026-11-03", "nights": 3}], (
        "night 11-02 is green, and its satisfier is the LATER run's"
    )
    # ... and green-then-red files the night red: the streak starts a night earlier.
    flipped = [
        (at(1), verdicts(True)),
        (at(2), verdicts(True)),
        (at(2, 23), verdicts(False)),
        (at(3), verdicts(False)),
    ]
    out = trips(flipped, verdicts(False), 4)["eval_case_persistent"]
    assert out == [{"case": 1, "satisfier": SAT_A, "since": "2026-11-02", "nights": 3}]


def test_tonight_replaces_an_earlier_run_of_its_own_night():
    # A resume run tonight (04:00 local) after a red 01:00 run of the SAME night.
    history = [(at(1), verdicts(True)), (at(2), verdicts(False)), (at(3), verdicts(False))]
    series = eval_persistence.night_series(history, at(3, 23), verdicts(True), PKT)
    assert [n.isoformat() for n, _m in series] == ["2026-11-01", "2026-11-02", "2026-11-03"]
    assert series[-1][1]["eval_case_passed"][A] is True, "tonight is the night's last run"
    assert trips(history, verdicts(False), 3)["eval_case_persistent"] == []


def test_nights_key_on_the_run_start_and_tonight_on_its_step_ledger():
    # Finished 12:30 local Nov 3 (past the noon rollover) but STARTED 06:30: night 11-02.
    late = (datetime(2026, 11, 3, 7, 30, tzinfo=UTC), {**verdicts(False), "run_secs": 6 * 3600})
    series = eval_persistence.night_series([late], at(3), verdicts(False), PKT)
    assert [n.isoformat() for n, _m in series] == ["2026-11-02", "2026-11-03"]
    counts = {"step_secs": {"queued_drained": 3000.0, "summaries_written": 600, "flag": True}}
    now = datetime(2026, 11, 4, 7, 30, tzinfo=UTC)
    assert eval_persistence.tonight_start(counts, now) == now - timedelta(seconds=3600)
    assert eval_persistence.tonight_start({}, now) == now


def test_output_is_golden_index_and_satisfier_only():
    history = [(at(1), verdicts(True, True)), (at(2), verdicts(False, False))]
    history.append((at(3), verdicts(False, False)))
    out = trips(history, verdicts(False, False), 4)
    assert [p["case"] for p in out["eval_case_persistent"]] == [1, 2], "golden order, 1-based"
    for entry in out["eval_case_persistent"]:
        assert set(entry) == {"case", "satisfier", "since", "nights"}
        assert all(isinstance(entry[k], int) for k in ("case", "satisfier", "nights"))
        datetime.fromisoformat(entry["since"])
    rendered = json.dumps(out)
    assert A not in rendered and B not in rendered and "prose" not in rendered
    assert eval_persistence.case_trips({"eval_case_passed": {}}, history) == {}
    assert eval_persistence.case_trips({}, history) == {}


# --- 2. the history read (kb_test) ---------------------------------------------------


def _report(when: datetime, golden_hash: str = E2, fallback: int = 0, **meta) -> int:
    rid = ingest(
        "kb-system",
        "report",
        "Consolidation report",
        "Nightly consolidation report\n\n- test",
        kind="chat",
        uri=f"report:{uuid.uuid4()}",
        trust="verified",
        meta={
            "consolidation": True,
            "eval_status": "ok",
            "eval_hit_at_5": 0.95,
            "eval_mrr": 0.85,
            "eval_golden_hash": golden_hash,
            "eval_rerank_tally": {"active": 2, "fallback": fallback, "off": 0},
            **meta,
        },
    )
    with db.transaction() as conn:  # test setup only: place the report on its night
        conn.execute("UPDATE items SET created_at = %s WHERE id = %s", (when, rid))
    return rid


@needs_db
def test_case_history_is_same_epoch_fallback_free_verdicts_oldest_first(kb):
    _report(at(1), **verdicts(True))
    _report(at(2), fallback=3, **verdicts(True))  # a fallback run: never history
    _report(at(3), golden_hash=E1, **verdicts(True))  # another epoch
    _report(at(4))  # pre-E2 shape: no per-case verdicts
    _report(at(5), eval_status="error", **verdicts(True))
    newest = _report(at(6), **verdicts(False))
    history = eval_baseline.case_history(E2)
    assert [h[0] for h in history] == [at(1), at(6)], "oldest first, the rest filtered"
    assert set(history[-1][1]) == set(eval_baseline._HISTORY_KEYS), "meta is projected"
    assert history[-1][1]["eval_case_passed"] == {A: False, B: True}
    assert eval_baseline.case_history(None) == [], "an untracked run has no epoch history"
    assert newest > 0


@needs_db
def test_fallback_nights_are_skipped_not_reset_and_other_epochs_ignored(kb):
    _report(at(1), **verdicts(True))
    _report(at(2), **verdicts(False))
    _report(at(3), fallback=4, **verdicts(True))  # a fallback PASS must not reset the streak
    _report(at(4), **verdicts(False))
    history = eval_baseline.case_history(E2)
    out = trips(history, verdicts(False), 5)["eval_case_persistent"]
    assert out == [{"case": 1, "satisfier": SAT_A, "since": "2026-11-02", "nights": 3}]


@needs_db
def test_a_fallback_red_night_does_not_count_and_another_epochs_pass_never_arms(kb):
    _report(at(1), golden_hash=E1, **verdicts(True))  # passed — but in ANOTHER epoch
    _report(at(2), **verdicts(False))
    _report(at(3), fallback=2, **verdicts(False))  # a fallback red: not a night
    _report(at(4), **verdicts(False))
    out = trips(eval_baseline.case_history(E2), verdicts(False), 5)
    assert out["eval_case_persistent"] == [], "never passed in THIS epoch: cannot arm"
    _report(at(5), **verdicts(True))
    _report(at(6), **verdicts(False))
    _report(at(7), fallback=1, **verdicts(False))
    out = trips(eval_baseline.case_history(E2), verdicts(False), 8)
    assert out["eval_case_persistent"] == [], "night 7 fell back: only 2 clean red nights"


# --- 3. the detector: the trip, the row, the blocked auto-resolve --------------------


def _tonight(day: int, a_passes: bool, fallback: int = 0, sat_a: int = SAT_A) -> dict:
    return {
        "eval_golden_hash": E2,
        "eval_hit_at_5": 0.95,
        "eval_mrr": 0.85,
        "eval_regression": False,
        "eval_rerank_tally": {"active": 2, "fallback": fallback, "off": 0},
        **verdicts(a_passes, sat_a=sat_a),
    }


def _night(
    monkeypatch, day: int, a_passes: bool, fallback: int = 0, sat_a: int = SAT_A, hour: int = 20
) -> dict:
    """Run tonight's detector on Nov ``day`` (``hour`` UTC: 20 is 01:00 PKT, 23 is 04:00
    PKT — both night ``day``), then persist tonight's report as the run does — the
    verdicts AND the trip's own lists (sticky arming reads them back)."""
    monkeypatch.setattr(eval_persistence, "_now", lambda: at(day, hour))
    counts = _tonight(day, a_passes, fallback, sat_a)
    eval_sentinel._detect_regression(counts, 0.95, 0.85)
    trip_keys = ("eval_case_persistent", "eval_case_first_pass")
    _report(
        at(day, hour),
        fallback=fallback,
        **verdicts(a_passes, sat_a=sat_a),
        **{k: counts[k] for k in trip_keys if k in counts},
    )
    return counts


def _open_row():
    return review_mod.open_review_by_reason(eval_regression.REVIEW_REASON)


@needs_db
def test_the_trip_sets_eval_regression_and_blocks_auto_resolve_until_the_case_passes(
    kb, monkeypatch
):
    monkeypatch.setattr(eval_persistence, "NIGHT_TZ", PKT)
    _night(monkeypatch, 1, True)
    _night(monkeypatch, 2, False)
    second = _night(monkeypatch, 3, False)
    assert second["eval_regression"] is False and second["eval_case_persistent"] == []
    third = _night(monkeypatch, 4, False)  # the 3rd red night: the metrics alone are green
    assert third["eval_regression"] is True
    assert third["eval_regression_cause"] == ["persistent"]
    assert third["eval_case_persistent"] == [
        {"case": 1, "satisfier": SAT_A, "since": "2026-11-02", "nights": 3}
    ]
    qid, detail = _open_row()
    assert detail["golden_hash"] == E2 and detail["cause"] == ["persistent"]
    assert detail["persistent_cases"] == third["eval_case_persistent"]
    assert (detail["baseline_hit_at_5"], detail["baseline_mrr"]) == (0.95, 0.85)
    lines = digest.digest_lines(third)
    assert lines[0] == "EVAL RED: hit@5 0.95 (floor 0.75), MRR 0.85 (floor 0.5) - persistent"
    assert lines[1] == "EVAL CASE #1 (11): red 3 nights since 2026-11-02"
    assert all(A not in ln and B not in ln for ln in lines) and all(ln.isascii() for ln in lines)
    # Night 5: still red, metrics AT the anchor — the persistent case blocks the resolve.
    fourth = _night(monkeypatch, 5, False)
    assert fourth["eval_regression"] is True and fourth["eval_case_persistent"][0]["nights"] == 4
    assert _open_row()[0] == qid, "never rewritten, never auto-resolved while persistent"
    # Night 6: the case passes again — the row self-heals and the first pass is a notice.
    healed = _night(monkeypatch, 6, True, sat_a=12)
    assert healed["eval_regression"] is False and _open_row() is None
    assert healed["eval_case_first_pass"] == [{"case": 1, "satisfier": 12}]
    notice = digest.digest_lines(healed)
    assert "eval case #1 (12): first pass after 3+ red nights" in notice
    assert not any(ln.startswith(("EVAL RED", "EVAL CASE")) for ln in notice)


@needs_db
def test_a_fallback_run_tonight_evaluates_no_persistence(kb, monkeypatch):
    monkeypatch.setattr(eval_persistence, "NIGHT_TZ", PKT)
    for day, passes in ((1, True), (2, False), (3, False)):
        _report(at(day), **verdicts(passes))
    monkeypatch.setattr(eval_persistence, "_now", lambda: at(4))
    stalled = _tonight(4, a_passes=False, fallback=5)
    eval_sentinel._detect_regression(stalled, 0.95, 0.85)
    assert "eval_case_persistent" not in stalled and "eval_case_first_pass" not in stalled
    assert stalled["eval_regression"] is False and _open_row() is None
    clean = _tonight(4, a_passes=False)
    eval_sentinel._detect_regression(clean, 0.95, 0.85)
    assert clean["eval_regression"] is True, "the same night, fallback-free, trips"


def _boom(golden_hash, lookback=None):
    raise RuntimeError("history unavailable")


@needs_db
def test_a_broken_history_read_costs_the_trip_not_the_floors(kb, monkeypatch):
    monkeypatch.setattr(eval_baseline, "case_history", _boom)
    counts = _tonight(4, a_passes=False)
    eval_sentinel._detect_regression(counts, 0.70, 0.85)  # under the 0.75 floor
    assert counts["eval_regression"] is True and counts["eval_regression_cause"] == [
        "floor_hit_at_5"
    ]
    assert "eval_case_persistent" not in counts and "eval_case_first_pass" not in counts
    assert counts["eval_persistence_status"] == "error", "UNKNOWN, recorded as a scalar"
    qid, detail = _open_row()  # an unknown night still opens the floor's row
    assert detail["cause"] == ["floor_hit_at_5"] and "persistent_cases" not in detail
    assert qid > 0


# --- 4. review #10 fix round: tri-state, sticky arming, the eligible window ----------


@needs_db
def test_an_unknown_persistence_night_neither_trips_nor_auto_resolves(kb, monkeypatch, caplog):
    """R2 inverted: a read error used to self-heal the row and mint a second next night."""
    monkeypatch.setattr(eval_persistence, "NIGHT_TZ", PKT)
    for day, passes in ((1, True), (2, False), (3, False)):
        _night(monkeypatch, day, passes)
    assert _night(monkeypatch, 4, False)["eval_regression"] is True
    qid = _open_row()[0]
    real = eval_baseline.case_history
    monkeypatch.setattr(eval_baseline, "case_history", _boom)
    with caplog.at_level(logging.WARNING, logger="kbworker.eval_sentinel"):
        unknown = _night(monkeypatch, 5, False)  # still red, metrics AT the stored anchor
    assert unknown["eval_regression"] is False and unknown["eval_persistence_status"] == "error"
    assert "eval_case_persistent" not in unknown and "eval_case_first_pass" not in unknown
    assert _open_row()[0] == qid, "an unknown night cannot prove recovery: the row stays open"
    warned = [r for r in caplog.records if "persistence UNKNOWN" in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING
    monkeypatch.setattr(eval_baseline, "case_history", real)
    back = _night(monkeypatch, 6, False)
    assert back["eval_regression"] is True and _open_row()[0] == qid, "no second row"
    assert back["eval_case_persistent"] == [
        {"case": 1, "satisfier": SAT_A, "since": "2026-11-02", "nights": 5}
    ], "the unknown night's red verdict still counts"


@needs_db
def test_lost_verdicts_make_tonight_unknown_not_recovered(kb):
    """A replay-telemetry error that cost eval_case_passed is UNKNOWN, never 'no case'."""
    qid = review_mod.queue_review(
        None,
        eval_regression.REVIEW_REASON,
        {"baseline_hit_at_5": 0.9, "baseline_mrr": 0.8, "golden_hash": E2, "cause": ["persistent"]},
    )
    lost = {"eval_golden_hash": E2, "eval_replay_telemetry_status": "error"}
    eval_sentinel._detect_regression(lost, 0.95, 0.85)  # above the stored anchor
    assert lost["eval_persistence_status"] == "error" and _open_row()[0] == qid
    plain = {"eval_golden_hash": E2}  # no verdicts and no error: persistence does not apply
    eval_sentinel._detect_regression(plain, 0.95, 0.85)
    assert "eval_persistence_status" not in plain and _open_row() is None, "auto-resolved"


@needs_db
def test_a_streak_longer_than_the_lookback_stays_persistent(kb, monkeypatch):
    """F1: the window holds 3 eligible reports; the case passed on night 1 and is red
    nights 2..9 — it stays persistent (sticky), the row stays open, then heals."""
    monkeypatch.setattr(eval_persistence, "NIGHT_TZ", PKT)
    monkeypatch.setattr(eval_baseline, "CASE_HISTORY_LOOKBACK", 3)
    _night(monkeypatch, 1, True)
    for day in (2, 3):
        assert _night(monkeypatch, day, False)["eval_regression"] is False
    qid = None
    for day in range(4, 10):
        if day >= 5:  # TA2-2: the premise — the patched window really lost the night-1 pass
            window = eval_baseline.case_history(E2)
            assert len(window) == 3 and not any(m["eval_case_passed"][A] for _t, m in window)
        counts = _night(monkeypatch, day, False)
        assert counts["eval_regression"] is True and counts["eval_regression_cause"] == [
            "persistent"
        ], day
        assert counts["eval_case_persistent"] == [
            {"case": 1, "satisfier": SAT_A, "since": "2026-11-02", "nights": day - 1}
        ], day
        qid = qid or _open_row()[0]
        assert _open_row()[0] == qid, "never rewritten, never auto-resolved"
    healed = _night(monkeypatch, 10, True, sat_a=12)
    assert healed["eval_case_first_pass"] == [{"case": 1, "satisfier": 12}]
    assert healed["eval_regression"] is False and _open_row() is None, "the pass heals it"


@needs_db
def test_case_history_limit_counts_only_eligible_reports(kb):
    """F1a: the eligibility filters run in SQL, so LIMIT counts eligible reports only."""
    _report(at(1), **verdicts(True))
    _report(at(2), golden_hash=E1, **verdicts(False))  # another epoch
    _report(at(3), fallback=2, **verdicts(False))  # a fallback run
    _report(at(4))  # no per-case verdicts (pre-E2)
    _report(at(5), eval_status="error", **verdicts(False))  # not a completed eval
    weekly = ingest(  # the NEWEST: a non-eval kb-system report (a weekly / deep-search save)
        "kb-system", "report", "Weekly report", "weekly", kind="chat", uri=f"w:{uuid.uuid4()}"
    )
    with db.transaction() as conn:  # test setup only: place it on night 6
        conn.execute("UPDATE items SET created_at = %s WHERE id = %s", (at(6), weekly))
    assert [h[0] for h in eval_baseline.case_history(E2, lookback=1)] == [at(1)]
    # rerank_fallbacks' rule, mirrored in SQL: only a JSON integer > 0 is a fallback.
    for day, fb in ((7, 0), (8, 2.5), (9, "3"), (10, True)):
        _report(at(day), fallback=fb, **verdicts(True))
    _report(at(11), **verdicts(True), eval_rerank_tally=[1, 2])  # malformed tally: eligible
    history = eval_baseline.case_history(E2)
    assert [h[0] for h in history] == [at(d) for d in (1, 7, 8, 9, 10, 11)]
    assert all(eval_baseline.rerank_fallbacks(m) == 0 for _at, m in history)
    assert eval_baseline.case_history(E2, lookback=2) == history[-2:], "newest eligible two"


def _carry(night: int, *entries: dict) -> dict:
    """``carried()``'s shape: ``{case: (its listing's night, entry)}``."""
    return {int(e["case"]): (datetime(2026, 11, night).date(), e) for e in entries}


def test_sticky_arming_carries_a_streak_past_the_window():
    entry = {"case": 1, "satisfier": 7, "since": "2026-10-01", "nights": 30}
    red2 = [(at(2), verdicts(False))]  # the window: night 2 red only (the pass fell out)
    series = eval_persistence.night_series(red2, at(3), verdicts(False), PKT)
    out = eval_persistence.fold(series, _carry(2, entry))
    assert out["eval_case_persistent"] == [
        {"case": 1, "satisfier": 7, "since": "2026-10-01", "nights": 31}
    ], "the carried since/satisfier; nights = carried + the red nights after"
    assert eval_persistence.fold(series)["eval_case_persistent"] == [], "the window alone: lost"
    # Carried from night 1 with night 2 red in between: + 2 nights.
    out = eval_persistence.fold(series, _carry(1, entry))
    assert out["eval_case_persistent"][0]["nights"] == 32
    # A pass after the carrying report ends it — the window decides.
    green2 = eval_persistence.night_series([(at(2), verdicts(True))], at(3), verdicts(False), PKT)
    assert eval_persistence.fold(green2, _carry(1, entry))["eval_case_persistent"] == []
    # Green tonight: the carried streak's first pass is a notice.
    healed = eval_persistence.night_series(red2, at(3), verdicts(True, sat_a=9), PKT)
    out = eval_persistence.fold(healed, _carry(2, entry))
    assert out == {
        "eval_case_persistent": [],
        "eval_case_first_pass": [{"case": 1, "satisfier": 9}],
    }
    # A malformed listing (a non-whole case or nights) is no listing — it never replaces
    # a valid one — and an index past the golden set carries nothing.
    junk = [{"case": "1", "nights": 3}, {"case": 1.5, "nights": 3}, {"case": 1}]
    past = {"case": 9, "nights": 3}
    listed = [(at(2), {**verdicts(False), "eval_case_persistent": [entry, *junk, past]})]
    carry = eval_persistence.carried(listed, at(3).date(), PKT)
    assert carry == {**_carry(2, entry), **_carry(2, past)}
    assert eval_persistence.fold(series, carry)["eval_case_persistent"] == [{**entry, "nights": 31}]


def test_the_window_entry_wins_and_a_later_same_night_pass_blocks_the_carry():
    stale = {"case": 1, "satisfier": 99, "since": "2026-10-01", "nights": 3}
    history = [(at(1), verdicts(True)), (at(2), verdicts(False)), (at(3), verdicts(False))]
    series = eval_persistence.night_series(history, at(4), verdicts(False), PKT)
    out = eval_persistence.fold(series, _carry(3, stale))
    assert out["eval_case_persistent"] == [
        {"case": 1, "satisfier": SAT_A, "since": "2026-11-02", "nights": 3}
    ], "the pass is in the window: its exact entry wins over the carried one"
    # Night 3 ran twice: the 01:00 run carried the case, the 04:00 run passed it.
    twice = [(at(3), {**verdicts(False), "eval_case_persistent": [stale]})]
    twice.append((at(3, 23), verdicts(True)))
    series = eval_persistence.night_series(twice, at(4), verdicts(False), PKT)
    carry = eval_persistence.carried(twice, series[-1][0], PKT)
    assert carry == _carry(3, stale), "the case's newest listing: the 01:00 run"
    assert eval_persistence.fold(series, carry)["eval_case_persistent"] == [], "night 3 is green"


def test_case_trips_reads_the_carried_entry_back_from_history():
    entry = {"case": 2, "satisfier": SAT_B, "since": "2026-10-20", "nights": 12}
    history = [(at(1), {**verdicts(True, False), "eval_case_persistent": [entry]})]
    history.append((at(2), verdicts(True, False)))  # an UNKNOWN night: no trip key, still red
    out = trips(history, verdicts(True, False), 3)
    assert out["eval_case_persistent"] == [{**entry, "nights": 14}]
    assert eval_persistence.carried(history, at(3).date(), PKT) == _carry(1, entry)


def test_the_case_index_is_golden_order_not_sorted_keys():
    """TA-9: the alert's index is the golden POSITION (Z before Y here), never key order."""
    z, y = "zulu prose case for the probe", "yankee prose case for the probe"

    def v(z_ok: bool, y_ok: bool) -> dict:
        return {
            "eval_case_passed": {z: z_ok, y: y_ok},
            "eval_case_satisfier": {z: 1, y: 2 if y_ok else None},
        }

    history = [(at(1), v(True, True)), (at(2), v(True, False)), (at(3), v(True, False))]
    out = eval_persistence.case_trips(v(True, False), history, now=at(4), tz=PKT)
    assert [p["case"] for p in out["eval_case_persistent"]] == [2]


def test_a_history_night_after_tonight_is_dropped():
    """Clock skew: a report filed on a LATER night never stands in for tonight."""
    history = [(at(1), verdicts(True)), (at(5), verdicts(True))]
    series = eval_persistence.night_series(history, at(3), verdicts(False), PKT)
    assert [n.isoformat() for n, _m in series] == ["2026-11-01", "2026-11-03"]
    assert series[-1][1]["eval_case_passed"][A] is False, "tonight is tonight's run"


def test_tonight_starts_at_the_stamped_run_start_when_present():
    now = datetime(2026, 11, 4, 7, 30, tzinfo=UTC)
    ledger = {"step_secs": {"queued_drained": 99_999.0}}
    stamped = {**ledger, "run_started_at": "2026-11-04T06:55:00+00:00"}
    assert eval_persistence.tonight_start(stamped, now) == datetime(2026, 11, 4, 6, 55, tzinfo=UTC)
    local = {"run_started_at": "2026-11-04T11:55:00+05:00"}  # any offset: read as UTC
    assert eval_persistence.tonight_start(local, now) == datetime(2026, 11, 4, 6, 55, tzinfo=UTC)
    naive = {"run_started_at": "2026-11-04T06:55:00"}  # a naive stamp is UTC (Invariant 7)
    assert eval_persistence.tonight_start(naive, now) == datetime(2026, 11, 4, 6, 55, tzinfo=UTC)
    bad = {**ledger, "run_started_at": "last night"}
    assert eval_persistence.tonight_start(bad, now) == now - timedelta(seconds=99_999.0)


@needs_db
def test_history_nights_key_on_the_run_start(kb):
    """TA-10: case_history projects run_secs, so a past-noon FINISH files under its START."""
    _report(datetime(2026, 11, 3, 7, 30, tzinfo=UTC), run_secs=6 * 3600, **verdicts(False))
    history = eval_baseline.case_history(E2)
    assert history[-1][1]["run_secs"] == 6 * 3600
    series = eval_persistence.night_series(history, at(3), verdicts(False), PKT)
    assert [n.isoformat() for n, _m in series] == ["2026-11-02", "2026-11-03"]


@needs_db
def test_persistence_and_a_floor_trip_together(kb, monkeypatch):
    """F6 (ii): both causes on one night, and the row carries the persistent cases."""
    monkeypatch.setattr(eval_persistence, "NIGHT_TZ", PKT)
    for day, passes in ((1, True), (2, False), (3, False)):
        _report(at(day), **verdicts(passes))
    monkeypatch.setattr(eval_persistence, "_now", lambda: at(4))
    counts = _tonight(4, a_passes=False)
    eval_sentinel._detect_regression(counts, 0.70, 0.85)
    assert counts["eval_regression_cause"] == ["drop_hit_at_5", "floor_hit_at_5", "persistent"]
    _qid, detail = _open_row()
    assert detail["persistent_cases"] == counts["eval_case_persistent"] != []
    assert detail["cause"] == counts["eval_regression_cause"]


# --- 5. fix-round review D1: the carry is chosen per CASE (its newest listing) ---------


def _night_date(day: int):
    return datetime(2026, 11, day).date()


def test_a_newer_report_that_omits_a_case_never_drops_it():
    """D1: a green 01:00 flap wrote an EMPTY list, but tonight's red 04:00 run replaces it
    (the night rule) — so it cannot drop the case: the carry is PER CASE, the case's
    newest listing (night 8), and a case red for longer than the window stays persistent
    exactly as the window path (pass still in view) says."""
    entry = {"case": 1, "satisfier": SAT_A, "since": "2026-11-02", "nights": 7}
    flap = (at(9), {**verdicts(True, sat_a=12), "eval_case_persistent": []})  # 01:00 PKT
    history = [
        (at(7), {**verdicts(False), "eval_case_persistent": [{**entry, "nights": 6}]}),
        (at(8), {**verdicts(False), "eval_case_persistent": [entry]}),
        flap,
    ]
    assert eval_persistence.carried(history, _night_date(9), PKT) == _carry(8, entry)
    late = eval_persistence.case_trips(verdicts(False), history, now=at(9, 23), tz=PKT)
    assert late["eval_case_persistent"] == [{**entry, "nights": 8}], "night 9's LAST run is red"
    full = [(at(1), verdicts(True)), *((at(d), verdicts(False)) for d in range(2, 9)), flap]
    in_view = eval_persistence.case_trips(verdicts(False), full, now=at(9, 23), tz=PKT)
    assert in_view["eval_case_persistent"] == late["eval_case_persistent"], "window-size free"
    # An UNKNOWN run (no list) omits it too: night 8's LAST run (04:00, red) decides
    # whether night 8's earlier listing still counts, and every red night is counted.
    unknown = [*history[:2], (at(8, 23), verdicts(False))]
    assert eval_persistence.carried(unknown, _night_date(9), PKT) == _carry(8, entry)
    assert trips(unknown, verdicts(False), 9)["eval_case_persistent"] == [{**entry, "nights": 8}]
    green8 = [*history[:2], (at(8, 23), verdicts(True))]  # ... and a green last run ends it
    assert trips(green8, verdicts(False), 9)["eval_case_persistent"] == []


def test_a_listing_from_tonights_own_night_counts_that_night_once():
    """TA2-5: an earlier run TONIGHT armed the case on its 3rd red night (nights 3); the
    window lost the pre-streak pass. Tonight's later run red: still 3 nights (the review
    backtest's lookback-3 episode); green: only 2 PRIOR red nights, so no notice."""
    entry = {"case": 1, "satisfier": SAT_A, "since": "2026-11-01", "nights": 3}
    armed = [(at(1), verdicts(False)), (at(2), verdicts(False))]
    armed.append((at(3), {**verdicts(False), "eval_case_persistent": [entry]}))  # 01:00 run
    assert eval_persistence.carried(armed, _night_date(3), PKT) == _carry(3, entry)
    red = eval_persistence.case_trips(verdicts(False), armed, now=at(3, 23), tz=PKT)
    assert red == {"eval_case_persistent": [entry], "eval_case_first_pass": []}
    green = eval_persistence.case_trips(verdicts(True), armed, now=at(3, 23), tz=PKT)
    assert green == {"eval_case_persistent": [], "eval_case_first_pass": []}
    longer = {**entry, "nights": 4}  # one more prior red night: now the pass is a notice
    out = eval_persistence.fold(
        eval_persistence.night_series(armed, at(3, 23), verdicts(True), PKT), _carry(3, longer)
    )
    assert out["eval_case_first_pass"] == [{"case": 1, "satisfier": SAT_A}]


def test_a_listing_filed_after_tonights_night_never_carries():
    """TA2-7: clock skew — a report on a LATER night is no listing, and a later-night
    carry handed to the fold directly is ignored."""
    entry = {"case": 1, "satisfier": SAT_A, "since": "2026-10-01", "nights": 9}
    skewed = [
        (at(1), verdicts(False)),
        (at(6), {**verdicts(False), "eval_case_persistent": [entry]}),
    ]
    assert eval_persistence.carried(skewed, _night_date(3), PKT) == {}
    series = eval_persistence.night_series(skewed[:1], at(3), verdicts(False), PKT)
    assert eval_persistence.fold(series, _carry(6, entry))["eval_case_persistent"] == []


@needs_db
def test_a_same_night_flap_never_disarms_a_streak_longer_than_the_window(kb, monkeypatch):
    """D1 end to end (R4 inverted; window patched to 3 reports): pass on night 1, red
    2..8; night 9 flaps green at 01:00 (a notice; the row auto-resolves) and red at
    04:00, so night 9 files RED — the late run re-arms from the carried streak, a fresh
    row opens, and nights 10 and 11 keep counting."""
    monkeypatch.setattr(eval_persistence, "NIGHT_TZ", PKT)
    monkeypatch.setattr(eval_baseline, "CASE_HISTORY_LOOKBACK", 3)
    _night(monkeypatch, 1, True)
    for day in range(2, 9):
        _night(monkeypatch, day, False)
    early = _night(monkeypatch, 9, True, sat_a=12)  # 01:00 PKT
    assert early["eval_case_first_pass"] == [{"case": 1, "satisfier": 12}] and _open_row() is None
    window = eval_baseline.case_history(E2)
    assert [h[0] for h in window] == [at(7), at(8), at(9)], "premise: the night-1 pass is out"
    late = _night(monkeypatch, 9, False, hour=23)  # 04:00 PKT, the SAME night: red
    assert late["eval_case_persistent"] == [
        {"case": 1, "satisfier": SAT_A, "since": "2026-11-02", "nights": 8}
    ]
    assert late["eval_regression"] is True and _open_row() is not None
    for day, nights in ((10, 9), (11, 10)):
        assert _night(monkeypatch, day, False)["eval_case_persistent"][0]["nights"] == nights


def test_the_sticky_and_window_union_comes_out_in_golden_order():
    """TA2-3: case 1 armed by the window, case 2 only by the carry — both lists read
    [1, 2] (the digest's 5-line cap shows the FIRST cases in golden order)."""
    history = [(at(1), verdicts(True, False)), (at(2), verdicts(False, False))]
    history.append((at(3), verdicts(False, False)))
    b_entry = {"case": 2, "satisfier": SAT_B, "since": "2026-10-01", "nights": 30}
    red = eval_persistence.night_series(history, at(4), verdicts(False, False), PKT)
    out = eval_persistence.fold(red, _carry(3, b_entry))
    assert [p["case"] for p in out["eval_case_persistent"]] == [1, 2]
    reds = [(at(d), verdicts(False, False)) for d in (1, 2, 3)]
    green = eval_persistence.night_series(reds, at(4), verdicts(True, True), PKT)
    out = eval_persistence.fold(green, _carry(3, b_entry))
    assert [p["case"] for p in out["eval_case_first_pass"]] == [1, 2]


@needs_db
def test_the_real_report_writer_persists_what_sticky_arming_reads_back(kb, monkeypatch):
    """TA2-2: the trip's own lists round-trip through the PRODUCTION path — run_sentinel
    -> _detect_regression -> run_report._write_report -> case_history — so a window of 3
    reports still carries a streak the window lost (the _night helper builds reports by
    hand). Synthetic queries; the epoch key is stubbed."""
    q1, q2 = "synthetic sticky probe case one", "synthetic sticky probe case two"
    monkeypatch.setattr(eval_persistence, "NIGHT_TZ", PKT)
    monkeypatch.setattr(eval_epoch, "golden_hash", lambda path=None: E2)
    monkeypatch.setattr(eval_baseline, "CASE_HISTORY_LOOKBACK", 3)
    monkeypatch.setattr(eval_sentinel, "TRUST_METRIC_ENABLED", False)
    golden = [{"q": q1}, {"q": q2}]
    monkeypatch.setattr(eval_harness, "load_golden", lambda golden_path=None: golden)

    def night(n: int, one_passes: bool) -> dict:
        start = at(1) + timedelta(days=n)  # 01:00 PKT, night Nov 1 + n
        results = [
            eval_harness.CaseResult(q1, 1 if one_passes else None, one_passes, top_ids=[11, 12]),
            eval_harness.CaseResult(q2, 1, True, top_ids=[21, 22]),
        ]
        hit = sum(r.passed for r in results) / 2
        monkeypatch.setattr(
            eval_harness, "evaluate", lambda golden=None, golden_path=None: (results, hit, hit)
        )
        counts: dict = {"run_started_at": start.isoformat(), "run_secs": 600.0}
        eval_sentinel.run_sentinel(counts)
        rid = run_report._write_report(counts)
        with db.transaction() as conn:  # test setup only: the report's finish instant
            conn.execute(
                "UPDATE items SET created_at = %s WHERE id = %s",
                (start + timedelta(minutes=10), rid),
            )
        return counts

    night(0, True)
    seen = [[p["nights"] for p in night(n, False)["eval_case_persistent"]] for n in range(1, 8)]
    assert seen == [[], [], [3], [4], [5], [6], [7]], "carried past a 3-report window"
    history = eval_baseline.case_history(E2)
    assert len(history) == 3 and history[-1][1]["eval_case_persistent"][0]["nights"] == 7
    assert _open_row() is not None

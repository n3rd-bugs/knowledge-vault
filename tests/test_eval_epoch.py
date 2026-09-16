"""Eval EPOCH plumbing (E1, KB 31857 review #8 / KB 32942 review #9a).

golden.yaml changes land only at a review boundary and "re-anchor the floor
and reset the WoW baseline in the same commit". The epoch is the golden
file's content hash (kbcore.eval_epoch): every eval report records it, the
clean-baseline read (kbcore.eval_baseline) and the detector
(kbworker.eval_regression) compare only within it, the digest names the first
night of a new epoch, and the weekly trend never draws one line through two
golden sets (kbworker.eval_epoch_lines).

Real kb_test DB for the baseline/detector halves (seeded report items, as the
Wave H1 baseline tests do); the renderers are hermetic with a PINNED zone.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from kbcore import eval_baseline, eval_epoch
from kbcore import review as review_mod
from kbcore.ingest import ingest
from kbworker import (
    digest,
    eval_epoch_lines,
    eval_regression,
    eval_sentinel,
    weekly_instruments,
    weekly_render,
)
from kbworker.digest_lines import night_of
from tests.conftest import DB_UP

needs_db = pytest.mark.skipif(not DB_UP, reason="compose Postgres not reachable")
PKT = timezone(timedelta(hours=5))  # the deployment's zone, pinned (never the box's)
MISS_STATS = {"total": 0, "distinct": 0, "empty": 0, "thin": 0}
NOW = datetime(2026, 9, 20, 1, 4, tzinfo=PKT)  # a Sunday weekly instant; night_of == 09-19
E1 = "e1e1e1e1e1e1"
E0 = "000000000000"


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


def _ok(**meta) -> int:
    return _report(**{"eval_status": "ok", "eval_hit_at_5": 0.98, "eval_mrr": 0.92, **meta})


def _rep(at: datetime, **meta):
    """A hermetic report meta as the sentinel writes it: ``eval_status='ok'`` beside
    the metrics unless the test says otherwise (an error night overrides it)."""
    return (at, {"eval_status": "ok", **meta})


# --- 1. the epoch id -----------------------------------------------------------------


def test_golden_hash_is_content_derived_stable_and_twelve_hex(tmp_path):
    golden = tmp_path / "golden.yaml"
    golden.write_text("- q: a\n  expect_substr: [x]\n", encoding="utf-8")
    first = eval_epoch.golden_hash(golden)
    assert len(first) == eval_epoch.HASH_HEX_CHARS == 12 and int(first, 16) >= 0
    assert eval_epoch.golden_hash(golden) == first, "same bytes, same epoch"
    golden.write_text("- q: a\n  expect_substr: [x]\n- q: b\n", encoding="utf-8")
    assert eval_epoch.golden_hash(golden) != first, "ANY change to the file opens a new epoch"
    assert eval_epoch.golden_hash(tmp_path / "absent.yaml") == eval_epoch.golden_hash(
        tmp_path / "also-absent.yaml"
    ), "a missing file hashes deterministically (empty bytes) instead of raising"
    live = eval_epoch.golden_hash()  # the default: the repo's eval/golden.yaml
    assert len(live) == 12 and live == eval_epoch.golden_hash()


def test_sentinel_records_the_golden_hash_next_to_eval_queries(monkeypatch):
    monkeypatch.setattr(eval_sentinel, "TRUST_METRIC_ENABLED", False)
    monkeypatch.setattr(eval_sentinel, "_detect_regression", lambda counts, h, m: None)
    cases = [{"q": "a", "expect_substr": ["x"]}, {"q": "b", "expect_substr": ["x"]}]
    monkeypatch.setattr(eval_sentinel.eval_harness, "load_golden", lambda golden_path=None: cases)
    monkeypatch.setattr(
        eval_sentinel.eval_harness,
        "evaluate",
        lambda golden=None, golden_path=None: ([("a", 1), ("b", 2)], 1.0, 0.75),
    )
    counts: dict = {}
    eval_sentinel.run_sentinel(counts)
    assert counts["eval_status"] == "ok" and counts["eval_queries"] == 2
    assert counts["eval_golden_hash"] == eval_epoch.golden_hash()


# --- 2. the epoch-confined baseline read ---------------------------------------------


@needs_db
def test_baseline_with_a_hash_skips_other_epochs_and_untracked_reports(kb, caplog):
    caplog.set_level(logging.INFO, logger="kbcore.eval_baseline")
    _ok(eval_mrr=0.9077)  # pre-E1: no key at all
    other = _ok(eval_mrr=0.95, eval_golden_hash=E0)  # another golden set
    prior = eval_baseline.previous_eval_metrics(E1)
    assert prior is None, "nothing in tonight's epoch: no WoW baseline"
    assert "golden epoch untracked, tonight is e1e1e1e1e1e1" in caplog.text
    assert "golden epoch 000000000000, tonight is e1e1e1e1e1e1" in caplog.text
    none_prior, skips = eval_baseline.read_baseline(E1)
    assert none_prior is None and skips == {"skipped_fallback": 0, "skipped_epoch": 2}
    # Untracked read: byte-identical to before — the newest ok report, no epoch key.
    assert eval_baseline.previous_eval_metrics() == {
        "hit_at_5": 0.98,
        "mrr": 0.95,
        "report_id": other,
        "skipped_fallback": 0,
    }
    mine = _ok(eval_mrr=0.91, eval_golden_hash=E1)
    _ok(eval_mrr=0.99, eval_golden_hash=E0)  # newer, but a different set: walked over
    assert eval_baseline.previous_eval_metrics(E1) == {
        "hit_at_5": 0.98,
        "mrr": 0.91,
        "report_id": mine,
        "skipped_fallback": 0,
        "skipped_epoch": 1,
    }


@needs_db
def test_baseline_epoch_and_fallback_skips_compose(kb):
    _ok(eval_mrr=0.90, eval_golden_hash=E1)
    _ok(eval_mrr=0.94, eval_golden_hash=E1, eval_rerank_tally={"active": 40, "fallback": 15})
    _ok(eval_mrr=0.97, eval_golden_hash=E0)
    prior = eval_baseline.previous_eval_metrics(E1)
    assert prior["mrr"] == 0.90 and prior["skipped_fallback"] == 1 and prior["skipped_epoch"] == 1


# --- 3. the detector: first night resets, second night compares ----------------------


@needs_db
def test_detector_marks_the_first_night_of_an_epoch_and_not_the_second(kb, caplog):
    _ok(eval_mrr=0.9077)  # the pre-E1 52-case world
    first = {"eval_regression": False, "eval_queries": 59, "eval_golden_hash": E1}
    with caplog.at_level(logging.INFO, logger="kbworker.eval_sentinel"):
        eval_sentinel._detect_regression(first, 0.98, 0.85)
    assert first["eval_epoch_reset"] == {"golden_hash": E1, "queries": 59, "skipped_epoch": 1}
    assert first["eval_regression"] is False, "no prior: a -0.06 MRR step is not a WoW drop"
    assert "eval epoch e1e1e1e1e1e1 (n=59): first night" in caplog.text
    assert "floors 0.75/0.50 still apply" in caplog.text
    _ok(eval_mrr=0.85, eval_golden_hash=E1)  # tonight's report, persisted as the run does
    second = {"eval_regression": False, "eval_queries": 59, "eval_golden_hash": E1}
    eval_sentinel._detect_regression(second, 0.98, 0.85)
    assert "eval_epoch_reset" not in second, "the second night has a same-epoch prior"
    assert second["eval_regression"] is False
    third = {"eval_regression": False, "eval_queries": 59, "eval_golden_hash": E1}
    eval_sentinel._detect_regression(third, 0.98, 0.78)  # -0.07 vs the epoch's own prior
    assert third["eval_regression"] is True and "eval_epoch_reset" not in third
    _qid, detail = review_mod.open_review_by_reason(eval_regression.REVIEW_REASON)
    assert detail["baseline_mrr"] == 0.85, "anchored to the epoch's prior, not the 52-case 0.9077"


@needs_db
def test_detector_floors_still_gate_the_reset_night_and_a_genuine_first_run_is_no_reset(kb):
    fresh = {"eval_regression": False, "eval_queries": 59, "eval_golden_hash": E1}
    eval_sentinel._detect_regression(fresh, 0.98, 0.92)
    assert "eval_epoch_reset" not in fresh, "no prior reports at all: a first run, not a reset"
    _ok(eval_mrr=0.92)  # untracked prior -> tonight is a reset night
    red = {"eval_regression": False, "eval_queries": 59, "eval_golden_hash": E1}
    eval_sentinel._detect_regression(red, 0.70, 0.40)  # under the 0.75/0.5 code-default floors
    assert red["eval_epoch_reset"]["skipped_epoch"] == 1
    assert red["eval_regression"] is True, "the anchored floors apply on the reset night"
    _qid, detail = review_mod.open_review_by_reason(eval_regression.REVIEW_REASON)
    assert detail["baseline_mrr"] == 0.5, "the anchor is the floor: no same-epoch prior to max with"


@needs_db
def test_detector_does_not_re_reset_when_the_epochs_only_nights_fell_back(kb, caplog):
    _ok(eval_mrr=0.9077)  # untracked prior: the pre-E1 world
    _ok(eval_mrr=0.94, eval_golden_hash=E1, eval_rerank_tally={"active": 48, "fallback": 11})
    # Night 2 of the epoch: no CLEAN prior yet, but night 1 exists in the epoch.
    assert eval_baseline.read_baseline(E1) == (None, {"skipped_fallback": 1, "skipped_epoch": 1})
    second = {"eval_regression": False, "eval_queries": 59, "eval_golden_hash": E1}
    with caplog.at_level(logging.INFO, logger="kbworker.eval_sentinel"):
        eval_sentinel._detect_regression(second, 0.98, 0.90)
    assert "eval_epoch_reset" not in second, "a fallback night 1 is still the epoch's night 1"
    assert "first night" not in caplog.text
    assert second["eval_regression"] is False, "no clean prior: floors only, and these clear them"
    assert not any("eval epoch" in ln for ln in digest.digest_lines(second))
    # The rule relies on read_baseline tallying a report under at most ONE skip with
    # the epoch filter first: an OTHER-epoch fallback night is skipped_epoch, never
    # skipped_fallback — so 'fallback skips == 0' really means 'no same-epoch report'.
    _ok(eval_mrr=0.9, eval_golden_hash=E0, eval_rerank_tally={"active": 40, "fallback": 5})
    assert eval_baseline.read_baseline(E1) == (None, {"skipped_fallback": 1, "skipped_epoch": 2})


@needs_db
def test_detector_without_a_hash_reads_the_unfiltered_baseline(kb):
    _ok(eval_mrr=0.92)
    counts = {"eval_regression": False}
    eval_sentinel._detect_regression(counts, 0.98, 0.80)  # -0.12 vs the untracked prior
    assert counts["eval_regression"] is True and "eval_epoch_reset" not in counts


# --- 4. the digest line ----------------------------------------------------------------


def test_digest_names_the_first_night_of_an_epoch_only_when_the_run_carries_the_key():
    counts = {"queued_drained": 3, "eval_status": "ok", "eval_golden_hash": E1}
    assert not any("eval epoch" in ln for ln in digest.digest_lines(counts)), (
        "a tracked but ordinary night contributes no epoch line"
    )
    counts["eval_epoch_reset"] = {"golden_hash": E1, "queries": 59, "skipped_epoch": 7}
    lines = digest.digest_lines(counts)
    assert "eval epoch e1e1e1e1e1e1 (n=59): first night - WoW baseline reset, floors only" in lines
    assert all(ln.isascii() for ln in lines), "ntfy to a phone: pure ASCII"
    assert lines.index("drained: 3") < lines.index(next(ln for ln in lines if "epoch" in ln)), (
        "a routine fact, after the flows"
    )
    assert eval_epoch_lines.eval_epoch_line({"eval_epoch_reset": "bogus"}) is None


# --- 5. the weekly series splits on the hash -----------------------------------------


def test_weekly_eval_series_read_the_latest_epoch_and_say_when_it_reset():
    day = timedelta(days=1)
    t0 = datetime(2026, 9, 12, 20, 0, tzinfo=UTC)  # 01:00 local Sep 13: night 09-12
    week = [
        _rep(t0, eval_hit_at_5=0.9808, eval_mrr=0.9077),  # untracked: the 52-case world
        _rep(t0 + day, eval_hit_at_5=0.9808, eval_mrr=0.9077),
        _rep(t0 + 2 * day, eval_hit_at_5=0.9808, eval_mrr=0.9269, eval_golden_hash=E0),
        _rep(t0 + 3 * day, eval_hit_at_5=0.9661, eval_mrr=0.8800, eval_golden_hash=E1),
        _rep(t0 + 4 * day, eval_hit_at_5=0.9831, eval_mrr=0.9000, eval_golden_hash=E1),
        _rep(
            t0 + 5 * day,
            eval_hit_at_5=1.0,
            eval_mrr=0.9500,
            eval_golden_hash=E1,
            eval_rerank_tally={"active": 40, "fallback": 19, "off": 0},
        ),
    ]
    assert eval_epoch_lines.epoch_groups(week) == [None, E0, E1]
    assert [m["eval_mrr"] for _a, m in eval_epoch_lines.epoch_reports(week)] == [0.88, 0.9, 0.95]
    assert [m["eval_mrr"] for _a, m in eval_epoch_lines.epoch_clean(week)] == [0.88, 0.9]
    assert eval_epoch_lines.epoch_suffix(week, PKT) == " (epoch reset 2026-09-15)"
    push = weekly_render._push_lines(NOW, week, MISS_STATS, [], 0, None, 1, tz=PKT)
    assert "eval mrr: 0.880 -> 0.900 (epoch reset 2026-09-15)" in push
    assert "eval hit@5: 0.966 -> 0.983" in push, "hit@5 reads the same epoch; the note rides mrr"
    assert all(ln.isascii() for ln in push) and len(push) <= weekly_render.PUSH_MAX_LINES
    # One epoch only (tracked or not): the series is what it always was, no note.
    untracked = week[:2]
    assert eval_epoch_lines.epoch_suffix(untracked, PKT) == ""
    push = weekly_render._push_lines(NOW, untracked, MISS_STATS, [], 0, None, 1, tz=PKT)
    assert "eval mrr: 0.908 -> 0.908" in push
    tracked = week[3:5]
    assert eval_epoch_lines.epoch_suffix(tracked, PKT) == ""
    push = weekly_render._push_lines(NOW, tracked, MISS_STATS, [], 0, None, 1, tz=PKT)
    assert "eval mrr: 0.880 -> 0.900" in push
    assert eval_epoch_lines.epoch_reports([]) == [] and eval_epoch_lines.epoch_suffix([]) == ""


# --- 6. epochs are defined over EVALUATED reports only (review finding P2) ------------

T0 = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)  # 01:00 local Sep 14: night 09-13
DAY = timedelta(days=1)


def _e1_nights() -> list:
    """Two clean E1 nights, 09-14 and 09-15 local."""
    return [
        _rep(T0 + DAY, eval_hit_at_5=0.9661, eval_mrr=0.8800, eval_golden_hash=E1),
        _rep(T0 + 2 * DAY, eval_hit_at_5=0.9831, eval_mrr=0.9000, eval_golden_hash=E1),
    ]


def test_a_trailing_error_night_neither_opens_a_group_nor_selects_the_pre_epoch_one():
    e1 = _e1_nights()
    error = _rep(T0 + 3 * DAY, eval_status="error", queued_drained=4)  # no hash, no metrics
    window = e1 + [error]
    assert eval_epoch_lines.epoch_groups(window) == [E1], "an error night is no epoch"
    assert eval_epoch_lines.epoch_reports(window) == e1
    suffix = eval_epoch_lines.epoch_suffix
    assert suffix(window, PKT) == suffix(e1, PKT) == "", "unchanged by the error night"
    push = weekly_render._push_lines(NOW, window, MISS_STATS, [], 0, None, 1, tz=PKT)
    assert "eval mrr: 0.880 -> 0.900" in push and "eval hit@5: 0.966 -> 0.983" in push
    # Pre-fix, reports[-1] carried no key, so the key-less group was 'latest': the
    # series trended nothing (n/a) under a bogus ' (epoch reset 2026-09-16)'.
    assert not any("epoch reset" in ln for ln in push)


def test_an_error_night_inside_a_single_epoch_is_no_reset():
    first, second = _e1_nights()
    mid_error = _rep(first[0] + timedelta(hours=12), eval_status="error")
    skipped = _rep(first[0] + timedelta(hours=13), eval_status="skipped")
    window = [first, mid_error, skipped, second]
    assert eval_epoch_lines.epoch_groups(window) == [E1]
    assert eval_epoch_lines.epoch_suffix(window, PKT) == ""
    assert [m["eval_mrr"] for _a, m in eval_epoch_lines.epoch_clean(window)] == [0.88, 0.9]


def test_a_trailing_disabled_night_keeps_the_latest_epoch_and_its_reset_night():
    pre = _rep(T0, eval_hit_at_5=0.9808, eval_mrr=0.9077)  # untracked: the 52-case world
    window = [pre, *_e1_nights(), _rep(T0 + 3 * DAY, eval_status="disabled")]
    assert eval_epoch_lines.epoch_groups(window) == [None, E1]
    assert [m["eval_mrr"] for _a, m in eval_epoch_lines.epoch_reports(window)] == [0.88, 0.9]
    assert eval_epoch_lines.epoch_suffix(window, PKT) == " (epoch reset 2026-09-14)"
    push = weekly_render._push_lines(NOW, window, MISS_STATS, [], 0, None, 1, tz=PKT)
    assert "eval mrr: 0.880 -> 0.900 (epoch reset 2026-09-14)" in push
    nothing_evaluated = [_rep(T0, eval_status="error"), _rep(T0 + DAY, eval_status="disabled")]
    assert eval_epoch_lines.epoch_reports(nothing_evaluated) == [], "n/a, as before"
    assert eval_epoch_lines.epoch_suffix(nothing_evaluated, PKT) == ""


# --- 7. the reset night is keyed on the run's START, like the table rows (P3) ---------


def test_the_reset_night_is_keyed_on_the_runs_start_like_the_table_rows():
    # The epoch's first report FINISHED 12:30 local Sep 15 (07:30 UTC), past the noon
    # rollover, but the 6 h run STARTED 06:30 local: night 09-14 — the row
    # weekly_instruments.nights files it under. Keyed on the finish it read 09-15,
    # a row the table does not have.
    pre = _rep(T0, eval_hit_at_5=0.9808, eval_mrr=0.9077)
    first = _rep(
        datetime(2026, 9, 15, 7, 30, tzinfo=UTC),
        run_secs=6 * 3600,
        eval_hit_at_5=0.9661,
        eval_mrr=0.88,
        eval_golden_hash=E1,
    )
    ((table_night, _runs),) = weekly_instruments.nights([first], PKT)
    assert table_night.isoformat() == "2026-09-14"
    assert night_of(first[0], PKT).isoformat() == "2026-09-15", "the finish is the next night"
    assert eval_epoch_lines.epoch_suffix([pre, first], PKT) == " (epoch reset 2026-09-14)"
    push = weekly_render._push_lines(NOW, [pre, first], MISS_STATS, [], 0, None, 1, tz=PKT)
    assert "eval mrr: 0.880 (epoch reset 2026-09-14)" in push

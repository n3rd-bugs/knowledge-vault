"""Search-quality regression sentinel (§8): the nightly consolidation replays the
golden eval, records hit@5/MRR in the run result, flags drops vs the previous run
into the review queue, and never breaks the run.

Real DB in kb_test; conftest mocks the embedder; the 14B extractor is mocked here
(Invariant 8 — the real Ollama extractor is never called). The eval itself is
stubbed via kbcore.eval_harness so the metrics are deterministic, except one test
that runs the REAL harness to prove search uses embeds+SQL only (no extractor).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from kbcore import db, eval_harness, llm
from kbcore import review as review_mod
from kbcore.config import settings
from kbcore.ingest import ingest
from kbcore.items import get_item
from kbworker import consolidate, eval_cases, eval_persistence, eval_regression, eval_sentinel


@pytest.fixture(autouse=True)
def _mock_extractor(monkeypatch, tmp_path):
    """Mock the 14B extractor and keep raw/disk reads off the real ~/kb."""
    monkeypatch.setattr(llm, "summarize_items", lambda topic, items: f"Summary of {topic}.")
    monkeypatch.setattr(llm, "items_contradict", lambda *a, **k: False)
    monkeypatch.setattr(settings, "kb_raw", tmp_path / "kbraw")


# --- helpers -----------------------------------------------------------------


def _stub_eval(monkeypatch, hit: float, mrr: float, n: int = 5) -> None:
    """Make the sentinel's eval return fixed metrics over ``n`` fake queries."""
    golden = [{"q": f"q{i}", "expect_substr": ["x"]} for i in range(n)]
    monkeypatch.setattr(eval_harness, "load_golden", lambda golden_path=None: golden)
    monkeypatch.setattr(
        eval_harness,
        "evaluate",
        lambda golden=None, golden_path=None: ([(c["q"], 1) for c in (golden or [])], hit, mrr),
    )


def _open_regression_rows() -> list[tuple]:
    with db.connect() as conn:
        return conn.execute(
            "SELECT id, item_id, detail FROM review_queue "
            "WHERE reason = 'search_regression' AND resolved = false ORDER BY id"
        ).fetchall()


# --- metrics recorded --------------------------------------------------------


def test_sentinel_records_metrics_in_run_result(kb, monkeypatch):
    _stub_eval(monkeypatch, 1.0, 0.9, n=5)

    result = consolidate.main()

    assert result["skipped"] is False
    assert result["eval_status"] == "ok"
    assert result["eval_hit_at_5"] == 1.0
    assert result["eval_mrr"] == 0.9
    assert result["eval_queries"] == 5
    assert result["eval_regression"] is False
    # Persisted in the report item meta — the exact place the next run reads back.
    report = get_item(result["report_item_id"])
    assert report.meta["eval_status"] == "ok"
    assert report.meta["eval_hit_at_5"] == 1.0
    assert report.meta["eval_mrr"] == 0.9


# --- regression detection ----------------------------------------------------


def test_metric_drop_enqueues_exactly_one_review(kb, monkeypatch):
    _stub_eval(monkeypatch, 0.9, 0.85)
    consolidate.main()  # baseline run

    _stub_eval(monkeypatch, 0.7, 0.6)
    second = consolidate.main()  # drop > epsilon

    assert second["skipped"] is False, "a regression must never abort the run"
    assert second["eval_regression"] is True
    rows = _open_regression_rows()
    assert len(rows) == 1
    _qid, item_id, detail = rows[0]
    assert item_id is None, "the regression flag is item-less"
    assert detail["baseline_hit_at_5"] == 0.9
    assert detail["observed_hit_at_5"] == 0.7

    # A further drop still flags the run but must NOT stack a second row (dedup).
    _stub_eval(monkeypatch, 0.5, 0.4)
    third = consolidate.main()
    assert third["eval_regression"] is True
    assert len(_open_regression_rows()) == 1, "exactly one open flag at a time"


def test_small_drop_within_epsilon_is_not_a_regression(kb, monkeypatch):
    _stub_eval(monkeypatch, 0.90, 0.90)
    consolidate.main()

    _stub_eval(monkeypatch, 0.87, 0.87)  # 0.03 drop < 0.05 epsilon
    result = consolidate.main()

    assert result["eval_regression"] is False
    assert _open_regression_rows() == []


def test_baseline_walks_back_past_an_errored_night(kb, monkeypatch):
    # HIGH-2: one errored night must NOT wipe the baseline. Night 3 is above the
    # floor, so the ONLY way it can flag is a drop vs night 1 — which requires
    # previous_eval_metrics() to walk back over night 2's non-ok report.
    _stub_eval(monkeypatch, 0.95, 0.90)
    consolidate.main()  # night 1: ok baseline

    monkeypatch.setattr(
        eval_harness, "load_golden", lambda golden_path=None: [{"q": "q", "expect_substr": ["x"]}]
    )

    def _boom(*a, **k):
        raise RuntimeError("embed backend down")

    monkeypatch.setattr(eval_harness, "evaluate", _boom)
    night2 = consolidate.main()  # night 2: eval errors → interleaved non-ok report
    assert night2["eval_status"] == "error"

    _stub_eval(monkeypatch, 0.85, 0.80)  # above floor, but 0.10 below night 1
    third = consolidate.main()
    assert third["eval_regression"] is True, "regression detected vs the last OK night"
    rows = _open_regression_rows()
    assert len(rows) == 1 and rows[0][2]["baseline_hit_at_5"] == 0.95


def test_absolute_floor_flags_gradual_subepsilon_drift(kb, monkeypatch):
    # The boiled-frog case: each step is under epsilon so the relative check never
    # trips, but the metrics cross the anchored floor → flagged (finding).
    _stub_eval(monkeypatch, 0.78, 0.78)
    consolidate.main()  # above floor, no prior → not flagged
    _stub_eval(monkeypatch, 0.74, 0.74)  # drop 0.04 < epsilon, but 0.74 < 0.75 floor
    result = consolidate.main()
    assert result["eval_regression"] is True, "below-floor drift is caught under epsilon"
    assert len(_open_regression_rows()) == 1


def test_partial_rebound_does_not_auto_resolve_or_ratchet(kb, monkeypatch):
    # The flapping ratchet: a partial rebound (>= baseline − epsilon but < baseline)
    # must NOT auto-resolve, and the recovery anchor must not drift down (finding).
    _stub_eval(monkeypatch, 0.9, 0.9)
    consolidate.main()  # baseline
    _stub_eval(monkeypatch, 0.7, 0.7)  # regression; anchor pinned at 0.9
    consolidate.main()
    assert _open_regression_rows()[0][2]["baseline_hit_at_5"] == 0.9

    _stub_eval(monkeypatch, 0.86, 0.86)  # >= 0.9 − 0.05 but < 0.9 → partial
    consolidate.main()
    rows = _open_regression_rows()
    assert len(rows) == 1, "a partial rebound does not self-close the flag"
    assert rows[0][2]["baseline_hit_at_5"] == 0.9, "the recovery anchor never ratchets down"

    _stub_eval(monkeypatch, 0.9, 0.9)  # full recovery to the anchor
    consolidate.main()
    assert _open_regression_rows() == [], "full recovery to the anchor auto-resolves"


def test_recovery_auto_resolves_open_flag(kb, monkeypatch):
    _stub_eval(monkeypatch, 0.9, 0.85)
    consolidate.main()  # baseline
    _stub_eval(monkeypatch, 0.7, 0.6)
    consolidate.main()  # regression → one open row
    assert len(_open_regression_rows()) == 1

    _stub_eval(monkeypatch, 0.9, 0.85)
    recovered = consolidate.main()  # quality back to baseline

    assert recovered["eval_regression"] is False
    assert _open_regression_rows() == [], "recovered quality auto-resolves the flag"


# --- kill switch -------------------------------------------------------------


def test_kill_switch_skips_eval_entirely(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_eval_in_consolidation", False)
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("eval must not run when the kill switch is off")

    monkeypatch.setattr(eval_harness, "load_golden", _boom)
    monkeypatch.setattr(eval_harness, "evaluate", _boom)

    result = consolidate.main()

    assert result["skipped"] is False
    assert calls["n"] == 0
    assert result["eval_status"] == "disabled"
    assert "eval_hit_at_5" not in result


# --- resilience: a broken eval never breaks consolidation --------------------


def test_eval_failure_does_not_break_consolidation(kb, monkeypatch):
    monkeypatch.setattr(
        eval_harness, "load_golden", lambda golden_path=None: [{"q": "q", "expect_substr": ["x"]}]
    )

    def _boom(*a, **k):
        raise RuntimeError("embed backend down")

    monkeypatch.setattr(eval_harness, "evaluate", _boom)

    result = consolidate.main()

    assert result["skipped"] is False, "the run must still succeed"
    assert result["eval_status"] == "error"
    assert "report_item_id" in result
    assert "eval_hit_at_5" not in result
    assert _open_regression_rows() == [], "a failed eval enqueues nothing"


def test_empty_golden_records_skipped(kb, monkeypatch):
    monkeypatch.setattr(eval_harness, "load_golden", lambda golden_path=None: [])

    def _boom(*a, **k):
        raise AssertionError("evaluate must not run against an empty golden set")

    monkeypatch.setattr(eval_harness, "evaluate", _boom)

    result = consolidate.main()

    assert result["eval_status"] == "skipped"
    assert "eval_hit_at_5" not in result


# --- GPU courtesy (Invariant 8): the REAL eval never invokes the 14B ----------


def test_real_eval_is_search_only_no_extractor(kb, monkeypatch):
    # Rig EVERY generation path to explode, then run the real harness: search is
    # embed (mocked) + SQL only, so it must retrieve the seeded item untouched.
    def _no_llm(*a, **k):
        raise AssertionError("the 14B extractor must not run during eval")

    monkeypatch.setattr(llm, "_chat", _no_llm)
    ingest(
        "p",
        "gotcha",
        "SPI polarity fix",
        "The DRV8706 SPI clock polarity gotcha and its fix.",
        uri="g1",
        trust="verified",
    )
    golden = [{"q": "DRV8706 SPI polarity", "expect_substr": ["DRV8706"]}]

    results, hit_at_5, mrr = eval_harness.evaluate(golden=golden)

    assert len(results) == 1
    assert hit_at_5 == 1.0 and mrr == 1.0, "seeded item is retrieved at rank 1"


# --- E2 (review #10): floors in meta, the trip cause, the printed anchor -------

EPOCH = "f1f1f1f1f1f1"


def test_floors_are_always_in_meta_and_floors_at_defaults_warn(kb, monkeypatch, caplog):
    _stub_eval(monkeypatch, 1.0, 0.9)
    with caplog.at_level(logging.WARNING, logger="kbworker.eval_sentinel"):
        green = consolidate.main()  # conftest pins the CODE default floors 0.75/0.5
    assert (green["eval_floor_hit_at_5"], green["eval_floor_mrr"]) == (0.75, 0.5)
    assert green["eval_floors_at_defaults"] is True and green["eval_regression"] is False
    assert "are the CODE defaults" in caplog.text and "missing from .env" in caplog.text
    meta = get_item(green["report_item_id"]).meta
    assert meta["eval_floor_hit_at_5"] == 0.75 and meta["eval_floors_at_defaults"] is True
    caplog.clear()
    monkeypatch.setattr(settings, "kb_eval_min_hit5", 0.95)
    monkeypatch.setattr(settings, "kb_eval_min_mrr", 0.855)
    with caplog.at_level(logging.WARNING, logger="kbworker.eval_sentinel"):
        anchored = consolidate.main()
    assert (anchored["eval_floor_hit_at_5"], anchored["eval_floor_mrr"]) == (0.95, 0.855)
    assert "eval_floors_at_defaults" not in anchored and "CODE defaults" not in caplog.text
    # Floors ride a fallback night's meta too — every run, not only the clean ones.
    stalled = {"eval_rerank_tally": {"active": 3, "fallback": 2, "off": 0}}
    eval_sentinel._detect_regression(stalled, 0.99, 0.99)
    assert (stalled["eval_floor_hit_at_5"], stalled["eval_floor_mrr"]) == (0.95, 0.855)


def test_every_trip_names_its_cause_in_meta_and_on_the_row(kb, monkeypatch):
    _stub_eval(monkeypatch, 0.9, 0.85)
    consolidate.main()
    _stub_eval(monkeypatch, 0.7, 0.84)  # floor AND drop on hit@5; MRR within epsilon
    red = consolidate.main()
    assert red["eval_regression"] is True
    assert red["eval_regression_cause"] == ["drop_hit_at_5", "floor_hit_at_5"]
    (_qid, _item, detail) = _open_regression_rows()[0]
    assert detail["cause"] == red["eval_regression_cause"]
    assert detail["golden_hash"] == red["eval_golden_hash"], "rows carry their epoch"
    assert "eval_regression_cause" not in get_item(red["report_item_id"]).body, "a list: meta"
    _stub_eval(monkeypatch, 0.9, 0.85)
    assert "eval_regression_cause" not in consolidate.main(), "a green night has no cause"


def _prior(**meta) -> None:
    ingest(
        "kb-system",
        "report",
        "Consolidation report",
        "Nightly consolidation report\n\n- test",
        kind="chat",
        uri=f"report:{uuid.uuid4()}",
        trust="verified",
        meta={"consolidation": True, "eval_status": "ok", "eval_golden_hash": EPOCH, **meta},
    )


def test_the_warning_prints_the_anchor_the_resolve_test_reads(kb, caplog):
    stored = {"baseline_hit_at_5": 0.8, "baseline_mrr": 0.6, "golden_hash": EPOCH}
    qid = review_mod.queue_review(None, eval_regression.REVIEW_REASON, stored)
    _prior(eval_hit_at_5=0.95, eval_mrr=0.9)  # a HIGHER prior: pre-E2 printed max() = 0.95
    counts = {"eval_regression": False, "eval_golden_hash": EPOCH}
    with caplog.at_level(logging.WARNING, logger="kbworker.eval_sentinel"):
        eval_sentinel._detect_regression(counts, 0.70, 0.40)
    assert f"recovery anchor 0.800/0.600 stored on open row {qid}" in caplog.text
    assert "0.950/0.900 stored" not in caplog.text
    assert "REGRESSION: drop_hit_at_5+drop_mrr+floor_hit_at_5+floor_mrr" in caplog.text
    # ...and the auto-resolve reads exactly that stored anchor: 0.80/0.60 closes it.
    _prior(eval_hit_at_5=0.70, eval_mrr=0.40)  # that night's report, persisted as the run does
    eval_sentinel._detect_regression({"eval_golden_hash": EPOCH}, 0.80, 0.60)
    assert review_mod.open_review_by_reason(eval_regression.REVIEW_REASON) is None
    _prior(eval_hit_at_5=0.80, eval_mrr=0.60)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="kbworker.eval_sentinel"):
        eval_sentinel._detect_regression({"eval_golden_hash": EPOCH}, 0.70, 0.40)
    assert "recovery anchor 0.800/0.600 for a new row" in caplog.text, "max(prior, floor)"


# --- review #10 fix round: boundaries, a single default floor, the guarded fold --------


def test_a_night_exactly_at_a_floor_or_epsilon_is_no_trip(kb):
    """F6 (iii) / TA-11: the boundaries are strict — AT the floor or a decimal drop of
    exactly epsilon (0.90 -> 0.85 is 0.05000000000000004 in floats) never trips."""
    at_floor = {"eval_regression": False, "eval_golden_hash": EPOCH}
    eval_sentinel._detect_regression(at_floor, 0.75, 0.5)  # no prior: the floors alone
    assert at_floor["eval_regression"] is False and "eval_regression_cause" not in at_floor
    _prior(eval_hit_at_5=0.9, eval_mrr=0.9)
    at_eps = {"eval_regression": False, "eval_golden_hash": EPOCH}
    eval_sentinel._detect_regression(at_eps, 0.85, 0.85)
    assert at_eps["eval_regression"] is False and "eval_regression_cause" not in at_eps
    past = {"eval_regression": False, "eval_golden_hash": EPOCH}
    eval_sentinel._detect_regression(past, 0.8499, 0.85)  # one stored quantum past epsilon
    assert past["eval_regression_cause"] == ["drop_hit_at_5"]
    assert _open_regression_rows() != []


def test_only_one_floor_at_its_default_sets_no_flag(monkeypatch, caplog):
    monkeypatch.setattr(settings, "kb_eval_min_mrr", 0.6)  # hit@5 still at its 0.75 default
    counts: dict = {}
    with caplog.at_level(logging.WARNING, logger="kbworker.eval_sentinel"):
        eval_regression._record_floors(counts)
    assert "eval_floors_at_defaults" not in counts and "CODE defaults" not in caplog.text
    assert (counts["eval_floor_hit_at_5"], counts["eval_floor_mrr"]) == (0.75, 0.6)


def test_a_broken_replay_fold_never_costs_the_detector(kb, monkeypatch):
    """F13: replay telemetry raising is an error STATUS; the floors still trip tonight."""
    _stub_eval(monkeypatch, 0.6, 0.4)  # under both floors

    def boom(results):
        raise RuntimeError("telemetry fold broke")

    monkeypatch.setattr(eval_cases, "replay_keys", boom)
    red = consolidate.main()
    assert red["eval_status"] == "ok" and red["eval_replay_telemetry_status"] == "error"
    assert red["eval_regression"] is True and red["eval_regression_cause"] == [
        "floor_hit_at_5",
        "floor_mrr",
    ], "the detector ran"
    assert red["eval_persistence_status"] == "error", "lost verdicts: persistence UNKNOWN"
    assert "eval_case_ranks" in red, "the other producer's keys still landed"
    report = get_item(red["report_item_id"])
    assert report.meta["eval_replay_telemetry_status"] == "error", "the owner reads it in meta"
    assert "eval_replay_telemetry_status" not in report.body, "E2 adds no body line (FB-1)"


def test_a_raising_per_case_fold_alone_still_lands_the_verdicts(monkeypatch):
    """H3 (MJ) / TA2-8 (G1): per_case_keys is guarded ON ITS OWN — when it raises, the
    status reads 'error' and replay_keys' verdicts still land for the detector."""

    def boom(results):
        raise RuntimeError("per-case fold broke")

    monkeypatch.setattr(eval_cases, "per_case_keys", boom)
    monkeypatch.setattr(eval_cases, "_fingerprint", lambda: {})
    q = "synthetic prose case for the guarded fold"
    counts: dict = {}
    eval_cases.replay_telemetry(counts, [eval_harness.CaseResult(q, 1, True, top_ids=[5])])
    assert counts["eval_replay_telemetry_status"] == "error"
    assert counts["eval_case_passed"] == {q: True} and "eval_case_ranks" not in counts


def test_a_fallback_measurement_reads_its_flags_off_the_mrr_causes_too(kb):
    """TA2-8 (B55/B56): below_floor / dropped follow EVERY cause, the MRR ones included."""
    fell = {"active": 3, "fallback": 2, "off": 0}
    _prior(eval_hit_at_5=0.95, eval_mrr=0.52)
    floor_only = {"eval_regression": False, "eval_golden_hash": EPOCH, "eval_rerank_tally": fell}
    eval_sentinel._detect_regression(floor_only, 0.95, 0.49)  # under 0.5, within epsilon
    m = floor_only["eval_regression_measurement"]
    assert (m["cause"], m["below_floor"], m["dropped"]) == (["floor_mrr"], True, False)
    _prior(eval_hit_at_5=0.95, eval_mrr=0.90)
    drop_only = {"eval_regression": False, "eval_golden_hash": EPOCH, "eval_rerank_tally": fell}
    eval_sentinel._detect_regression(drop_only, 0.95, 0.80)  # 0.10 down, above the floor
    m = drop_only["eval_regression_measurement"]
    assert (m["cause"], m["below_floor"], m["dropped"]) == (["drop_mrr"], False, True)
    assert _open_regression_rows() == [], "a measurement never queues a row"


def test_the_run_stamps_its_utc_start_and_the_trip_keys_tonight_on_it(kb, monkeypatch):
    """F4: consolidate.main stamps run_started_at at its run clock; persistence reads it."""
    seen: list = []
    real = eval_persistence.tonight_start

    def spy(counts, now):
        seen.append(counts.get("run_started_at"))
        return real(counts, now)

    monkeypatch.setattr(eval_persistence, "tonight_start", spy)
    q = "synthetic prose case for the run clock"
    monkeypatch.setattr(eval_harness, "load_golden", lambda golden_path=None: [{"q": q}])
    monkeypatch.setattr(
        eval_harness,
        "evaluate",
        lambda golden=None, golden_path=None: ([eval_harness.CaseResult(q, 1, True)], 1.0, 1.0),
    )
    before = datetime.now(UTC)
    result = consolidate.main()
    started = datetime.fromisoformat(result["run_started_at"])
    assert started.utcoffset() == timedelta(0) and before <= started <= datetime.now(UTC)
    assert seen == [result["run_started_at"]], "tonight is keyed on the stamped start"
    report = get_item(result["report_item_id"])
    assert report.meta["run_started_at"] == result["run_started_at"], "stored in meta"
    assert "run_started_at" not in report.body, "E2 adds no body line (FB-1)"

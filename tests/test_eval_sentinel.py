"""Search-quality regression sentinel (§8): the nightly consolidation replays the
golden eval, records hit@5/MRR in the run result, flags drops vs the previous run
into the review queue, and never breaks the run.

Real DB in kb_test; conftest mocks the embedder; the 14B extractor is mocked here
(Invariant 8 — the real Ollama extractor is never called). The eval itself is
stubbed via kbcore.eval_harness so the metrics are deterministic, except one test
that runs the REAL harness to prove search uses embeds+SQL only (no extractor).
"""

from __future__ import annotations

import pytest

from kbcore import db, eval_harness, llm
from kbcore.config import settings
from kbcore.ingest import ingest
from kbcore.items import get_item
from kbworker import consolidate


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

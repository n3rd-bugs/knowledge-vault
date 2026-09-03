"""Wave H eval/reporting package (review #8 H5 + H7, KB 31857).

H5 — E1 prerequisites: the harness forwards ``kwargs.include_superseded``
(transaction time, beside ``as_of``), and the composition pass records WHICH
hit satisfied each golden case (``eval_satisfier_types`` /
``eval_summary_only_cases`` / ``eval_satisfier_ids``) behind its own guard.
H7 — digest truth-telling: the rerank rollup is fallback > active > off and
the tally renders VERBATIM (alert tier on a fallback night), a battery pause /
swap > 70% / the miss counters are alert-class, the honest ``drained_deferred``
line, zero-count steps dropped from the stopped line, the RESUME line for a
resumed pause, a run-duration line, and the shared LOCAL-night rule.

Hermetic where the code is pure (digest_lines, the rollup, the rider on stub
hits); real kb_test DB (conftest's deterministic embeddings) where the harness
or search is exercised end to end.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kbcore import eval_harness, rerank, supersede
from kbcore.ingest import ingest
from kbworker import digest as digest_mod
from kbworker import digest_lines, eval_composition, eval_sentinel, eval_trust
from tests.conftest import DB_UP

needs_db = pytest.mark.skipif(not DB_UP, reason="compose Postgres not reachable")
PKT = timezone(timedelta(hours=5))  # the deployment's zone, pinned (never the box's)


@pytest.fixture(autouse=True)
def _fresh_rerank_state():
    """Participation marker AND tally are thread-local and outlive tests."""
    rerank.reset_outcome()
    rerank._participation.tally = None
    yield
    rerank.reset_outcome()
    rerank._participation.tally = None


# --- H5a: include_superseded joins the allowed case kwargs --------------------


def test_include_superseded_is_an_allowed_kwarg_and_coerces_to_bool():
    assert "include_superseded" in eval_harness.ALLOWED_CASE_KWARGS
    assert eval_harness._search_kwargs({"q": "x", "kwargs": {"include_superseded": True}}) == {
        "include_superseded": True
    }
    assert eval_harness._search_kwargs({"q": "x", "kwargs": {"include_superseded": 0}}) == {
        "include_superseded": False
    }
    with pytest.raises(ValueError, match="unknown kwargs"):
        eval_harness._search_kwargs({"q": "x", "kwargs": {"include_supersede": True}})


@needs_db
def test_include_superseded_reaches_a_superseded_revision_end_to_end(kb):
    """Transaction time: default search hides the superseded revision (forbid
    passes), ``include_superseded: true`` recovers it, and the same substring
    case WITHOUT the flag fails — the flag really reaches search()."""
    old = ingest("alpha", "note", "widget limits v1", "ZOLDREV77 legacy limits.", uri="bt:1")
    supersede.supersede(old, new_body="ZNEWREV88 current limits.", reason="revised")
    golden = [
        {"q": "ZOLDREV77", "forbid_item_id": old},
        {"q": "ZOLDREV77", "expect_substr": ["ZOLDREV77"], "kwargs": {"include_superseded": True}},
        {"q": "ZOLDREV77", "expect_substr": ["ZOLDREV77"]},
    ]
    results, _hit5, _mrr = eval_harness.evaluate(golden=golden)
    assert [r.passed for r in results] == [True, True, False]
    assert results[1].rank == 1, "the superseded revision is the only ZOLDREV77 row"


# --- H5b: satisfier telemetry -------------------------------------------------


def _blind(item_id: int) -> int:
    """NULL every chunk embedding so the keyword leg alone decides the result set."""
    from kbcore import db

    with db.transaction() as conn:
        conn.execute("UPDATE chunks SET embedding = NULL WHERE item_id = %s", (item_id,))
    return item_id


@needs_db
def test_satisfier_telemetry_classes_summary_only_and_ids(kb, monkeypatch):
    s = _blind(
        ingest(
            "kb-system",
            "summary",
            "Widget calibration rollup",
            "The widget calibration rollup summarizes the soak.",
            kind="chat",
            uri="sat:s",
            trust="unverified",
        )
    )
    n = _blind(
        ingest(
            "kb-system",
            "note",
            "Widget calibration observation",
            "A widget calibration bench observation.",
            kind="chat",
            uri="sat:n",
            trust="unverified",
        )
    )
    v = _blind(
        ingest(
            "kb-system",
            "decision",
            "Widget calibration decision",
            "The widget calibration routine takes the median.",
            kind="chat",
            uri="sat:v",
            trust="verified",
        )
    )
    golden = [
        {"q": "widget calibration", "expect_substr": ["calibration"]},  # all three match
        {"q": "widget calibration rollup", "expect_substr": ["rollup"]},  # ONLY the summary
        {"q": "widget calibration", "expect_item_id": v},  # the id owns the case
        {"q": "widget calibration", "expect_substr": ["zzz-no-such-token"]},  # unsatisfied
        {"q": "widget calibration", "forbid_item_id": 999_999},  # forbid-only: skipped
    ]
    monkeypatch.setattr(eval_sentinel.eval_harness, "load_golden", lambda golden_path=None: golden)
    counts: dict = {}
    eval_sentinel.run_sentinel(counts)

    assert counts["eval_status"] == "ok"
    assert counts["eval_satisfier_status"] == "ok"
    types = counts["eval_satisfier_types"]
    assert set(types) == {"summary", "verified", "unverified", "unsatisfied"}, "stable shape"
    assert sum(types.values()) == 4, "forbid-only cases are neither satisfied nor unsatisfied"
    assert types["unsatisfied"] == 1
    assert types["verified"] >= 1, "the expect_item_id case is satisfied by the verified item"
    assert types["summary"] >= 1, "the rollup case is satisfied by the summary"
    assert counts["eval_summary_only_cases"] == 1, "only the rollup case is summary-only"
    assert s in counts["eval_satisfier_ids"] and v in counts["eval_satisfier_ids"]
    assert set(counts["eval_satisfier_ids"]) <= {s, n, v}, "only first satisfiers, only ours"
    # The composition shares still compute on the same pass, untouched.
    assert counts["eval_top5_results"] >= 3


def _stub_hit(item_id: int, title: str, type_: str = "note", trust: str = "unverified"):
    return SimpleNamespace(item_id=item_id, title=title, snippet="", type=type_, trust=trust)


def test_satisfier_rider_matches_stub_hits_on_title_and_never_costs_the_shares(monkeypatch):
    """Hermetic: stub ids have no row, so body matching degrades to title+snippet;
    a broken rider costs eval_satisfier_status only, never the composition."""
    hits = [_stub_hit(1, "Gamma summary", "summary"), _stub_hit(2, "Gamma note")]
    monkeypatch.setattr(eval_sentinel, "search", lambda q, k, tool: list(hits))
    monkeypatch.setattr(eval_trust, "verified_lift", lambda share, n: {})
    golden = [
        {"q": "gamma", "expect_substr": ["Gamma"]},  # both match: not summary-only
        {"q": "gamma", "expect_substr": ["summary"]},  # only the summary matches
        {"q": "gamma", "expect_substr": ["nope"]},
    ]
    result = eval_composition._trust_composition(golden)
    assert result["eval_satisfier_status"] == "ok"
    assert result["eval_satisfier_types"] == {
        "summary": 2,
        "verified": 0,
        "unverified": 0,
        "unsatisfied": 1,
    }
    assert result["eval_summary_only_cases"] == 1
    assert result["eval_satisfier_ids"] == [1]
    assert result["eval_top5_summary_share"] == 0.5, "shares unchanged by the rider"

    def _boom(case, hits):
        raise RuntimeError("rider broke")

    monkeypatch.setattr(eval_composition, "_matching_hits", _boom)
    broken = eval_composition._trust_composition(golden)
    assert broken["eval_satisfier_status"] == "error"
    assert "eval_satisfier_types" not in broken
    assert broken["eval_top5_results"] == 6, "the shares survive a broken rider"


def test_satisfier_class_summary_beats_trust():
    assert eval_composition._satisfier_class(_stub_hit(1, "t", "summary", "verified")) == "summary"
    assert eval_composition._satisfier_class(_stub_hit(1, "t", "note", "verified")) == "verified"
    assert (
        eval_composition._satisfier_class(_stub_hit(1, "t", "note", "unverified")) == "unverified"
    )


# --- H7a: the rerank tally renders VERBATIM, alert tier on a fallback night -----


def test_rerank_tally_line_is_verbatim_and_tier_follows_fallback():
    clean = {"queued_drained": 1, "eval_rerank_tally": {"active": 51, "fallback": 0, "off": 1}}
    lines = digest_mod.digest_lines(clean)
    assert lines == ["drained: 1", "rerank: active 51 / fallback 0 / off 1"], "a routine fact"
    fell = {"queued_drained": 1, "eval_rerank_tally": {"active": 50, "fallback": 2, "off": 0}}
    assert digest_mod.digest_lines(fell)[0] == "rerank: active 50 / fallback 2 / off 0", (
        "a fallback night carries the tally in the ALERT tier, before the facts"
    )
    for counts in (
        {"queued_drained": 1},
        {"queued_drained": 1, "eval_rerank_tally": {"active": "x"}},
        {"queued_drained": 1, "eval_rerank_outcome": "off"},  # the scalar alone renders nothing
    ):
        assert not any(ln.startswith("rerank:") for ln in digest_mod.digest_lines(counts))


# --- H7b: alert-tier promotions -------------------------------------------------


def test_swap_is_alert_class_past_70_percent_only():
    lines = digest_mod.digest_lines({"queued_drained": 1, "swap_used_pct": 71.0})
    assert lines == ["swap: 71% used", "drained: 1"]
    assert digest_mod.digest_lines({"queued_drained": 1, "swap_used_pct": 70.0}) == ["drained: 1"]


def test_miss_counters_and_battery_pause_are_alert_class_but_a_deadline_cut_is_a_fact():
    counts = {
        "queued_drained": 1,
        "search_misses_24h": 4,
        "search_misses_thin_24h": 2,
        "stopped": {"evidence_refreshed": {"reason": "battery", "deferred": 20}},
    }
    lines = digest_mod.digest_lines(counts)
    assert lines[:3] == [
        "paused (battery): evidence_refreshed(20)",
        "empty-result searches: 4",
        "thin-result searches: 2",
    ]
    assert lines[3] == "drained: 1"
    deadline = digest_mod.digest_lines(
        {
            "queued_drained": 1,
            "stopped": {"summaries_written": {"reason": "deadline", "deferred": 4}},
        }
    )
    assert deadline == ["drained: 1", "stopped early (deadline): summaries_written(4)"]


# --- H7e: zero-count steps drop out of the stopped line -----------------------


def test_stopped_line_drops_zero_parts_but_still_names_an_all_zero_cut():
    stopped = {
        "summaries_written": {"reason": "battery", "deferred": 0},
        "evidence_refreshed": {"reason": "battery", "deferred": 20},
        "contradictions_queued": {"reason": "battery", "deferred": 0},
    }
    assert digest_lines._stopped_line(stopped) == "paused (battery): evidence_refreshed(20)"
    all_zero = {"summaries_written": {"reason": "deadline", "deferred": 0}}
    assert digest_lines._stopped_line(all_zero) == "stopped early (deadline): summaries_written"
    unknown = {"x": {"reason": "deadline"}}
    assert digest_lines._stopped_line(unknown) == "stopped early (deadline): x(?)"


# --- H6/H7 contract: the honest drained_deferred line ---------------------------


def test_drain_deferred_line_wording_and_shape_tolerance():
    assert digest_lines._drain_deferred_line({"drained_deferred": 7}) == "drain: 7 deferred"
    for counts in (
        {"drained_deferred": 0},
        {"drained_deferred": True},
        {"drained_deferred": "7"},
        {},
    ):
        assert digest_lines._drain_deferred_line(counts) is None


# --- H7d: RESUME line + the shared LOCAL-night rule ------------------------------


def test_night_of_files_pre_dawn_runs_under_the_previous_night():
    assert digest_lines.night_of(datetime(2026, 8, 29, 20, 30, tzinfo=UTC), PKT) == date(
        2026, 8, 29
    ), "01:30 local Sunday morning is still Saturday night"
    assert digest_lines.night_of(datetime(2026, 8, 30, 5, 0, tzinfo=UTC), PKT) == date(
        2026, 8, 30
    ), "a 10:00 local boot catch-up is its own day"
    assert digest_lines.night_of(datetime(2026, 8, 30, 1, 0), PKT) == date(2026, 8, 30), (
        "a naive instant is UTC (Invariant 7): 06:00 local is the rollover"
    )
    assert digest_lines.night_of(datetime(2026, 8, 30, 0, 59), PKT) == date(2026, 8, 29)


def test_resume_line_renders_the_prior_marker_local_and_first():
    counts = {
        "queued_drained": 3,
        "resumed_from_pause": {"at": "2026-08-29T20:59:00+00:00", "deferred": 41},
    }
    assert digest_lines._resume_line(counts, tz=PKT) == (
        "RESUME of 2026-08-29 (paused 01:59 local, 41 deferred)"
    )
    lines = digest_mod.digest_lines(counts)
    assert lines[0].startswith("RESUME of ") and lines[1] == "drained: 3"
    for counts in (
        {"queued_drained": 1},
        {"queued_drained": 1, "resumed_from_pause": None},
        {"queued_drained": 1, "resumed_from_pause": {"at": "not-a-time"}},
    ):
        assert digest_mod.digest_lines(counts) == ["drained: 1"]
    unknown_count = {"resumed_from_pause": {"at": "2026-08-29T20:59:00+00:00"}}
    assert digest_lines._resume_line(unknown_count, tz=PKT).endswith(
        "(paused 01:59 local, ? deferred)"
    )


# --- H13c digest half: promote-run ready --------------------------------------


def test_promote_runs_stash_and_line_are_fail_open_and_positive_only(monkeypatch, caplog):
    from kbcore import review_promote_runs

    summary = {
        "runs": 2,
        "items": 9,
        "largest_key": "session:box-a\n2026-09-02T20:00:00+00:00" + "x" * 80,
        "largest_items": 6,
    }
    monkeypatch.setattr(review_promote_runs, "promote_runs_summary", lambda days=1: summary)
    counts: dict = {"queued_drained": 1}
    digest_mod._note_promote_runs(counts)
    assert counts["promote_runs_ready"] == summary, "an ADDED key, the whole summary"
    lines = digest_mod.digest_lines(counts)
    line = next(ln for ln in lines if ln.startswith("promote-run ready:"))
    assert line.startswith("promote-run ready: 2 runs / 9 items (largest: session:box-a 2026-09-02")
    assert line.endswith(", 6 items)") and line.isascii()
    assert "x" * 60 not in line, "the run key is bounded for the phone"
    assert "\n" not in line

    quiet = {"queued_drained": 1, "promote_runs_ready": {"runs": 0, "items": 0}}
    assert digest_mod.digest_lines(quiet) == ["drained: 1"], "zero ready runs render nothing"

    def _boom(days=1):
        raise RuntimeError("no such table")

    monkeypatch.setattr(review_promote_runs, "promote_runs_summary", _boom)
    broken: dict = {"queued_drained": 1}
    digest_mod._note_promote_runs(broken)
    assert "promote_runs_ready" not in broken
    assert "promote-run summary unavailable" in caplog.text
    assert digest_mod.digest_lines(broken) == ["drained: 1"]


# --- run duration --------------------------------------------------------------


def test_run_duration_line_prefers_wall_clock_and_labels_the_ledger_honestly():
    ledger = {"step_secs": {"a": 3600.0, "b": 720.0, "eval_sentinel": 90.4, "flag": True}}
    assert digest_lines._run_duration_line(ledger) == "steps timed: 1h 13m over 3"
    wall = {**ledger, "run_secs": 5.0 * 3600 + 12 * 60}
    assert digest_lines._run_duration_line(wall) == "run: 5h 12m (3 steps timed)"
    assert digest_lines._run_duration_line({"step_secs": {"a": 42}}) == "steps timed: 0m 42s over 1"
    for counts in ({}, {"step_secs": {}}, {"step_secs": "x"}, {"run_secs": 0}):
        assert digest_lines._run_duration_line(counts) is None
    lines = digest_mod.digest_lines({"queued_drained": 1, **ledger})
    assert lines[-1] == "steps timed: 1h 13m over 3", "closes the facts"


def test_every_new_digest_line_is_ascii_and_the_cap_still_holds(caplog):
    counts = {
        "eval_rerank_tally": {"active": 1, "fallback": 1, "off": 0},
        "swap_used_pct": 88.2,
        "search_misses_24h": 1,
        "stopped": {"s": {"reason": "battery", "deferred": 1}},
        "resumed_from_pause": {"at": "2026-08-29T20:59:00+00:00", "deferred": 2},
        "drained_deferred": 3,
        "step_secs": {"a": 10},
        **{f"k{i}": i for i in range(30)},
    }
    lines = digest_mod.digest_lines(counts)
    assert all(ln.isascii() for ln in lines)
    assert len(lines) <= digest_mod.DIGEST_MAX_LINES
    logging.getLogger(__name__).debug("%s", lines)  # keep caplog wired for future riders

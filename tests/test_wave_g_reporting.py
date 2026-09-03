"""Wave G reporting package (review #6): digest hygiene (alerts first, hard
20-line cap, wallpaper retirement, battery-stop line), expiry cap-saturation
visibility, the drained_deferred contract line, lift components rendered beside
the lift + the WoW trip repointed at the numerator (see also
test_wave_f_reporting), the weekly stocks block, the per-case rerank outcome
tally, and the sentinel's self-timed cost-ledger entry.

Everything here is unit-level: digest_lines and the weekly renderers are pure
functions, the sentinel runs with its DB-touching detector/instruments patched
out, and the rerank tally is thread-local state — no DB, no sidecar, no LLM.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kbcore import rerank
from kbcore.config import settings
from kbworker import digest as digest_mod
from kbworker import eval_sentinel, weekly_render


@pytest.fixture(autouse=True)
def _fresh_rerank_state():
    """Participation marker AND tally are thread-local and outlive tests."""
    rerank.reset_outcome()
    rerank._participation.tally = None
    yield
    rerank.reset_outcome()
    rerank._participation.tally = None


# --- digest hygiene: alerts first + the hard cap ------------------------------

FULL_NIGHT = {
    # ALERT-class (5)
    "eval_regression": True,
    "eval_hit_at_5": 0.5,
    "eval_trust_status": "watch",
    "eval_top5_unverified_share": 0.8,
    "eval_top5_verified_lift": 2.0,
    "eval_top5_verified": 2,
    "eval_top5_results": 10,
    "eval_corpus_verified_fraction": 0.05,
    "untriaged_expiring_7d": 3,
    "fleet_degenerate_by_project": {"alpha": {"degenerate": 1, "total": 1}},
    "deferral_escalations": 2,
    # facts (22 possible)
    "queued_drained": 1,
    "queued_items_saved": 2,
    "review_triaged": 3,
    "auto_applied": 4,
    "contradiction_pairs_judged": 5,
    "groups_near_deferral_cap": 1,
    "drained_deferred": 6,
    "attempts_cap_retirements": 1,
    "wall_blocked_closed": 2,
    "review_triaged_topup": 3,
    "auto_apply_counts": {
        "triage_invalidated": 1,
        "triage_unresolvable": 2,
        "skipped_cluster_winner": 3,
        "cluster_winners_closed": 1,
    },
    "review_expired": 500,
    "quiet_expiry_counts": {"expired": 500, "eligible": 1267, "eligible_remaining": 767},
    "never_triaged_open": 100,
    "promote_candidates": 9,
    "search_misses_24h": 4,
    "search_misses_thin_24h": 2,
    "swap_used_pct": 90.0,
    "rerank_probe": "failed",
    "stopped": {"evidence_refreshed": {"reason": "battery", "deferred": 20}},
    # review #8 H7a: a fallback night carries its tally in the alert tier
    "eval_rerank_tally": {"active": 50, "fallback": 2, "off": 0},
}


def test_alert_class_lines_render_first_and_the_digest_is_hard_capped():
    lines = digest_mod.digest_lines(FULL_NIGHT)
    assert len(lines) == digest_mod.DIGEST_MAX_LINES == 20, "hard cap (review #6)"
    assert lines[0] == "EVAL RED: hit@5 0.5 (floor 0.75)", "conftest pins the floor"
    assert lines[1].startswith("TRUST WATCH:")
    assert lines[2] == "EXPIRY: 3 untriaged rows reach expiry age within 7d"
    assert lines[3] == "STALE HOOKS suspected: alpha"
    assert lines[4] == "drain: 2 row(s) escalated to failed at the deferral cap"
    # Review #8 H7b: rerank fallback, the battery PAUSE, swap and the miss
    # counters are alert-class now, so the 20-line cap trims only routine facts.
    assert lines[5] == "rerank: active 50 / fallback 2 / off 0"
    assert lines[6] == "paused (battery): evidence_refreshed(20)"
    assert lines[7] == "swap: 90% used"
    assert lines[8] == "empty-result searches: 4"
    assert lines[9] == "thin-result searches: 2"
    assert lines[10] == "drained: 1", "facts follow the alerts in their old order"
    assert digest_mod._alert_lines(FULL_NIGHT) == lines[:10], "the cap never trims an alert"
    # On this 30-line fixture the cap trims ROUTINE facts (the saturation line
    # among them) — it is still produced, and it survives any ordinary night.
    assert "expired: 500 (cap; 767 still eligible)" in digest_mod._fact_lines(FULL_NIGHT)
    assert all(line.isascii() for line in lines), "ntfy to a phone: pure ASCII"


def test_a_quiet_night_is_unchanged_by_the_ordering():
    assert digest_mod.digest_lines({"queued_drained": 7}) == ["drained: 7"]


def test_the_wallpaper_expiry_line_is_retired():
    """'review rows start expiring <date>' was permanently true post-rails."""
    near = (datetime.now().date() + timedelta(days=2)).isoformat()
    lines = digest_mod.digest_lines({"queued_drained": 1, "first_expiry_date": near})
    assert lines == ["drained: 1"]


# --- expiry cap-saturation (review #6 contract) -------------------------------


def test_expired_line_names_cap_saturation_when_rows_remain_eligible():
    lines = digest_mod.digest_lines(
        {
            "review_expired": 500,
            "quiet_expiry_counts": {"expired": 500, "eligible": 1267, "eligible_remaining": 767},
        }
    )
    assert "expired: 500 (cap; 767 still eligible)" in lines


def test_expired_line_stays_plain_when_the_cap_did_not_bind():
    for counts in (
        {"review_expired": 7, "quiet_expiry_counts": {"eligible_remaining": 0}},
        {"review_expired": 7},  # step failed to stash the breakdown
    ):
        assert "expired: 7" in digest_mod.digest_lines(counts)
    quiet = digest_mod.digest_lines(
        {"review_expired": 0, "quiet_expiry_counts": {"eligible_remaining": 5}}
    )
    assert not any(line.startswith("expired:") for line in quiet), "positive-only"


# --- drained_deferred (review #6 cross-package contract) ----------------------


def test_drained_deferred_renders_the_contract_line_positive_only():
    # Wording since review #8 H6/H7: the count is honest on EVERY stop path
    # (budget, guard, pre-first-cohort), so the line no longer names the budget.
    lines = digest_mod.digest_lines({"queued_drained": 1, "drained_deferred": 12})
    assert "drain: 12 deferred" in lines
    for counts in ({"queued_drained": 1, "drained_deferred": 0}, {"queued_drained": 1}):
        assert not any("deferred" in ln for ln in digest_mod.digest_lines(counts))


# --- battery-pause line -------------------------------------------------------
# Wording change 2026-08-29 (pause-not-stopped): a battery cut renders 'paused',
# because grid restore (kb-ac-restore.timer) or the next nightly resumes it.
# The 'stopped' counts key and the reason STRINGS are data and stay verbatim.


def test_stopped_steps_render_one_line_with_reason_and_deferred_counts():
    lines = digest_mod.digest_lines(
        {
            "queued_drained": 1,
            "stopped": {
                "evidence_refreshed": {"reason": "battery", "deferred": 20},
                "contradictions_queued": {"reason": "battery", "deferred": 2},
            },
        }
    )
    assert "paused (battery): evidence_refreshed(20), contradictions_queued(2)" in lines
    assert all(line.isascii() for line in lines)


def test_stopped_line_joins_distinct_reasons_and_is_positive_only():
    lines = digest_mod.digest_lines(
        {
            "stopped": {
                "summaries_written": {"reason": "deadline", "deferred": 4},
                "evidence_refreshed": {"reason": "battery", "deferred": 1},
            }
        }
    )
    assert "paused (deadline/battery): summaries_written(4), evidence_refreshed(1)" in lines
    for counts in ({"queued_drained": 1, "stopped": {}}, {"queued_drained": 1}):
        rendered = digest_mod.digest_lines(counts)
        assert not any(ln.startswith(("stopped early", "paused")) for ln in rendered)


def test_a_pure_deadline_stop_still_renders_stopped_early():
    """Only a battery cut is a 'pause' — the AC-restore timer will not resume a
    deadline stop, so its line must not promise what nothing delivers."""
    lines = digest_mod.digest_lines(
        {"stopped": {"summaries_written": {"reason": "deadline", "deferred": 4}}}
    )
    assert "stopped early (deadline): summaries_written(4)" in lines


# --- TRUST WATCH renders the lift components ----------------------------------


def test_trust_watch_line_carries_numerator_and_denominator():
    lines = digest_mod.digest_lines(
        {
            "eval_trust_status": "watch",
            "eval_top5_unverified_share": 0.65,
            "eval_top5_verified_lift": 10.8,
            "eval_top5_verified": 12,
            "eval_top5_results": 260,
            "eval_corpus_verified_fraction": 0.0344,
        }
    )
    assert (
        "TRUST WATCH: top5 unverified 0.65, verified lift 10.8x (v-top5 12/260, corpus 0.0344)"
        in lines
    )


def test_trust_watch_derives_the_count_from_parts_and_degrades_bare():
    # Pre-Wave-G meta: only the parts — the numerator is derived (91 - 60 = 31).
    lines = digest_mod.digest_lines(
        {
            "eval_trust_status": "watch",
            "eval_top5_unverified_share": 0.66,
            "eval_top5_verified_lift": 9.0,
            "eval_top5_unverified": 60,
            "eval_top5_results": 91,
            "eval_corpus_verified_fraction": 0.04,
        }
    )
    assert any("(v-top5 31/91, corpus 0.04)" in ln for ln in lines)
    # No components at all: the bare pre-Wave-G line renders unchanged.
    bare = digest_mod.digest_lines(
        {
            "eval_trust_status": "watch",
            "eval_top5_unverified_share": 0.9,
            "eval_top5_verified_lift": 3.1,
        }
    )
    assert "TRUST WATCH: top5 unverified 0.9, verified lift 3.1x" in bare


# --- weekly: stocks block, expired column, push additions ---------------------


def _report(day: int, **meta):
    return (datetime(2026, 8, day, 2, 0), meta)


WEEK = [
    _report(
        20,
        queued_drained=3,
        review_expired=2,
        review_queue_open=14981,
        never_triaged_open=100,
        reject_suggestions_pending=395,
        eval_top5_unverified_share=0.64,
        eval_top5_unverified=250,
        eval_top5_results=260,  # verified DERIVED: 260 - 250 = 10 (pre-Wave-G meta)
        eval_corpus_verified_fraction=0.041,
        eval_top5_verified_lift=5.4,
    ),
    _report(
        26,
        queued_drained=5,
        review_expired=500,
        review_queue_open=17330,
        never_triaged_open=160,
        reject_suggestions_pending=1264,
        eval_top5_unverified_share=0.65,
        eval_top5_verified=9,  # Wave-G meta persists the numerator explicitly
        eval_top5_results=260,
        eval_corpus_verified_fraction=0.034,
        eval_top5_verified_lift=10.8,
    ),
]
MISS_STATS = {"total": 0, "distinct": 0, "empty": 0, "thin": 0}
NOW = datetime(2026, 8, 27, 1, 30)
# Rows are grouped by LOCAL night since review #8 H9f; pin the deployment's zone
# so the 02:00Z fixtures file under their own date on any box (07:00 local).
PKT = timezone(timedelta(hours=5))


def test_weekly_body_gains_the_stocks_block_and_the_expired_column():
    body = weekly_render._report_body(NOW, WEEK, MISS_STATS, [], 0, 0, None, 0, [], tz=PKT)
    lines = body.splitlines()
    header = next(ln for ln in lines if ln.startswith("night"))
    for col in ("expired", "v-top5", "corp-v"):
        assert col in header
    row1 = next(ln for ln in lines if ln.startswith("2026-08-20"))
    assert "0.041" in row1 and "10" in row1, "derived numerator + denominator per night"
    row2 = next(ln for ln in lines if ln.startswith("2026-08-26"))
    assert "500" in row2 and "0.034" in row2
    assert "stocks (first -> last over the window):" in lines
    stocks = "\n".join(lines)
    assert "review queue open:" in stocks and "14,981 -> 17,330" in stocks
    assert "never-triaged open:" in stocks and "100 -> 160" in stocks
    assert "expired/night:" in stocks and "2 -> 500" in stocks
    assert "reject suggestions open:" in stocks and "395 -> 1,264" in stocks


def test_weekly_push_gains_one_stocks_line_and_lift_components_stay_inline():
    lines = weekly_render._push_lines(NOW, WEEK, MISS_STATS, [], 0, None, 42, tz=PKT)
    assert "queue open: 14,981 -> 17,330" in lines, "exactly the ONE budgeted stocks line"
    assert sum("queue open" in ln for ln in lines) == 1
    assert "verified lift: 5.400 -> 10.800 (v-top5 10 -> 9, corpus 0.041 -> 0.034)" in lines, (
        "the lift never renders without its components (review #6)"
    )
    assert len(lines) <= weekly_render.PUSH_MAX_LINES
    assert all(ln.isascii() for ln in lines)


def test_trend_int_and_verified_top5_degrade_to_na_and_none():
    assert weekly_render._trend_int([]) == "n/a"
    assert weekly_render._trend_int([7.0]) == "7"
    assert weekly_render._verified_top5({}) is None
    assert weekly_render._verified_top5({"eval_top5_results": 5}) is None, "parts incomplete"
    assert weekly_render._verified_top5({"eval_top5_verified": 4}) == 4


# --- rerank: the per-case outcome tally ---------------------------------------


def test_tally_is_unarmed_by_default_and_off_is_derived():
    rerank._note("active", 0.9)  # production search outside an eval window
    assert rerank.outcome_tally(3) == {"active": 0, "fallback": 0, "off": 3}


def test_armed_tally_counts_only_final_outcomes():
    rerank.reset_tally()
    rerank._note("off")  # rerank_fused entry note — never counted
    rerank._note("active", 0.5)
    rerank._note("off")  # reset_outcome between searches — never counted
    rerank._note("fallback")
    assert rerank.outcome_tally(3) == {"active": 1, "fallback": 1, "off": 1}
    assert rerank.outcome_tally(1)["off"] == 0, "derived off is floored at zero"


def test_rerank_fused_paths_feed_the_armed_tally(monkeypatch):
    class _Conn:
        def execute(self, *a):
            class _R:
                @staticmethod
                def fetchall():
                    return [(1, "t1", "b1"), (2, "t2", "b2")]

            return _R()

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "results": [
                    {"index": 0, "relevance_score": 0.2},
                    {"index": 1, "relevance_score": 0.7},
                ]
            }

    fused = [(1, 0.5, "s1"), (2, 0.3, "s2")]
    monkeypatch.setattr(settings, "kb_rerank_url", "http://127.0.0.1:9")
    rerank.reset_tally()
    monkeypatch.setattr(rerank.httpx, "post", lambda url, *, json=None, timeout=None: _Resp())
    rerank.rerank_fused(_Conn(), "q", fused, top_n=5)  # active

    def _boom(*a, **kw):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(rerank.httpx, "post", _boom)
    rerank.rerank_fused(_Conn(), "q", fused, top_n=5)  # fallback
    rerank.rerank_fused(_Conn(), "q", fused[:1], top_n=5)  # nothing to rerank: off
    assert rerank.outcome_tally(3) == {"active": 1, "fallback": 1, "off": 1}


# --- sentinel: tally persistence + the cost-ledger entry ----------------------


@pytest.fixture()
def _quiet_sentinel(monkeypatch):
    """Silence the sentinel's DB surfaces so these stay unit-level."""
    monkeypatch.setattr(eval_sentinel, "TRUST_METRIC_ENABLED", False)
    monkeypatch.setattr(eval_sentinel, "_detect_regression", lambda counts, h, m: None)


def _stub_golden(monkeypatch, n: int) -> None:
    golden = [{"q": f"q{i}", "expect_substr": ["x"]} for i in range(n)]
    monkeypatch.setattr(eval_sentinel.eval_harness, "load_golden", lambda golden_path=None: golden)


def test_sentinel_persists_the_per_case_tally_and_the_worst_scalar(monkeypatch, _quiet_sentinel):
    _stub_golden(monkeypatch, 4)

    def fake_evaluate(golden=None, golden_path=None):
        for i in range(len(golden)):  # one final outcome per case, worst first
            rerank._note("fallback" if i == 0 else "active", 0.5)
        return ([], 1.0, 0.9)

    monkeypatch.setattr(eval_sentinel.eval_harness, "evaluate", fake_evaluate)
    counts: dict = {}
    eval_sentinel.run_sentinel(counts)
    assert counts["eval_status"] == "ok"
    assert counts["eval_rerank_tally"] == {"active": 3, "fallback": 1, "off": 0}
    assert counts["eval_rerank_outcome"] == "fallback", "WORST observed, not the last case"


def test_sentinel_reports_off_when_no_case_reranked(monkeypatch, _quiet_sentinel):
    _stub_golden(monkeypatch, 5)
    monkeypatch.setattr(
        eval_sentinel.eval_harness,
        "evaluate",
        lambda golden=None, golden_path=None: ([], 1.0, 0.9),
    )
    counts: dict = {}
    eval_sentinel.run_sentinel(counts)
    assert counts["eval_rerank_tally"] == {"active": 0, "fallback": 0, "off": 5}
    assert counts["eval_rerank_outcome"] == "off", "'off' now means NO case reranked"


def test_sentinel_reports_active_when_cases_reranked_without_fallback(monkeypatch, _quiet_sentinel):
    """Review #8 H7a: rollup fallback > active > off. One golden case structurally
    has <2 fused candidates, so the review #6 order (fallback > off > active)
    read 'off' on every healthy night and hid the sidecar's activity."""
    _stub_golden(monkeypatch, 5)

    def fake_evaluate(golden=None, golden_path=None):
        for _ in range(4):
            rerank._note("active", 0.5)
        return ([], 1.0, 0.9)

    monkeypatch.setattr(eval_sentinel.eval_harness, "evaluate", fake_evaluate)
    counts: dict = {}
    eval_sentinel.run_sentinel(counts)
    assert counts["eval_rerank_tally"] == {"active": 4, "fallback": 0, "off": 1}
    assert counts["eval_rerank_outcome"] == "active"


def test_sentinel_self_times_into_the_step_secs_ledger(monkeypatch, _quiet_sentinel):
    _stub_golden(monkeypatch, 1)
    monkeypatch.setattr(
        eval_sentinel.eval_harness,
        "evaluate",
        lambda golden=None, golden_path=None: ([], 1.0, 0.9),
    )
    counts: dict = {}
    eval_sentinel.run_sentinel(counts)
    assert counts["step_secs"]["eval_sentinel"] >= 0.0, "the eval joins the cost ledger"
    # The kill switch is timed too — a ledger hole would read as a missing step.
    monkeypatch.setattr(settings, "kb_eval_in_consolidation", False)
    disabled: dict = {}
    eval_sentinel.run_sentinel(disabled)
    assert disabled["eval_status"] == "disabled"
    assert disabled["step_secs"]["eval_sentinel"] >= 0.0

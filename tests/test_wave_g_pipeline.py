"""Wave G pipeline package (review #6): contradiction weekday day-cap, day-aware
triage batch, weekday drain budget + drained_deferred, group-atomic escalation,
the reembed gate, the summaries since-mark fix, and the tiled fleet window.

Hermeticity: every calendar-sensitive branch is either day-pinned (an explicit
``now``) or made day-independent by pinning the deadline day list to all-days /
no-days — never read from the box's .env.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from kbcore import consolidation, drainsched, fleet_health, ingest_queue
from kbcore import reembed as reembed_mod
from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.errors import ExtractorUnavailable
from kbcore.ingest import ingest
from kbworker import consolidate, sessions, tail_ops, tail_steps

MON = datetime(2026, 8, 17)  # weekday() == 0
SAT = datetime(2026, 8, 22)  # weekday() == 5


@pytest.fixture()
def _weekday_deadline(monkeypatch):
    """The live shape, hermetically: the noon deadline applies Mon-Fri."""
    monkeypatch.setattr(settings, "kb_drain_deadline", "12:00")
    monkeypatch.setattr(settings, "kb_drain_deadline_days", "mon,tue,wed,thu,fri")


@pytest.fixture()
def _every_day_bounded(monkeypatch):
    """Day-independent 'deadline day': the deadline applies every day, so the
    weekday branch is taken no matter which day the suite runs on."""
    monkeypatch.setattr(settings, "kb_drain_deadline", "12:00")
    monkeypatch.setattr(settings, "kb_drain_deadline_days", "mon,tue,wed,thu,fri,sat,sun")


@pytest.fixture()
def _no_stand_down(monkeypatch):
    """Disarm the run guards (drain-refill suite pattern): no deadline, no power
    check, clean stop bookkeeping — day-independent."""
    monkeypatch.setattr(settings, "kb_drain_deadline", "")
    monkeypatch.setattr(settings, "kb_drain_deadline_days", "")
    monkeypatch.setattr(settings, "kb_power_db", "")
    consolidate.reset_stop_cache()
    consolidate._STOP_LOG.clear()
    yield
    consolidate.reset_stop_cache()
    consolidate._STOP_LOG.clear()


# ------------------------------ item 1: contradiction weekday day-cap (40/500)


def test_contradiction_cap_is_40_on_a_deadline_day(_weekday_deadline, monkeypatch):
    monkeypatch.setattr(settings, "kb_contradiction_pair_cap", 500)
    monkeypatch.setattr(settings, "kb_contradiction_pair_cap_weekday", 40)
    assert MON.weekday() == 0  # day-pin guard
    assert tail_ops._contradiction_pair_cap(MON) == 40


def test_contradiction_cap_keeps_500_on_an_unbounded_day(_weekday_deadline, monkeypatch):
    monkeypatch.setattr(settings, "kb_contradiction_pair_cap", 500)
    monkeypatch.setattr(settings, "kb_contradiction_pair_cap_weekday", 40)
    assert SAT.weekday() == 5  # day-pin guard
    assert tail_ops._contradiction_pair_cap(SAT) == 500


def test_contradiction_cap_min_keeps_both_kill_switches(_weekday_deadline, monkeypatch):
    monkeypatch.setattr(settings, "kb_contradiction_pair_cap_weekday", 40)
    # The main knob stays the master kill: 0 kills weekdays too.
    monkeypatch.setattr(settings, "kb_contradiction_pair_cap", 0)
    assert tail_ops._contradiction_pair_cap(MON) == 0
    assert tail_ops._contradiction_pair_cap(SAT) == 0
    # A main cap LOWER than the weekday knob still governs (min, not swap).
    monkeypatch.setattr(settings, "kb_contradiction_pair_cap", 2)
    assert tail_ops._contradiction_pair_cap(MON) == 2
    # 0 in the weekday knob is a weekday-only kill.
    monkeypatch.setattr(settings, "kb_contradiction_pair_cap", 500)
    monkeypatch.setattr(settings, "kb_contradiction_pair_cap_weekday", 0)
    assert tail_ops._contradiction_pair_cap(MON) == 0
    assert tail_ops._contradiction_pair_cap(SAT) == 500


def test_contradictions_all_hands_the_day_aware_budget_to_the_query(
    _every_day_bounded, monkeypatch
):
    """The budget must reach the sim-DESC LIMIT, so a capped night spends its
    pairs on the closest candidates — not get applied after an unbounded fetch."""
    monkeypatch.setattr(settings, "kb_contradiction_pair_cap", 500)
    monkeypatch.setattr(settings, "kb_contradiction_pair_cap_weekday", 3)
    seen: dict[str, int | None] = {}

    def fake_pairs(project, low, high, limit=None):
        seen[project] = limit
        return []

    monkeypatch.setattr(tail_ops.consolidation, "contradiction_pairs", fake_pairs)
    assert tail_ops._contradictions_all(["p1"], None) == 0
    assert seen == {"p1": 3}


def test_an_explicit_cap_still_overrides_the_calendar(_every_day_bounded, monkeypatch):
    seen: dict[str, int | None] = {}

    def fake_pairs(project, low, high, limit=None):
        seen[project] = limit
        return []

    monkeypatch.setattr(tail_ops.consolidation, "contradiction_pairs", fake_pairs)
    tail_ops._contradictions_all(["p1"], None, cap=7)
    assert seen == {"p1": 7}, "the caller's explicit cap must keep its pre-split meaning"


# ------------------------------------ item 2: day-aware triage batch (300/500)


def test_triage_batch_is_300_on_a_deadline_day(_weekday_deadline, monkeypatch):
    monkeypatch.setattr(settings, "kb_review_triage_batch", 500)
    monkeypatch.setattr(settings, "kb_review_triage_batch_weekday", 300)
    assert drainsched.triage_batch(MON) == 300
    assert drainsched.triage_batch(SAT) == 500


def test_triage_batch_min_keeps_both_kill_switches(_weekday_deadline, monkeypatch):
    monkeypatch.setattr(settings, "kb_review_triage_batch", 100)
    monkeypatch.setattr(settings, "kb_review_triage_batch_weekday", 300)
    assert drainsched.triage_batch(MON) == 100, "a lower master batch still governs"
    monkeypatch.setattr(settings, "kb_review_triage_batch", 0)
    assert drainsched.triage_batch(MON) == 0, "batch=0 stays the master kill switch"
    assert drainsched.triage_batch(SAT) == 0
    monkeypatch.setattr(settings, "kb_review_triage_batch", 500)
    monkeypatch.setattr(settings, "kb_review_triage_batch_weekday", 0)
    assert drainsched.triage_batch(MON) == 0, "weekday knob 0 is a weekday-only kill"
    assert drainsched.triage_batch(SAT) == 500


def test_no_deadline_at_all_means_the_full_batch(monkeypatch):
    monkeypatch.setattr(settings, "kb_drain_deadline", "")
    monkeypatch.setattr(settings, "kb_drain_deadline_days", "")
    monkeypatch.setattr(settings, "kb_review_triage_batch", 500)
    monkeypatch.setattr(settings, "kb_review_triage_batch_weekday", 300)
    assert drainsched.triage_batch(MON) == 500


def test_consolidate_wires_the_day_aware_batch():
    """Mechanical guard (test_box_identity pattern): the nightly step must read
    the day-aware helper, never the raw knob."""
    from pathlib import Path

    source = Path(consolidate.__file__).read_text(encoding="utf-8")
    assert "drainsched.triage_batch()" in source
    assert "settings.kb_review_triage_batch," not in source


# ------------------- item 3: weekday drain budget + drained_deferred counter


def test_drain_budget_reason_fires_only_when_spent_on_a_bounded_day(
    _every_day_bounded, monkeypatch
):
    monkeypatch.setattr(settings, "kb_drain_weekday_budget_minutes", 1)
    spent = time.monotonic() - 120  # a drain that started two minutes ago
    fresh = time.monotonic()
    assert drainsched.drain_budget_reason(spent) == drainsched.DRAIN_BUDGET_REASON
    assert drainsched.drain_budget_reason(fresh) is None, "an unspent budget must not stop"


def test_drain_budget_is_inert_when_disabled_or_unbounded(monkeypatch):
    spent = time.monotonic() - 7200
    monkeypatch.setattr(settings, "kb_drain_deadline", "12:00")
    monkeypatch.setattr(settings, "kb_drain_deadline_days", "mon,tue,wed,thu,fri,sat,sun")
    monkeypatch.setattr(settings, "kb_drain_weekday_budget_minutes", 0)
    assert drainsched.drain_budget_reason(spent) is None, "0 is the kill switch"
    monkeypatch.setattr(settings, "kb_drain_weekday_budget_minutes", 1)
    monkeypatch.setattr(settings, "kb_drain_deadline", "")
    monkeypatch.setattr(settings, "kb_drain_deadline_days", "")
    assert drainsched.drain_budget_reason(spent) is None, "unbounded days are never budgeted"


def _enqueue(project: str, body: str, source_uri: str | None = None) -> int:
    res = ingest_queue.enqueue(project, body, kind="session", source_uri=source_uri)
    assert res.id is not None
    return res.id


def _recorder(monkeypatch, fail_on: dict[str, Exception] | None = None) -> list[str]:
    seen: list[str] = []

    def fake(text, project, uri, **kw):
        seen.append(uri)
        exc = (fail_on or {}).get(uri)
        if exc is not None:
            raise exc
        return 1, 0

    monkeypatch.setattr(sessions, "extract_and_ingest", fake)
    return seen


def test_a_spent_budget_stops_the_drain_and_counts_the_shed(kb, _no_stand_down, monkeypatch):
    """The wiring: a budget stop defers exactly like the deadline (rows stay
    'pending', next run resumes) and writes the contract key drained_deferred —
    the shed volume in still-pending rows, visible instead of inferable from
    finish times."""
    monkeypatch.setattr(settings, "kb_drain_cohort", 1)
    for i in range(3):
        _enqueue("budget", f"User: b{i}\nAssistant: b{i}")
    seen = _recorder(monkeypatch)
    monkeypatch.setattr(
        drainsched,
        "drain_budget_reason",
        lambda started: drainsched.DRAIN_BUDGET_REASON if seen else None,
    )

    drained, saved = consolidate._drain_ingest_queue()

    assert (drained, saved) == (1, 1), "the run's first cohort stays exempt; the second stops"
    assert consolidate._STOP_LOG["queued_drained"]["reason"] == "drain-budget"
    assert consolidate._COHORT_STATS["drained_deferred"] == 2, (
        "the shed must be counted in still-pending rows"
    )
    assert consolidate._COHORT_STATS["deadline_deferred_jobs"] == 1
    assert ingest_queue.queue_depth() == 2, "budget-shed rows stay 'pending', never lost"

    # ...and the next (unbudgeted) run drains the leftovers exactly like a
    # deadline deferral would.
    monkeypatch.setattr(drainsched, "drain_budget_reason", lambda started: None)
    assert consolidate._drain_ingest_queue() == (2, 2)


def test_drained_deferred_is_seeded_zero_on_an_unstopped_run(kb, _no_stand_down, monkeypatch):
    """The contract key must be present (int) every night, not only on shed
    nights — pkg-reporting renders it positive-only."""
    monkeypatch.setattr(consolidate.eval_sentinel, "run_sentinel", lambda counts: None)
    _enqueue("budget-zero", "User: only\nAssistant: one")
    _recorder(monkeypatch)

    result = consolidate.main()

    assert result["drained_deferred"] == 0
    assert result["queued_drained"] == 1


# --------------------------- item 4: group-atomic deferral-cap escalation


def _age_rows(ids: list[int], minutes: int) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET created_at = now() - make_interval(mins => %s) "
            "WHERE id = ANY(%s)",
            (minutes, list(ids)),
        )


def _set_deferrals(ids: list[int], n: int) -> None:
    with transaction() as conn:
        conn.execute("UPDATE ingest_queue SET deferrals = %s WHERE id = ANY(%s)", (n, list(ids)))


def _row(qid: int) -> tuple:
    with connect() as conn:
        return conn.execute(
            "SELECT status, attempts, deferrals, error FROM ingest_queue WHERE id = %s", (qid,)
        ).fetchone()


def test_a_partial_escalation_retires_the_whole_group(kb, _no_stand_down, monkeypatch):
    """The split-coalesced-group hazard (review #6): rows join a session on
    different nights, so their deferral counters diverge — one row escalating at
    the cap while its siblings stay 'pending' would extract the session in
    pieces next run. The group must retire as a unit, attempts pinned."""
    a = _enqueue("atomic", "chunk one\n", source_uri="session:atomic")
    b = _enqueue("atomic", "chunk two\n", source_uri="session:atomic")
    _age_rows([a, b], 60)  # past the coalescing quiet window
    _set_deferrals([a], ingest_queue.DEFERRAL_CAP - 1)  # tonight's defer escalates a
    _recorder(monkeypatch, fail_on={"session:atomic": ExtractorUnavailable("never heals")})

    consolidate._drain_ingest_queue()

    sa, sb = _row(a), _row(b)
    assert sa[0] == "failed" and sa[1] == ingest_queue.MAX_ATTEMPTS
    assert sb[0] == "failed" and sb[1] == ingest_queue.MAX_ATTEMPTS, (
        "the sibling row split off from its group instead of retiring with it"
    )
    assert "ExtractorUnavailable" in (sb[3] or ""), "the sibling must carry the same error"
    assert consolidate._COHORT_STATS["deferral_escalations"] == 2
    assert ingest_queue.queue_depth() == 0, "no half-pending group left behind"


def test_the_owner_requeue_reverses_the_escalation_group_wide(kb, _no_stand_down, monkeypatch):
    a = _enqueue("atomic-rq", "chunk one\n", source_uri="session:rq")
    b = _enqueue("atomic-rq", "chunk two\n", source_uri="session:rq")
    _age_rows([a, b], 60)
    _set_deferrals([a], ingest_queue.DEFERRAL_CAP - 1)
    _recorder(monkeypatch, fail_on={"session:rq": ExtractorUnavailable("outage")})
    consolidate._drain_ingest_queue()

    requeued = ingest_queue.requeue_failed(reason="outage fixed")

    assert set(requeued) == {a, b}
    assert _row(a)[:3] == ("pending", 0, 0)
    assert _row(b)[:3] == ("pending", 0, 0), "both budgets re-granted, group intact"


def test_a_sub_cap_deferral_still_leaves_the_whole_group_pending(kb, _no_stand_down, monkeypatch):
    """The fix must not widen: below the cap NOTHING escalates — both rows stay
    'pending' with attempts untouched, exactly as before."""
    a = _enqueue("atomic-sub", "chunk one\n", source_uri="session:sub")
    b = _enqueue("atomic-sub", "chunk two\n", source_uri="session:sub")
    _age_rows([a, b], 60)
    _recorder(monkeypatch, fail_on={"session:sub": ExtractorUnavailable("blip")})

    consolidate._drain_ingest_queue()

    assert _row(a)[:3] == ("pending", 0, 1)
    assert _row(b)[:3] == ("pending", 0, 1)
    assert consolidate._COHORT_STATS.get("deferral_escalations", 0) == 0


# ------------------------------------------------- item 5: the reembed gate


def _stale_chunks(model: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT count(*) FROM chunks WHERE embed_model IS DISTINCT FROM %s", (model,)
        ).fetchone()[0]


@pytest.fixture()
def _stale_store(kb, monkeypatch):
    """Three single-chunk items, then a simulated embedder swap: every chunk's
    embed_model differs from the configured one."""
    for i in range(3):
        ingest("reembed-p", "note", f"n{i}", f"short body number {i}", uri=f"n{i}")
    monkeypatch.setattr(settings, "kb_embed_model", "swapped:test-1b")
    assert _stale_chunks("swapped:test-1b") == 3


def test_reembed_stops_between_batches(_stale_store, monkeypatch):
    monkeypatch.setattr(reembed_mod, "BATCH", 1)
    monkeypatch.setattr(settings, "kb_reembed_night_cap", 0)
    verdicts = iter([None, "battery"])

    assert reembed_mod.reembed(stop=lambda: next(verdicts)) == 1
    assert _stale_chunks("swapped:test-1b") == 2, "the stop must leave the rest stale, not lost"


def test_reembed_honors_the_night_cap_knob(_stale_store, monkeypatch):
    monkeypatch.setattr(reembed_mod, "BATCH", 1)
    monkeypatch.setattr(settings, "kb_reembed_night_cap", 2)

    assert reembed_mod.reembed() == 2
    assert _stale_chunks("swapped:test-1b") == 1
    # The next (weekend) night finishes the job — nothing was lost to the cap.
    assert reembed_mod.reembed(max_chunks=0) == 1
    assert _stale_chunks("swapped:test-1b") == 0


def test_reembed_zero_cap_means_uncapped(_stale_store, monkeypatch):
    monkeypatch.setattr(settings, "kb_reembed_night_cap", 0)
    assert reembed_mod.reembed() == 3
    assert _stale_chunks("swapped:test-1b") == 0


def test_the_nightly_step_is_gated_to_unbounded_days(_every_day_bounded, monkeypatch):
    monkeypatch.setattr(
        reembed_mod, "reembed", lambda **kw: pytest.fail("reembed ran on a deadline day")
    )
    assert tail_steps.reembed_step(lambda: None) == 0


def test_the_nightly_step_passes_the_runs_stop_through(monkeypatch):
    monkeypatch.setattr(settings, "kb_drain_deadline", "")
    monkeypatch.setattr(settings, "kb_drain_deadline_days", "")
    called: dict = {}

    def fake_reembed(stop=None, max_chunks=None):
        called["stop"] = stop
        return 5

    monkeypatch.setattr(reembed_mod, "reembed", fake_reembed)

    def guard():
        return None

    assert tail_steps.reembed_step(guard) == 5
    assert called["stop"] is guard, "the stand-down guard must reach the batch loop"


# ------------------------- item 6: summaries since-mark fix (lossy deferral)


def test_an_advanced_run_mark_cannot_drop_unreached_tags(kb):
    """THE regression: _write_report advanced `since` even over a stood-down
    summaries step, silently dropping its unreached tags. Need is now per-tag
    from the data, so a `since` AFTER every item changes nothing."""
    tag = "hw:tps55288"
    for i in range(3):
        ingest("p", "decision", f"d{i}", f"decision body {i}", tags=[tag], uri=f"d{i}")

    future_mark = datetime.now(UTC) + timedelta(hours=1)
    assert consolidation.tags_needing_summary(future_mark) == [("p", tag, 3)]
    assert consolidation.tags_needing_summary(None) == [("p", tag, 3)]


def test_a_written_summary_is_the_tags_high_water(kb):
    tag = "lang:python"
    ids = [
        ingest("p", "decision", f"d{i}", f"decision body {i}", tags=[tag], uri=f"d{i}")
        for i in range(3)
    ]
    consolidation.create_summary("p", tag, "summary body", ids)
    assert consolidation.tags_needing_summary(None) == [], "summarized items no longer count"

    # Two post-summary items: below min_new, still quiet...
    for i in range(2):
        ingest("p", "decision", f"e{i}", f"newer body {i}", tags=[tag], uri=f"e{i}")
    assert consolidation.tags_needing_summary(None) == []
    assert consolidation.tags_needing_summary(None, min_new=1) == [("p", tag, 2)]

    # ...and the third re-opens the tag with exactly the post-summary count.
    ingest("p", "decision", "e2", "newer body 2", tags=[tag], uri="e2")
    assert consolidation.tags_needing_summary(None) == [("p", tag, 3)]


def test_a_superseded_summary_still_proves_the_pass_ran(kb):
    """The high-water is ANY-status: refreshes supersede prior summaries, and a
    superseded mark must not re-open a tag whose items are all old."""
    tag = "tool:docker"
    ids = [
        ingest("p", "decision", f"d{i}", f"decision body {i}", tags=[tag], uri=f"d{i}")
        for i in range(3)
    ]
    sid = consolidation.create_summary("p", tag, "summary body", ids)
    with transaction() as conn:
        conn.execute("UPDATE items SET status = 'superseded' WHERE id = %s", (sid,))
    assert consolidation.tags_needing_summary(None) == []


def test_foreign_reports_do_not_advance_any_tags_mark(kb):
    """A deep-search report (or the nightly's own report on a stood-down night)
    lands in kb-system; no tag's high-water moves."""
    tag = "hw:esp32"
    for i in range(3):
        ingest("p", "decision", f"d{i}", f"decision body {i}", tags=[tag], uri=f"d{i}")
    ingest(
        "kb-system",
        "report",
        "Consolidation report today",
        "Nightly consolidation report",
        uri="report:today",
        meta={"consolidation": True},
    )
    assert consolidation.tags_needing_summary(datetime.now(UTC)) == [("p", tag, 3)]


# ----------------------------- item 7: fleet window tiles to previous report


def _report_at(hours_ago: float, uri: str) -> int:
    rid = ingest(
        "kb-system",
        "report",
        f"Consolidation report {uri}",
        "Nightly consolidation report",
        kind="chat",
        uri=f"report:{uri}",
        meta={"consolidation": True},
    )
    with transaction() as conn:
        conn.execute(
            "UPDATE items SET created_at = now() - make_interval(mins => %s) WHERE id = %s",
            (int(hours_ago * 60), rid),
        )
    return rid


def _age_queue_rows(ids: list[int], hours: float) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET created_at = now() - make_interval(mins => %s) "
            "WHERE id = ANY(%s)",
            (int(hours * 60), list(ids)),
        )


def test_without_a_previous_report_the_window_stays_24h(kb):
    qid = _enqueue("firstrun", "no session id here")  # degenerate on purpose
    _age_queue_rows([qid], 25)
    assert "firstrun" not in fleet_health.degenerate_by_project()
    assert fleet_health.degenerate_by_project(hours=48)["firstrun"]["degenerate"] == 1


def test_the_window_tiles_to_the_previous_report(kb):
    """The 08-21 hole: a 30h-old submission fell outside now()-24h because the
    run landed late. Anchored to the previous report (48h ago) it is covered."""
    _report_at(48, "prev")
    qid = _enqueue("tiled", "no session id here")
    _age_queue_rows([qid], 30)

    report = fleet_health.degenerate_by_project()
    assert report["tiled"] == {"degenerate": 1, "total": 1}
    assert fleet_health.fleet_by_box()["owner"]["total"] == 1, (
        "both aggregates must describe the same tiled window"
    )


def test_the_window_is_bounded_above_at_7d(kb):
    _report_at(24 * 10, "ancient")  # box was off for ten days
    old = _enqueue("bounded", "no session id, eight days old")
    _age_queue_rows([old], 24 * 8)
    recent = _enqueue("bounded", "no session id, six days old")
    _age_queue_rows([recent], 24 * 6)

    report = fleet_health.degenerate_by_project()
    assert report["bounded"] == {"degenerate": 1, "total": 1}, (
        "a 10-day anchor must clamp to 7d, not scan the whole table"
    )


def test_the_window_is_floored_at_12h_on_a_double_run_day(kb):
    """The second run of a double-run day anchors to a report minutes old; the
    12h floor keeps the window from shrinking into a sliver that misses an
    evening-only machine."""
    _report_at(1, "an-hour-ago")
    qid = _enqueue("floored", "no session id here")
    _age_queue_rows([qid], 6)
    assert fleet_health.degenerate_by_project()["floored"]["degenerate"] == 1


def test_window_start_clamps_and_anchors(kb):
    now = datetime.now(UTC)
    # No report: the 24h fallback, inside the clamp band.
    start = fleet_health.window_start(now=now)
    assert abs((now - start) - timedelta(hours=24)) < timedelta(minutes=1)
    # Explicit hours bypass the anchor entirely.
    fixed = fleet_health.window_start(hours=48, now=now)
    assert now - fixed == timedelta(hours=48)
    # An anchored start never comes closer than 12h nor further than 7d.
    _report_at(1, "clamp-near")
    assert now - fleet_health.window_start(now=now) >= timedelta(hours=12) - timedelta(minutes=1)

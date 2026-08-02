"""The nightly window is DEALT, not raced for.

Three weeks of run reports say the same thing: the window is subscribed at
100-110% of capacity (drain 5.9-7.1 h + LLM tail 5.0-6.0 h against 11 h), the
drain is Step 0 with a refill-until-empty loop, and so the tail is the residual
claimant. Report 9684 is the shape of that: queued_drained 33,284 s (99.5% of
the window), triage 0 rows, contradictions/summaries/evidence all stood down,
and the judged-pair memory frozen at 0 rows since it landed.

These tests pin the fix (``kb_tail_reserve_minutes``): the drain stops admitting
NEW refill cohorts once the next one would eat the reserve, the deferred rows
stay 'pending', the TAIL then runs, and the deadline/battery guards still
preempt everything including the tail. They also pin the two P0-2 hooks the
nightly wiring owns — the ``auto_applied`` tail step and the run-end digest —
against the shared contract, including the case where the module implementing
that contract is not installed at all.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta

import pytest

from kbcore import ingest_queue
from kbcore.config import settings
from kbworker import allocator, consolidate, sessions, tail_steps

# ------------------------------------------------------------------ the maths


def _at(hhmm: str) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 8, 2, hh, mm)


def test_remaining_window_is_the_wall_clock_gap_and_None_without_a_deadline():
    assert allocator.remaining_window(_at("03:00"), _at("12:00")) == 9 * 3600
    assert allocator.remaining_window(_at("13:00"), _at("12:00")) == -3600, (
        "a passed cutoff must report as negative, not clamp to zero — the caller "
        "decides, and admitting on a clamp is exactly the bug"
    )
    assert allocator.remaining_window(_at("03:00"), None) is None


def test_parse_deadline_reads_the_HHMM_knob_and_refuses_to_guess():
    now = _at("03:00")
    assert allocator.parse_deadline("12:00", now) == _at("12:00")
    assert allocator.parse_deadline("", now) is None
    assert allocator.parse_deadline("noon", now) is None, "an unparseable knob must not invent one"


def test_the_admission_boundary_is_exactly_deadline_minus_reserve():
    remaining = 4 * 3600.0  # 08:00, deadline 12:00
    reserve = 3 * 3600.0  # the tail gets 09:00-12:00
    assert allocator.should_admit_cohort(remaining, reserve, 3600.0) is True, "it fits exactly"
    assert allocator.should_admit_cohort(remaining, reserve, 3601.0) is False, "one second over"
    assert allocator.should_admit_cohort(remaining, reserve, 1.0) is True


def test_reserve_of_zero_disables_the_whole_thing():
    """The kill switch has to restore the OLD behaviour byte for byte: admit
    even a cohort that cannot possibly finish before the cutoff."""
    assert allocator.should_admit_cohort(60.0, 0, 10 * 3600.0) is True
    assert allocator.should_admit_cohort(-9999.0, 0, 10 * 3600.0) is True


def test_no_deadline_means_no_window_to_apportion():
    assert allocator.should_admit_cohort(None, 3 * 3600.0, 10 * 3600.0) is True


def test_the_first_cohort_falls_back_to_the_MEASURED_constant():
    t = allocator.CostTracker()
    assert t.observed is False
    assert t.seconds_per_job == allocator.FIRST_COHORT_SECONDS_PER_JOB == 111.0
    assert t.estimate(30) == pytest.approx(30 * 111.0)


def test_the_estimate_switches_to_THIS_RUNs_observed_cost():
    """Tonight's GPU is the only honest predictor of tonight's next cohort."""
    t = allocator.CostTracker()
    t.record(jobs=10, seconds=200.0)
    assert t.observed is True
    assert t.seconds_per_job == pytest.approx(20.0)
    t.record(jobs=10, seconds=400.0)
    assert t.seconds_per_job == pytest.approx(30.0), "cohorts average, they do not replace"
    assert t.estimate(4) == pytest.approx(120.0)
    t.record(jobs=0, seconds=999.0)
    assert t.seconds_per_job == pytest.approx(30.0), "a zero-job cohort must not skew the average"


def test_a_reserve_admits_while_the_window_is_wide_and_denies_at_the_edge(monkeypatch):
    monkeypatch.setattr(settings, "kb_tail_reserve_minutes", 180)
    monkeypatch.setattr(settings, "kb_drain_deadline", "12:00")
    monkeypatch.setattr(
        settings, "kb_drain_deadline_days", ""
    )  # day-independent: the weekend rule must not flip tests by calendar
    clock = {"now": _at("01:00")}
    res = allocator.TailReserve.for_run(now=clock["now"])
    res._now = lambda: clock["now"]
    res.record(jobs=10, seconds=1000.0)  # 100 s/job
    batch = [{"group": object()} for _ in range(10)]  # ~1000 s

    assert res.deny_reason([batch]) is None, "01:00 + 1000s is nowhere near 09:00"
    clock["now"] = _at("08:50")  # 10 min left before the 09:00 reserve line
    assert res.deny_reason([batch]) == "tail-reserve"
    assert res.deny_reason([]) is None, "nothing pending is not a stop"


def test_the_reserve_never_binds_when_the_knob_is_off(monkeypatch):
    monkeypatch.setattr(settings, "kb_tail_reserve_minutes", 0)
    monkeypatch.setattr(settings, "kb_drain_deadline", "12:00")
    monkeypatch.setattr(
        settings, "kb_drain_deadline_days", ""
    )  # day-independent: the weekend rule must not flip tests by calendar
    res = allocator.TailReserve.for_run(now=_at("11:59"))
    res._now = lambda: _at("11:59")
    assert res.enabled is False
    assert res.deny_reason([[{"group": object()}] * 30]) is None


def test_the_reserve_never_binds_without_a_deadline(monkeypatch):
    monkeypatch.setattr(settings, "kb_tail_reserve_minutes", 180)
    monkeypatch.setattr(settings, "kb_drain_deadline", "")
    monkeypatch.setattr(
        settings, "kb_drain_deadline_days", ""
    )  # day-independent: the weekend rule must not flip tests by calendar
    res = allocator.TailReserve.for_run(now=_at("11:59"))
    assert res.enabled is False
    assert res.deny_reason([[{"group": object()}]]) is None


def test_deferred_work_is_counted_in_GROUPS_not_windows():
    """A 350-row session is ONE deferred submission however many windows it
    became; reporting windows would inflate every stop line."""
    g1, g2 = {"ids": [1]}, {"ids": [2]}
    jobs = [{"group": g1}, {"group": g1}, {"group": g1}, {"group": g2}]
    assert allocator.deferred_groups(jobs) == 2
    assert allocator.deferred_groups([]) == 0


# ------------------------------------------------------- the drain integration


@pytest.fixture(autouse=True)
def _clean_stop_state(monkeypatch):
    """No battery DB, no stale stop log, and the wall-clock guard disarmed —
    these tests arm the RESERVE, and a suite run after noon would otherwise stop
    every one of them on the deadline instead."""
    monkeypatch.setattr(settings, "kb_power_db", "")
    monkeypatch.setattr(consolidate, "should_stop", lambda: None)
    consolidate.reset_stop_cache()
    consolidate._STOP_LOG.clear()
    yield
    consolidate.reset_stop_cache()
    consolidate._STOP_LOG.clear()


def _enqueue(project: str, body: str) -> int:
    res = ingest_queue.enqueue(project, body, kind="session")
    assert res.id is not None, f"setup failed to queue {body!r}"
    return res.id


def _pinned(now: datetime, *, seed: tuple[int, float] = (1, 7200.0)) -> allocator.TailReserve:
    """A reserve with a frozen clock and a pre-seeded per-job cost.

    Extraction is mocked in these tests, so an unseeded tracker would measure
    ~0 s/job and every cohort would fit. Seeding it with one 7,200 s job makes
    the ESTIMATE the thing under test: at 08:00 against a 12:00 deadline the
    reserve line is 09:00, so a 2-job cohort estimated at 4,800 s cannot fit the
    3,600 s of headroom left in front of it.
    """
    res = allocator.TailReserve(
        reserve_seconds=180 * 60.0,
        deadline=now.replace(hour=12, minute=0, second=0, microsecond=0),
    )
    res._now = lambda: now
    res.record(*seed)
    return res


def _freeze(monkeypatch, now: datetime) -> None:
    monkeypatch.setattr(
        allocator.TailReserve, "for_run", staticmethod(lambda started=None: _pinned(now))
    )


def test_a_backlog_meets_the_reserve_and_the_REST_STAYS_PENDING(kb, monkeypatch, caplog):
    """The headline behaviour. A multi-cohort backlog whose next cohort would
    run into the tail's reserve stops with reason 'tail-reserve', recorded in
    the SAME counts["stopped"] shape as the deadline/battery stops, and every
    unreached row is still 'pending' for tomorrow (Invariant 3 makes it safe)."""
    monkeypatch.setattr(settings, "kb_drain_cohort", 2)
    for i in range(4):
        _enqueue("reserve-drain", f"User: q{i}\nAssistant: a{i}")
    seen: list[str] = []

    def extract(text, project, uri, **kw):
        seen.append(uri)
        return 1, 0

    monkeypatch.setattr(sessions, "extract_and_ingest", extract)
    _freeze(monkeypatch, _at("08:00"))

    with caplog.at_level(logging.WARNING, logger="kbworker.allocator"):
        drained, saved = consolidate._drain_ingest_queue()

    assert (drained, saved) == (2, 2), "the drain kept eating into the tail's reserve"
    assert len(seen) == 2
    assert consolidate._STOP_LOG["queued_drained"]["reason"] == "tail-reserve"
    assert consolidate._STOP_LOG["queued_drained"]["deferred"] == 2
    assert "drain pausing for tail reserve: 2 groups deferred" in caplog.text
    assert "the tail runs from 08:00" in caplog.text
    assert ingest_queue.queue_depth() == 2, "deferred rows must stay pending, never lost"

    # ...and tomorrow's run — a window with room — picks up exactly here.
    monkeypatch.setattr(
        allocator.TailReserve, "for_run", staticmethod(lambda started=None: allocator.TailReserve())
    )
    assert consolidate._drain_ingest_queue() == (2, 2)
    assert ingest_queue.queue_depth() == 0


def test_the_runs_FIRST_cohort_is_exempt_from_the_reserve_too(kb, monkeypatch):
    """'Should this run start at all' belongs to _drain_step, exactly as it does
    for the deadline guard — a drain that asked before its own first batch would
    make no progress at all on a late-starting night, and the queue only grows."""
    monkeypatch.setattr(settings, "kb_drain_cohort", 1)
    _enqueue("reserve-exempt", "User: only\nAssistant: one")
    monkeypatch.setattr(sessions, "extract_and_ingest", lambda t, p, u, **kw: (1, 0))
    _freeze(monkeypatch, _at("11:59"))

    assert consolidate._drain_ingest_queue() == (1, 1)


def test_the_DEADLINE_still_preempts_the_reserve(kb, monkeypatch):
    """The reserve is an INNER bound: a stop that is really the wall clock must
    still be reported as 'deadline'. The reserve can never keep a run alive."""
    monkeypatch.setattr(settings, "kb_drain_cohort", 1)
    for i in range(3):
        _enqueue("reserve-deadline", f"User: d{i}\nAssistant: d{i}")
    monkeypatch.setattr(sessions, "extract_and_ingest", lambda t, p, u, **kw: (1, 0))
    monkeypatch.setattr(consolidate, "should_stop", lambda: "deadline")
    _freeze(monkeypatch, _at("08:00"))

    consolidate._drain_ingest_queue()

    assert consolidate._STOP_LOG["queued_drained"]["reason"] == "deadline", (
        "the wall clock must win over the reserve, always"
    )
    assert ingest_queue.queue_depth() == 2


def test_the_tail_RUNS_after_the_drain_stands_down_for_the_reserve(kb, monkeypatch):
    """The whole point. A reserve that stops the drain but never lets the tail
    run has bought nothing. Order matters too: auto-apply sits after triage and
    before contradictions, and the digest is pushed once, at the end."""
    monkeypatch.setattr(settings, "kb_drain_cohort", 2)
    for i in range(4):
        _enqueue("reserve-tail", f"User: t{i}\nAssistant: t{i}")
    _freeze(monkeypatch, _at("08:00"))

    order: list[str] = []
    monkeypatch.setattr(sessions, "extract_and_ingest", lambda t, p, u, **kw: (1, 0))
    monkeypatch.setattr(
        consolidate.review_triage,
        "run_triage",
        lambda batch, should_stop=None: (order.append("triage"), 0)[1],
    )
    monkeypatch.setattr(
        tail_steps, "auto_apply_step", lambda counts, stop=None: (order.append("auto"), 2)[1]
    )
    monkeypatch.setattr(
        consolidate, "_contradictions_all", lambda p, s=None, c=None: (order.append("contra"), 0)[1]
    )
    monkeypatch.setattr(
        consolidate, "_evidence", lambda s=None: (order.append("evidence"), (0, 0))[1]
    )
    monkeypatch.setattr(consolidate, "_summaries", lambda since, s=None: 0)
    monkeypatch.setattr(consolidate.eval_sentinel, "run_sentinel", lambda counts: None)
    pushed: list[dict] = []
    monkeypatch.setattr(tail_steps, "push_run_digest", lambda counts: pushed.append(counts) or True)

    result = consolidate.main(stop=lambda: None)

    assert result["queued_drained"] == 2, "the drain did not stand down for the reserve"
    assert result["stopped"]["queued_drained"]["reason"] == "tail-reserve"
    assert order == ["triage", "auto", "contra", "evidence"], (
        f"the tail did not run after the reserve stopped the drain: {order}"
    )
    assert result["auto_applied"] == 2, "the tail step's count is missing from the report"
    assert result["digest_pushed"] is True
    assert len(pushed) == 1, "the digest must be pushed exactly once per run"
    assert ingest_queue.queue_depth() == 2, "the deferred rows are still waiting, unharmed"


# ------------------------------------------------------------- the P0-2 hooks


def test_auto_apply_calls_the_CONTRACT_and_reports_its_breakdown(monkeypatch):
    contract = {
        "applied_noise": 3,
        "applied_duplicate": 2,
        "skipped_accessed": 1,
        "skipped_cooling": 4,
        "skipped_target_gone": 0,
        "eligible_remaining": 7,
    }
    fake = type(sys)("kbcore.review_apply")
    fake.auto_apply_rejects = lambda cap=None: contract
    monkeypatch.setitem(sys.modules, "kbcore.review_apply", fake)
    # `from kbcore import review_apply` resolves the PACKAGE ATTRIBUTE first, so
    # once any test has imported the real module, a sys.modules fake alone is
    # invisible — patch both, or these tests are order-dependent.
    import kbcore

    monkeypatch.setattr(kbcore, "review_apply", fake, raising=False)

    counts: dict = {}
    assert tail_steps.auto_apply_step(counts) == 5, "applied = noise + duplicate"
    assert counts["auto_apply_counts"] == contract, "the breakdown must reach the report"


def test_auto_apply_is_stop_gated_like_every_other_tail_step(monkeypatch):
    called: list[int] = []
    fake = type(sys)("kbcore.review_apply")
    fake.auto_apply_rejects = lambda cap=None: called.append(1) or {}
    monkeypatch.setitem(sys.modules, "kbcore.review_apply", fake)
    # `from kbcore import review_apply` resolves the PACKAGE ATTRIBUTE first, so
    # once any test has imported the real module, a sys.modules fake alone is
    # invisible — patch both, or these tests are order-dependent.
    import kbcore

    monkeypatch.setattr(kbcore, "review_apply", fake, raising=False)

    counts: dict = {}
    assert tail_steps.auto_apply_step(counts, stop=lambda: "battery") == 0
    assert not called, "the step ran while the house was on battery"
    assert consolidate._STOP_LOG["auto_applied"]["reason"] == "battery"


def test_a_MISSING_review_apply_module_does_not_break_the_run(kb, monkeypatch, caplog):
    """Package U's module may simply not be there. A nightly run must degrade to
    'that step did nothing', never to a failed pass."""
    real_import = __import__

    def no_review_apply(name, *args, **kw):
        if name in {"kbcore.review_apply", "review_apply"} or (
            name == "kbcore" and args and len(args) > 2 and "review_apply" in (args[2] or ())
        ):
            raise ImportError("no module named kbcore.review_apply")
        return real_import(name, *args, **kw)

    monkeypatch.setattr("builtins.__import__", no_review_apply)
    counts: dict = {}
    with caplog.at_level(logging.WARNING, logger="kbworker.allocator"):
        assert tail_steps.auto_apply_step(counts) == 0
    assert "review_apply" in caplog.text
    assert "auto_apply_counts" not in counts


def test_the_digest_lines_carry_the_facts_the_run_produced():
    lines = tail_steps.digest_lines(
        {
            "queued_drained": 12,
            "queued_items_saved": 40,
            "review_triaged": 500,
            "auto_applied": 5,
            "contradiction_pairs_judged": 33,
            "first_expiry_date": (datetime.now().date() + timedelta(days=3)).isoformat(),
        }
    )
    assert lines[:5] == [
        "drained: 12",
        "saved: 40",
        "triaged: 500",
        "auto-applied: 5",
        "judged pairs: 33",
    ]
    assert "start expiring" in lines[5]


def test_the_digest_omits_facts_the_run_did_not_produce():
    """A step that FAILED leaves no key. Emitting a 0 for it would read like a
    healthy step that found nothing to do — the exact confusion the run report
    already had before the curation counts landed."""
    lines = tail_steps.digest_lines({"queued_drained": 0})
    assert lines == ["drained: 0"]
    far = (datetime.now().date() + timedelta(days=90)).isoformat()
    for expiry in (far, None, "not-a-date"):
        assert tail_steps.digest_lines({"queued_drained": 1, "first_expiry_date": expiry}) == [
            "drained: 1"
        ]


def test_push_digest_is_best_effort_and_NEVER_raises(monkeypatch):
    fake = type(sys)("kbcore.notify")
    sent: list[list[str]] = []
    fake.push_digest = lambda lines: sent.append(lines) or True
    monkeypatch.setitem(sys.modules, "kbcore.notify", fake)
    import kbcore

    monkeypatch.setattr(kbcore, "notify", fake, raising=False)
    assert tail_steps.push_run_digest({"queued_drained": 1}) is True
    assert sent == [["drained: 1"]]

    fake.push_digest = lambda lines: (_ for _ in ()).throw(RuntimeError("ntfy is down"))
    assert tail_steps.push_run_digest({"queued_drained": 1}) is False, "a dead ntfy failed the run"
    assert tail_steps.push_run_digest({}) is False, "nothing to say, nothing to send"

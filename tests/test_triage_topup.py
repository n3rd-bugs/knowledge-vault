"""Weekend triage top-up: spend an unbounded night on the review backlog.

Owner request 2026-08-08 ("at weekend triage as many items as we can"). The
nightly triages a FIXED batch and stops; on a day with no drain deadline the run
finished 19:45 and the card idled until the next 01:00 firing. The new tail step
keeps triaging in SLICES until the cutoff knob, an empty queue, or the run's
stand-down guard — and slices are the design, not an implementation detail: the
run holds pg_advisory_lock(42) end to end, so one long batch straddling 01:00
would make the NEXT run skip entirely.

THE DAY IS PINNED IN EVERY TEST. A previous weekend-logic suite flipped red on
Sundays. Note that ``kb_drain_deadline_days=""`` means the deadline applies
EVERY day (kbcore.drainsched), so it is NOT the always-unbounded pin — an empty
``kb_drain_deadline`` is. Bounded days are pinned with all seven days listed, so
neither case depends on when the suite runs.

``run_triage`` is the GPU boundary and is mocked throughout (CLAUDE.md: mock only
Ollama); the day logic, the knobs and the loop run as real code.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest

from kbcore import db, drainsched, review_triage
from kbcore.config import settings
from kbworker import consolidate, tail_steps

# Fixed instants for the parse tests — never "now", so nothing here depends on
# the wall clock of the machine running the suite.
SAT = datetime(2026, 8, 8, 9, 0)  # the owner's Saturday
MON = datetime(2026, 8, 3, 9, 0)

ALL_DAYS = "mon,tue,wed,thu,fri,sat,sun"


@pytest.fixture(autouse=True)
def _topup_defaults(monkeypatch):
    """Pin every knob the step reads: the live .env must not decide these tests."""
    monkeypatch.setattr(settings, "kb_drain_deadline", "")  # -> unbounded EVERY day
    monkeypatch.setattr(settings, "kb_drain_deadline_days", ALL_DAYS)
    monkeypatch.setattr(settings, "kb_triage_topup_until", "23:00")
    monkeypatch.setattr(settings, "kb_triage_topup_slice", 200)
    consolidate._STOP_LOG.clear()
    yield
    consolidate._STOP_LOG.clear()


@pytest.fixture()
def open_window(monkeypatch):
    """Anchor the cutoff an hour ahead, so 'before the cutoff' is not a wall-clock
    race at 22:59 on the machine running the suite."""
    monkeypatch.setattr(tail_steps, "_topup_cutoff", lambda: datetime.now() + timedelta(hours=1))


def _deadline_day(monkeypatch) -> None:
    """Pin a day the deadline DOES apply — on every weekday, so it never flips."""
    monkeypatch.setattr(settings, "kb_drain_deadline", "12:00")
    monkeypatch.setattr(settings, "kb_drain_deadline_days", ALL_DAYS)


def _never_called(*_a, **_kw):
    raise AssertionError("run_triage was called when the top-up should be OFF")


def _guard() -> str | None:
    """A stand-down guard that never stands down — i.e. an unattended run whose
    grid is healthy and whose clock is fine. The top-up requires SOME guard."""
    return None


# --------------------------------------------------------- the day question


def test_is_unbounded_day_mirrors_the_deadline(monkeypatch):
    """ONE definition of 'the box is free tonight' — derived from the deadline,
    never a second weekend notion that could drift out of step with it."""
    monkeypatch.setattr(settings, "kb_drain_deadline", "12:00")
    monkeypatch.setattr(settings, "kb_drain_deadline_days", "mon,tue,wed,thu,fri")
    assert drainsched.is_unbounded_day(SAT) is True
    assert drainsched.is_unbounded_day(MON) is False


def test_no_deadline_configured_makes_every_day_unbounded(monkeypatch):
    monkeypatch.setattr(settings, "kb_drain_deadline", "")
    assert drainsched.is_unbounded_day(MON) is True


# ------------------------------------------------------------- the cutoff knob


def test_the_cutoff_knob_parses_into_todays_local_wall_clock():
    assert tail_steps._topup_cutoff(now=SAT) == datetime(2026, 8, 8, 23, 0)


def test_an_empty_cutoff_is_the_kill_switch_and_is_silent(monkeypatch, caplog):
    monkeypatch.setattr(settings, "kb_triage_topup_until", "")
    with caplog.at_level(logging.WARNING, logger="kbworker.tail_steps"):
        assert tail_steps._topup_cutoff(now=SAT) is None
    assert caplog.text == ""


@pytest.mark.parametrize("bad", ["banana", "25:00", "23:00:30", "23", "-1:00"])
def test_a_malformed_cutoff_disables_the_feature_instead_of_crashing(monkeypatch, caplog, bad):
    """This runs unattended at 2am: a typo must cost the FEATURE, not the run."""
    monkeypatch.setattr(settings, "kb_triage_topup_until", bad)
    with caplog.at_level(logging.WARNING, logger="kbworker.tail_steps"):
        assert tail_steps._topup_cutoff(now=SAT) is None
    assert "KB_TRIAGE_TOPUP_UNTIL" in caplog.text


def test_a_malformed_cutoff_keeps_the_step_a_no_op(monkeypatch, caplog):
    monkeypatch.setattr(settings, "kb_triage_topup_until", "half past ten")
    monkeypatch.setattr(review_triage, "run_triage", _never_called)
    counts: dict = {}
    with caplog.at_level(logging.WARNING, logger="kbworker.tail_steps"):
        assert tail_steps.review_triaged_topup(counts, lambda: None) == 0
    assert "the weekend triage top-up is OFF" in caplog.text
    assert consolidate._STOP_LOG == {}


# ------------------------------------------------------------------ the loop


def test_slices_run_until_the_queue_is_drained(monkeypatch, open_window):
    """Unbounded day + before the cutoff: slice after slice until one triages 0."""
    calls: list[tuple[int, object]] = []
    yields = iter([5, 4, 0])

    def fake_triage(batch, should_stop=None):
        calls.append((batch, should_stop))
        return next(yields)

    def guard() -> str | None:  # identity is what the assertion below checks
        return None

    monkeypatch.setattr(review_triage, "run_triage", fake_triage)
    counts: dict = {}
    assert tail_steps.review_triaged_topup(counts, guard) == 9
    assert [c[0] for c in calls] == [200, 200, 200], "the slice knob must bound each call"
    assert all(c[1] is guard for c in calls), "the run's stand-down guard must reach run_triage"
    assert counts["triage_topup_slices"] == 3, "slice telemetry must ride into the report"
    assert consolidate._STOP_LOG == {}


def test_the_slice_knob_sizes_each_call(monkeypatch, open_window):
    monkeypatch.setattr(settings, "kb_triage_topup_slice", 25)
    seen: list[int] = []
    monkeypatch.setattr(
        review_triage, "run_triage", lambda batch, should_stop=None: seen.append(batch) or 0
    )
    assert tail_steps.review_triaged_topup({}, _guard) == 0
    assert seen == [25]


def test_a_deadline_day_gets_no_topup_at_all(monkeypatch, open_window):
    """The normal tail is unchanged: a workday night still stops after its one
    fixed batch, and this is NOT recorded as a stand-down (nothing was deferred
    by a guard — there was simply no idle window to fill)."""
    _deadline_day(monkeypatch)
    monkeypatch.setattr(review_triage, "run_triage", _never_called)
    counts: dict = {}
    assert tail_steps.review_triaged_topup(counts, lambda: None) == 0
    assert consolidate._STOP_LOG == {}
    assert "triage_topup_slices" not in counts, "a step that never ran must claim no telemetry"


def test_a_passed_cutoff_admits_no_slice(monkeypatch):
    monkeypatch.setattr(tail_steps, "_topup_cutoff", lambda: datetime.now() - timedelta(minutes=1))
    monkeypatch.setattr(review_triage, "run_triage", _never_called)
    assert tail_steps.review_triaged_topup({}, lambda: None) == 0


def test_the_cutoff_is_anchored_once_per_run(monkeypatch):
    """Re-reading it per slice would hand a slice that crosses midnight a fresh
    23-hour window off TOMORROW's date — the top-up would run into the workday."""
    anchored = datetime.now() + timedelta(hours=1)
    reads: list[int] = []

    def counted_cutoff():
        reads.append(1)
        return anchored

    yields = iter([3, 3, 0])
    monkeypatch.setattr(tail_steps, "_topup_cutoff", counted_cutoff)
    monkeypatch.setattr(review_triage, "run_triage", lambda batch, should_stop=None: next(yields))
    assert tail_steps.review_triaged_topup({}, _guard) == 6
    assert len(reads) == 1


def test_the_day_is_re_asked_every_slice(monkeypatch, open_window):
    """A Sunday top-up that survives into Monday must pick the deadline back up
    rather than leak the weekend into the work week."""
    slices: list[int] = []

    def fake_triage(batch, should_stop=None):
        slices.append(batch)
        _deadline_day(monkeypatch)  # the clock rolled into a bounded day
        return 7

    monkeypatch.setattr(review_triage, "run_triage", fake_triage)
    assert tail_steps.review_triaged_topup({}, _guard) == 7
    assert len(slices) == 1, "the top-up kept going on a day the deadline applies"


def test_the_backstop_bounds_a_pending_set_that_never_shrinks(monkeypatch, open_window, caplog):
    monkeypatch.setattr(tail_steps, "TOPUP_MAX_SLICES", 3)
    monkeypatch.setattr(review_triage, "run_triage", lambda batch, should_stop=None: 1)
    with caplog.at_level(logging.WARNING, logger="kbworker.tail_steps"):
        assert tail_steps.review_triaged_topup({}, _guard) == 3
    assert "backstop" in caplog.text


# ------------------------------------------------------- the stand-down guard


def test_the_battery_guard_stops_the_topup_mid_loop(monkeypatch, open_window):
    """The battery guard is NOT day-aware (drainsched docstring): a grid sag on a
    Saturday stands the top-up down exactly like any other night."""
    verdicts = iter([None, None, "battery", "battery"])
    slices: list[int] = []
    monkeypatch.setattr(
        review_triage, "run_triage", lambda batch, should_stop=None: slices.append(batch) or 6
    )

    assert tail_steps.review_triaged_topup({}, lambda: next(verdicts)) == 12
    assert len(slices) == 2, "the loop must RUN, then stop — not stop before working"
    assert consolidate._STOP_LOG["review_triaged_topup"] == {"reason": "battery", "deferred": 0}


def test_a_run_already_standing_down_starts_no_slice(monkeypatch, open_window):
    monkeypatch.setattr(review_triage, "run_triage", _never_called)
    assert tail_steps.review_triaged_topup({}, lambda: "deadline") == 0
    assert consolidate._STOP_LOG["review_triaged_topup"]["reason"] == "deadline"


def test_an_UNGUARDED_run_tops_up_nothing(monkeypatch, open_window):
    """UNATTENDED RUNS ONLY. The watcher executes on-demand requests (web / MCP /
    `kb consolidate-now`) as main() with stop=None — deliberately unbounded,
    because the owner asked for that run. Topping up there would turn an
    interactive request into a nine-hour triage grind holding pg_advisory_lock(42)
    until 23:00, which would ALSO make the real nightly skip: precisely the
    collision the slice design exists to avoid, entered from the other side.
    It would also be the one place the loop runs with nothing able to stop it."""
    monkeypatch.setattr(review_triage, "run_triage", _never_called)
    assert tail_steps.review_triaged_topup({}, None) == 0
    assert consolidate._STOP_LOG == {}, "not standing down — simply not this run's job"


# ------------------------------------------------------------- the kill switches


def test_an_empty_until_disables_the_whole_feature(monkeypatch):
    """No cutoff-fixture here on purpose: the REAL parse must be what says no."""
    monkeypatch.setattr(settings, "kb_triage_topup_until", "")
    monkeypatch.setattr(review_triage, "run_triage", _never_called)
    assert tail_steps.review_triaged_topup({}, lambda: None) == 0


@pytest.mark.parametrize("slice_size", [0, -1])
def test_a_non_positive_slice_disables_the_whole_feature(monkeypatch, open_window, slice_size):
    monkeypatch.setattr(settings, "kb_triage_topup_slice", slice_size)
    monkeypatch.setattr(review_triage, "run_triage", _never_called)
    assert tail_steps.review_triaged_topup({}, lambda: None) == 0


# ------------------------------------------------------------------ the digest


def test_the_digest_carries_the_topup_only_when_it_ran():
    counts = {"review_triaged": 1000, "review_triaged_topup": 412}
    assert "topup triaged: 412" in tail_steps.digest_lines(counts)

    assert not any("topup" in ln for ln in tail_steps.digest_lines({"review_triaged_topup": 0}))
    assert not any("topup" in ln for ln in tail_steps.digest_lines({"review_triaged": 1000}))


def test_the_digest_stays_short_and_ascii():
    lines = tail_steps.digest_lines({"review_triaged": 10, "review_triaged_topup": 7})
    assert lines == ["triaged: 10", "topup triaged: 7"]
    assert all(line.isascii() for line in lines)


# --------------------------------------------------- wired into the nightly run


def _report_body(item_id: int) -> str:
    with db.connect() as conn:
        return conn.execute("SELECT body FROM items WHERE id = %s", (item_id,)).fetchone()[0]


def test_the_topup_lands_in_the_report_and_the_digest(kb, monkeypatch, open_window):
    """End to end through main(): the step runs AFTER the tail and BEFORE the
    report + digest are written, so its count reaches the owner."""
    monkeypatch.setattr(settings, "kb_triage_topup_slice", 7)
    monkeypatch.setattr(consolidate.eval_sentinel, "run_sentinel", lambda counts: None)
    yields = iter([3, 0])

    def fake_triage(batch, should_stop=None):
        # The normal step passes the BATCH knob; only the top-up passes the slice.
        return next(yields) if batch == 7 else 0

    monkeypatch.setattr(review_triage, "run_triage", fake_triage)
    digest: dict = {}
    monkeypatch.setattr(
        tail_steps,
        "push_run_digest",
        lambda counts: bool(digest.update(lines=tail_steps.digest_lines(counts))),
    )

    result = consolidate.main(stop=_guard)  # the guarded, unattended shape

    assert result["review_triaged_topup"] == 3
    assert result["review_triaged"] == 0, "the normal step's fixed batch is unchanged"
    assert "review_triaged_topup" in result["step_secs"], "the step must be timed like the others"
    assert "topup triaged: 3" in digest["lines"]
    assert "- review_triaged_topup: 3" in _report_body(result["report_item_id"])


def test_the_topup_is_the_LAST_tail_step(kb, monkeypatch, open_window):
    """Position is the design: it runs after every existing tail step (so it only
    ever spends leftover window) and before the sentinel/report/digest (so its
    count is in the night's record). Same recording trick as test_tail_reserve."""
    order: list[str] = []
    monkeypatch.setattr(settings, "kb_triage_topup_slice", 7)
    monkeypatch.setattr(
        review_triage,
        "run_triage",
        lambda batch, should_stop=None: (order.append("topup" if batch == 7 else "triage"), 0)[1],
    )
    monkeypatch.setattr(
        tail_steps, "auto_apply_step", lambda counts, stop=None: (order.append("auto"), 0)[1]
    )
    monkeypatch.setattr(
        consolidate, "_contradictions_all", lambda p, s=None, c=None: (order.append("contra"), 0)[1]
    )
    monkeypatch.setattr(
        consolidate, "_evidence", lambda s=None: (order.append("evidence"), (0, 0))[1]
    )
    monkeypatch.setattr(consolidate, "_summaries", lambda since, s=None: 0)
    monkeypatch.setattr(
        consolidate.eval_sentinel, "run_sentinel", lambda counts: order.append("sentinel")
    )
    monkeypatch.setattr(
        tail_steps, "push_run_digest", lambda counts: bool(order.append("digest")) or True
    )

    consolidate.main(stop=_guard)

    assert order == ["triage", "auto", "contra", "evidence", "topup", "sentinel", "digest"]


def test_a_deadline_day_run_reports_a_zero_topup(kb, monkeypatch, open_window):
    """The key is always present (the report is read night-over-night), and it is
    0 on a day the deadline applies."""
    _deadline_day(monkeypatch)
    monkeypatch.setattr(consolidate.eval_sentinel, "run_sentinel", lambda counts: None)
    monkeypatch.setattr(review_triage, "run_triage", lambda batch, should_stop=None: 0)

    result = consolidate.main(stop=_guard)
    assert result["review_triaged_topup"] == 0
    assert "- review_triaged_topup: 0" in _report_body(result["report_item_id"])

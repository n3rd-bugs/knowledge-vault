"""The nightly drain REFILLS instead of taking one poll and going home.

Three weeks of run reports said the same thing three ways:

* **Fast nights stopped early.** ``QUEUE_BATCH`` was a per-RUN budget of 200
  ROWS, but the work is measured in JOBS. A night of one-row submissions retired
  200 groups and quit with most of its window unused; a night holding one
  350-row coalesced session spent the entire budget on that one session.
* **Old submissions could be starved.** Groups came back in the group query's
  order — effectively alphabetical by (project, uri) — so on a busy night the
  drain never reached the tail of the alphabet, and a submission could age
  indefinitely behind a lexically-luckier neighbour.
* **The fetch was unbounded in memory.** Every drainable row, content and all,
  was read into the client to hand back one batch of it.

The drain now asks for one cohort's worth of the OLDEST groups, processes it, and
asks again until the queue is empty or the stand-down guard fires. These tests
pin the loop, the ordering, the guard semantics it must not change, the row
bound, and the one thing a refill loop makes newly dangerous: re-fetching a group
this run has already deferred or failed.
"""

from __future__ import annotations

import pytest

from kbcore import ingest_queue
from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.errors import ExtractionFailed, ExtractorUnavailable
from kbworker import consolidate, sessions


@pytest.fixture(autouse=True)
def _no_stand_down(monkeypatch):
    """Disarm the wall-clock/power guard unless a test arms it deliberately.

    The guard is asked BETWEEN cohorts, and a refill loop has cohort boundaries
    where the single-poll drain had none — so a suite run after the default noon
    deadline would otherwise stop every multi-cohort test after its first batch.
    """
    monkeypatch.setattr(settings, "kb_drain_deadline", "")
    monkeypatch.setattr(
        settings, "kb_drain_deadline_days", ""
    )  # day-independent: the weekend rule must not flip tests by calendar
    monkeypatch.setattr(settings, "kb_power_db", "")
    consolidate.reset_stop_cache()
    consolidate._STOP_LOG.clear()
    yield
    consolidate.reset_stop_cache()
    consolidate._STOP_LOG.clear()


def _enqueue(project: str, body: str, source_uri: str | None = None) -> int:
    res = ingest_queue.enqueue(project, body, kind="session", source_uri=source_uri)
    assert res.id is not None, f"setup failed to queue {body!r}"
    return res.id


def _age_rows(ids: list[int], minutes: int) -> None:
    """Backdate rows: the coalescing quiet period, and the oldest-first order."""
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET created_at = now() - make_interval(mins => %s) "
            "WHERE id = ANY(%s)",
            (minutes, list(ids)),
        )


def _row(qid: int) -> tuple[str, int, int]:
    with connect() as conn:
        return conn.execute(
            "SELECT status, attempts, deferrals FROM ingest_queue WHERE id = %s", (qid,)
        ).fetchone()


def _statuses() -> dict[int, str]:
    with connect() as conn:
        return dict(conn.execute("SELECT id, status FROM ingest_queue").fetchall())


def _recorder(monkeypatch, fail_on: dict[str, Exception] | None = None) -> list[str]:
    """Record the URI of every extraction, optionally raising for one of them."""
    seen: list[str] = []

    def fake(text, project, uri, **kw):
        seen.append(uri)
        exc = (fail_on or {}).get(uri)
        if exc is not None:
            raise exc
        return 1, 0

    monkeypatch.setattr(sessions, "extract_and_ingest", fake)
    return seen


# ------------------------------------------------------------ the refill loop


def test_a_three_cohort_backlog_drains_FULLY_in_one_run(kb, monkeypatch):
    """The headline fix: the run is bounded by the window, not by one poll."""
    monkeypatch.setattr(settings, "kb_drain_cohort", 3)
    ids = [_enqueue("refill-full", f"User: q{i}\nAssistant: a{i}") for i in range(9)]
    seen = _recorder(monkeypatch)

    drained, saved = consolidate._drain_ingest_queue()

    assert (drained, saved) == (9, 9), "the run stopped short of the backlog"
    assert len(seen) == 9
    assert set(_statuses().values()) == {"extracted"}, "rows were left behind"
    assert ingest_queue.queue_depth() == 0
    assert consolidate._COHORT_STATS["cohort_refills"] == 3, "three cohorts, three refills"
    assert consolidate._COHORT_STATS["cohort_groups_considered"] == 9
    assert len(ids) == 9


def test_the_loop_stops_when_pending_is_exhausted_not_when_it_is_bored(kb, monkeypatch):
    """A refill that finds nothing ends the run — it must not spin to the
    backstop, and it must not count itself as a refill that did work."""
    monkeypatch.setattr(settings, "kb_drain_cohort", 10)
    _enqueue("refill-quiet", "User: one\nAssistant: only")
    _recorder(monkeypatch)

    assert consolidate._drain_ingest_queue() == (1, 1)
    assert consolidate._COHORT_STATS["cohort_refills"] == 1
    assert consolidate._drain_ingest_queue() == (0, 0), "an empty queue is not work"
    assert consolidate._COHORT_STATS["cohort_refills"] == 0


def test_the_refill_counts_ride_into_the_run_report(kb, monkeypatch):
    """Telemetry only exists if it reaches the report: an inert cohort path and a
    40% CPU embedding fallback both hid behind a logger.info."""
    monkeypatch.setattr(settings, "kb_drain_cohort", 2)
    monkeypatch.setattr(consolidate.eval_sentinel, "run_sentinel", lambda counts: None)
    for i in range(4):
        _enqueue("refill-report", f"User: r{i}\nAssistant: r{i}")
    _recorder(monkeypatch)

    result = consolidate.main()

    assert result["queued_drained"] == 4
    assert result["cohort_refills"] == 2
    assert result["cohort_groups_considered"] == 4
    assert result["ingest_queue_pending"] == 0


# --------------------------------------------------------------- oldest first


def test_pending_groups_returns_the_OLDEST_group_first(kb):
    """Ordering is by age, not by name. The projects here are seeded so that
    alphabetical order is the exact REVERSE of arrival order — under the old
    ordering the youngest submission drained first and the oldest sat."""
    old = _enqueue("zzz-oldest", "User: waited longest\nAssistant: x")
    mid = _enqueue("mmm-middle", "User: waited a while\nAssistant: y")
    new = _enqueue("aaa-newest", "User: just arrived\nAssistant: z")
    _age_rows([old], 600)
    _age_rows([mid], 300)
    _age_rows([new], 1)

    order = [g["project"] for g in ingest_queue.pending_groups(limit=200)]
    assert order == ["zzz-oldest", "mmm-middle", "aaa-newest"], f"not oldest-first: {order}"


def test_a_group_is_aged_by_its_OLDEST_row_not_its_newest(kb):
    """A long session keeps submitting, so its newest row is always young. Age it
    by the newest and a big session outranks the ancient note it is starving."""
    session = [
        _enqueue("age-by-oldest", f"chunk {i}\n", source_uri="session:long") for i in range(3)
    ]
    note = _enqueue("age-by-oldest", "User: a lone old note\nAssistant: x")
    # The session STARTED long ago and is still quiet-eligible; the note is newer.
    _age_rows(session, 900)
    _age_rows([session[-1]], 120)  # its newest row is much younger
    _age_rows([note], 300)

    order = [g["source_uri"] for g in ingest_queue.pending_groups(limit=200)]
    assert order[0] == "session:long", f"the group was ranked by its newest row: {order}"


def test_an_old_submission_is_not_starved_by_alphabetically_earlier_ones(kb, monkeypatch):
    """End-to-end across refills: with one group per refill, processing order IS
    the fetch order, so a starved tail-of-the-alphabet group would never run."""
    monkeypatch.setattr(settings, "kb_drain_cohort", 1)
    starved = _enqueue("zzz-starved", "User: queued days ago\nAssistant: x")
    _age_rows([starved], 4_000)
    noisy = [_enqueue("aaa-noisy", f"User: chatter {i}\nAssistant: y") for i in range(4)]
    _age_rows(noisy, 5)
    seen = _recorder(monkeypatch)

    drained, _saved = consolidate._drain_ingest_queue()

    assert drained == 5
    assert seen[0] == f"queue:session:{starved}", (
        f"the oldest submission was not extracted first: {seen}"
    )
    assert consolidate._COHORT_STATS["cohort_refills"] == 5, "one group per refill"


# ---------------------------------------------------- the stand-down guard


def test_a_deadline_mid_run_stops_BETWEEN_cohorts_and_leaves_the_rest_pending(kb, monkeypatch):
    """The guard's semantics are unchanged by refilling: the run's FIRST cohort
    is exempt (that check belongs to _drain_step), every later one — including
    the first cohort of a LATER refill — is not, and what is not reached stays
    'pending' for the next run. Nothing is lost, and the report says why."""
    monkeypatch.setattr(settings, "kb_drain_cohort", 2)
    ids = [_enqueue("refill-deadline", f"User: d{i}\nAssistant: d{i}") for i in range(6)]
    _age_rows(ids, 100)  # deterministic order: ids ascending == oldest first
    for n, qid in enumerate(ids):
        _age_rows([qid], 100 - n)
    seen = _recorder(monkeypatch)

    calls: list[int] = []

    def stop_after_the_first_cohort() -> str | None:
        calls.append(1)
        return "deadline" if seen else None

    monkeypatch.setattr(consolidate, "should_stop", stop_after_the_first_cohort)

    drained, saved = consolidate._drain_ingest_queue()

    assert (drained, saved) == (2, 2), "the drain did not stop at a cohort boundary"
    assert calls, "the guard was never asked across the refill boundary"
    statuses = _statuses()
    assert [statuses[i] for i in ids] == ["extracted"] * 2 + ["pending"] * 4, (
        "the unreached remainder is not still pending"
    )
    # Review #8 H6: the stop log carries the still-pending ROWS (4), the same number
    # as drained_deferred; the jobs unit keeps its own telemetry key (2 groups).
    assert consolidate._STOP_LOG["queued_drained"] == {"reason": "deadline", "deferred": 4}
    assert consolidate._COHORT_STATS["drained_deferred"] == 4
    assert consolidate._COHORT_STATS["deadline_deferred_jobs"] == 2

    # ...and the next run resumes exactly there.
    monkeypatch.setattr(consolidate, "should_stop", lambda: None)
    assert consolidate._drain_ingest_queue() == (4, 4)
    assert set(_statuses().values()) == {"extracted"}


def test_the_runs_FIRST_cohort_is_still_exempt_from_the_guard(kb, monkeypatch):
    """_drain_step owns 'should this run start at all'; a drain that asked the
    guard before its own first batch would double-check it and never start."""
    monkeypatch.setattr(settings, "kb_drain_cohort", 2)
    _enqueue("refill-exempt", "User: only\nAssistant: one")
    seen = _recorder(monkeypatch)
    monkeypatch.setattr(consolidate, "should_stop", lambda: "battery")

    assert consolidate._drain_ingest_queue() == (1, 1)
    assert len(seen) == 1


# ------------------------------------------- a refill must not retry this run


def test_a_DEFERRED_group_is_not_retried_within_the_same_run(kb, monkeypatch):
    """THE refill hazard. A deferred group is left 'pending' on purpose (an
    outage must never burn the retry budget) — so it is drainable again a
    millisecond later, and a naive refill would re-fetch it and spend the whole
    deferral cap inside one run instead of one deferral per night."""
    monkeypatch.setattr(settings, "kb_drain_cohort", 1)
    down = _enqueue("refill-defer", "User: outage\nAssistant: x")
    ok = _enqueue("refill-defer", "User: fine\nAssistant: y")
    _age_rows([down], 100)
    _age_rows([ok], 50)
    seen = _recorder(
        monkeypatch, fail_on={f"queue:session:{down}": ExtractorUnavailable("ollama restarting")}
    )

    drained, _saved = consolidate._drain_ingest_queue()

    assert seen.count(f"queue:session:{down}") == 1, (
        f"the deferred group was retried inside the same run: {seen}"
    )
    assert drained == 1, "the healthy group behind the deferral did not drain"
    assert _row(down) == ("pending", 0, 1), "one run must cost exactly one deferral"
    assert _row(ok)[0] == "extracted", "the refill after a deferral made no progress"


def test_a_deferred_group_IS_eligible_again_on_the_NEXT_run(kb, monkeypatch):
    """The exclusion is per RUN. Making it stick would turn a transient outage
    into a permanently stranded row, which is the failure it exists to avoid.
    The deferral BURN, though, is per LOCAL day (review #5 pipe-2): a same-day
    boot catch-up run re-offers the group but burns no second deferral."""
    from datetime import timedelta

    monkeypatch.setattr(settings, "kb_drain_cohort", 2)
    qid = _enqueue("refill-defer-next", "User: outage\nAssistant: x")
    uri = f"queue:session:{qid}"
    seen = _recorder(monkeypatch, fail_on={uri: ExtractorUnavailable("still down")})

    consolidate._drain_ingest_queue()
    assert _row(qid) == ("pending", 0, 1)

    consolidate._drain_ingest_queue()
    assert seen == [uri, uri], "the next run did not re-offer the deferred group"
    assert _row(qid) == ("pending", 0, 1), "a same-day catch-up burned a second deferral"

    # Tomorrow's run: still re-offered, and the day boundary re-arms the burn.
    # TOMORROW'S LOCAL MIDNIGHT, not now(UTC)+1d: the burn keys on the boundary's
    # civil DATE, and between local 00:00 and the UTC date change (00:00-05:00 PKT)
    # now(UTC)+1d falls on TODAY's local date, so the test failed every night.
    real_day_start = ingest_queue.drain_day_start
    monkeypatch.setattr(
        ingest_queue, "drain_day_start", lambda: real_day_start() + timedelta(days=1)
    )
    consolidate._drain_ingest_queue()
    assert seen == [uri, uri, uri], "the next DAY did not re-offer the deferred group"
    assert _row(qid) == ("pending", 0, 2), "each DAY costs exactly one deferral"


def test_a_CONTENT_FAILED_group_is_not_retried_within_the_same_run(kb, monkeypatch):
    """Same hazard, other branch: mark_failed leaves the row retryable-'failed'
    while attempts < MAX_ATTEMPTS, so it is still drainable. One run must cost
    one attempt or the bounded self-heal collapses into a single run."""
    monkeypatch.setattr(settings, "kb_drain_cohort", 1)
    bad = _enqueue("refill-fail", "User: poison\nAssistant: payload")
    uri = f"queue:session:{bad}"
    seen = _recorder(monkeypatch, fail_on={uri: ExtractionFailed("unparseable model output")})

    consolidate._drain_ingest_queue()

    assert seen == [uri], f"the failed group was retried inside the same run: {seen}"
    assert _row(bad) == ("failed", 1, 0), "one run must cost exactly one attempt"


def test_a_processed_group_is_excluded_at_the_SQL_layer_not_just_in_python(kb):
    """The exclusion has to reach the query, or the LIMIT keeps handing back the
    same group and the refill returns nothing new — a live-lock, not a drain."""
    first = _enqueue("refill-sql", "User: a\nAssistant: a")
    second = _enqueue("refill-sql", "User: b\nAssistant: b")
    _age_rows([first], 100)
    _age_rows([second], 50)

    head = ingest_queue.pending_groups(limit=200, max_groups=1)
    assert [r["id"] for g in head for r in g["rows"]] == [first]

    nxt = ingest_queue.pending_groups(
        limit=200, max_groups=1, exclude=[(g["project"], g["group_uri"]) for g in head]
    )
    assert [r["id"] for g in nxt for r in g["rows"]] == [second], "the cap re-served the same group"


# ----------------------------------------------------------- the memory bound


def test_QUEUE_BATCH_bounds_ONE_refills_rows_not_the_whole_run(kb, monkeypatch):
    """The row cap is a memory bound per fetch. It must still bound a fetch — and
    it must no longer bound the RUN."""
    ids = []
    for s in range(5):
        rows = [
            _enqueue("rowcap", f"session {s} chunk {i}\n", source_uri=f"session:{s}")
            for i in range(3)
        ]
        _age_rows(rows, 100 - s)  # aged past the quiet window, oldest session first
        ids += rows

    groups = ingest_queue.pending_groups(limit=7, max_groups=100)
    fetched = sum(len(g["rows"]) for g in groups)
    assert fetched == 6, f"the row cap fetched {fetched} rows against a limit of 7"
    assert [g["source_uri"] for g in groups] == ["session:0", "session:1"], "not oldest-first"
    assert len(ids) == 15


def test_a_single_group_over_the_row_cap_is_still_taken_ALONE(kb):
    """Skipping it would return nothing and one oversized session would wedge the
    queue permanently — a worse failure than one big fetch."""
    rows = [_enqueue("rowcap-big", f"chunk {i}\n", source_uri="session:huge") for i in range(5)]
    _age_rows(rows, 100)

    groups = ingest_queue.pending_groups(limit=2, max_groups=100)
    assert len(groups) == 1 and len(groups[0]["rows"]) == 5


def test_the_row_cap_does_not_end_the_RUN(kb, monkeypatch):
    """The regression this whole package exists for: a run whose backlog exceeds
    the per-fetch row cap must refill, not stop."""
    monkeypatch.setattr(consolidate, "QUEUE_BATCH", 2)
    monkeypatch.setattr(settings, "kb_drain_cohort", 30)
    for i in range(6):
        _enqueue("rowcap-run", f"User: n{i}\nAssistant: n{i}")
    _recorder(monkeypatch)

    drained, saved = consolidate._drain_ingest_queue()

    assert (drained, saved) == (6, 6), "the run stopped at the per-refill row cap"
    assert consolidate._COHORT_STATS["cohort_refills"] == 3, "two rows per refill, six rows"


def test_max_groups_bounds_a_call_to_one_cohorts_worth(kb):
    for i in range(5):
        _enqueue("groupcap", f"User: g{i}\nAssistant: g{i}")
    assert len(ingest_queue.pending_groups(limit=200, max_groups=2)) == 2
    assert len(ingest_queue.pending_groups(limit=200)) == 5, "no cap means no cap"


# --------------------------------------------------------------- termination


def _set_deferrals(ids: list[int], n: int) -> None:
    """Backdate a row's deferral history (kb_test-owned data) instead of running
    n nightly drains to accumulate it."""
    with transaction() as conn:
        conn.execute("UPDATE ingest_queue SET deferrals = %s WHERE id = ANY(%s)", (n, list(ids)))


# ------------------------------------------ deferral-cap visibility (review #3)


def test_a_deferral_escalation_is_counted_into_the_run_report(kb, monkeypatch):
    """An escalation to terminal 'failed' was SILENT: no report key, no digest
    line, and journald's <36h retention destroyed the log line before anyone
    looked. mark_deferred's escalated return now rides _COHORT_STATS."""
    monkeypatch.setattr(settings, "kb_drain_cohort", 1)
    qid = _enqueue("defer-cap", "User: poison\nAssistant: window")
    _set_deferrals([qid], ingest_queue.DEFERRAL_CAP - 1)
    _recorder(monkeypatch, fail_on={f"queue:session:{qid}": ExtractorUnavailable("never heals")})

    consolidate._drain_ingest_queue()

    assert _row(qid)[0] == "failed", "the deferral cap did not escalate the row"
    assert consolidate._COHORT_STATS["deferral_escalations"] == 1
    assert consolidate._COHORT_STATS["groups_near_deferral_cap"] == 0, (
        "an escalated group is 'failed' — no longer pending near the cap"
    )


def test_groups_near_the_deferral_cap_are_counted_AFTER_the_drain(kb, monkeypatch):
    """Counted at END of drain so tonight's deferral is included: a group
    arriving at DEFERRAL_CAP-2 tonight is ~2 nights from a silent terminal
    escalation, and this count is what earns it a digest line."""
    monkeypatch.setattr(settings, "kb_drain_cohort", 1)
    near = _enqueue("defer-near", "User: outage\nAssistant: x")
    _set_deferrals([near], ingest_queue.DEFERRAL_CAP - 3)  # tonight's defer -> CAP-2
    fine = _enqueue("defer-near", "User: healthy\nAssistant: y")
    _age_rows([near], 100)
    _age_rows([fine], 50)
    _recorder(monkeypatch, fail_on={f"queue:session:{near}": ExtractorUnavailable("down")})

    consolidate._drain_ingest_queue()

    assert _row(near) == ("pending", 0, ingest_queue.DEFERRAL_CAP - 2)
    assert consolidate._COHORT_STATS["groups_near_deferral_cap"] == 1
    assert consolidate._COHORT_STATS["deferral_escalations"] == 0
    assert _row(fine)[0] == "extracted", "the healthy neighbour still drained"


def test_the_refill_loop_has_a_backstop(kb, monkeypatch, caplog):
    """Clients can enqueue while the drain runs and a one-row submission is
    eligible immediately, so 'refill until empty' needs a floor under it for a
    run with no wall-clock guard at all."""
    import logging

    monkeypatch.setattr(consolidate, "MAX_REFILLS", 2)
    monkeypatch.setattr(settings, "kb_drain_cohort", 1)
    for i in range(5):
        _enqueue("backstop", f"User: b{i}\nAssistant: b{i}")
    _recorder(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="kbworker.consolidate"):
        drained, _saved = consolidate._drain_ingest_queue()

    assert drained == 2, "the backstop did not bound the loop"
    assert "refill backstop" in caplog.text, "the backstop fired silently"
    assert ingest_queue.queue_depth() == 3, "the remainder must stay pending, never lost"


# ------------------------------------- content-failure visibility (review #4)


def _set_attempts(ids: list[int], n: int) -> None:
    """Backdate a row's attempt history (kb_test-owned data) instead of running
    n nightly drains to accumulate it."""
    with transaction() as conn:
        conn.execute("UPDATE ingest_queue SET attempts = %s WHERE id = ANY(%s)", (n, list(ids)))


def _yesterday(ids: list[int]) -> None:
    """Turn the calendar: move the last-failure stamp back one day so the
    per-DAY attempt budget refreshes."""
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET processed_at = processed_at - interval '1 day' "
            "WHERE id = ANY(%s)",
            (list(ids),),
        )


def test_a_content_failure_is_counted_into_the_run_report(kb, monkeypatch):
    """The ExtractionFailed branch incremented NO counter (review #4 P0 rider),
    so a content-failing group existed only in journald (<36h retention)."""
    monkeypatch.setattr(settings, "kb_drain_cohort", 1)
    bad = _enqueue("content-count", "User: poison\nAssistant: payload")
    _recorder(monkeypatch, fail_on={f"queue:session:{bad}": ExtractionFailed("unparseable")})

    consolidate._drain_ingest_queue()

    assert consolidate._COHORT_STATS["content_failures"] == 1
    assert consolidate._COHORT_STATS["attempts_cap_retirements"] == 0, (
        "attempt 1 of 3 is not a retirement"
    )


def test_an_attempts_cap_retirement_is_counted_and_reaches_the_digest(kb, monkeypatch):
    """The P0's missing half: the terminal retirement is the queue's LAST offer
    of a group, so the owner hears it from the digest or from nobody."""
    from kbworker import tail_steps

    monkeypatch.setattr(settings, "kb_drain_cohort", 1)
    bad = _enqueue("cap-retire", "User: poison\nAssistant: payload")
    _set_attempts([bad], ingest_queue.MAX_ATTEMPTS - 1)  # tonight's failure is the last
    _recorder(monkeypatch, fail_on={f"queue:session:{bad}": ExtractionFailed("unparseable")})

    consolidate._drain_ingest_queue()

    assert _row(bad) == ("failed", ingest_queue.MAX_ATTEMPTS, 0)
    assert consolidate._COHORT_STATS["content_failures"] == 1
    assert consolidate._COHORT_STATS["attempts_cap_retirements"] == 1

    lines = tail_steps.digest_lines(dict(consolidate._COHORT_STATS))
    assert "drain: 1 group(s) retired at attempts cap" in lines
    assert not any("content" in line for line in lines), (
        "sub-cap content failures stay report-only (noise budget, review #4)"
    )


def test_the_retirement_digest_line_is_positive_only(kb):
    """A zero must contribute NO line — the digest is the one message the owner
    reads without going looking (same rule as its deferral-cap neighbours)."""
    from kbworker import tail_steps

    lines = tail_steps.digest_lines(
        {"queued_drained": 1, "attempts_cap_retirements": 0, "content_failures": 2}
    )
    assert not any("retired at attempts cap" in line for line in lines)
    assert not any("content" in line for line in lines)


# --------------------------------------- the per-DAY attempt bound (review #4)


def test_an_attempt_is_budgeted_per_DAY_not_per_run(kb, monkeypatch):
    """The 08-15 double-burn: the once-per-night policy was a per-RUN exclusion
    set, so the boot catch-up run re-drained a group that had already
    content-failed at 01:00 that morning — 2 of its 3 attempts in one day. The
    bound is now the failure's civil DAY (queue_repair.drain_day_start): failed
    at 01:00 means skipped at 21:00 and drained tomorrow, while the catch-up
    still drains everything else."""
    monkeypatch.setattr(settings, "kb_drain_cohort", 1)
    bad = _enqueue("day-budget", "User: poison\nAssistant: payload")
    uri = f"queue:session:{bad}"
    seen = _recorder(monkeypatch, fail_on={uri: ExtractionFailed("unparseable")})

    consolidate._drain_ingest_queue()  # the 01:00 nightly
    assert _row(bad) == ("failed", 1, 0)

    ok = _enqueue("day-budget", "User: healthy\nAssistant: fine")
    consolidate._drain_ingest_queue()  # the 21:00 boot catch-up, same day
    assert seen.count(uri) == 1, "the same-day catch-up re-burned an attempt (review #4)"
    assert _row(bad) == ("failed", 1, 0), "one day must cost exactly one attempt"
    assert _row(ok)[0] == "extracted", "the day bound must not hold back the rest of the catch-up"

    _yesterday([bad])  # the calendar turns
    consolidate._drain_ingest_queue()  # tomorrow's nightly
    assert seen.count(uri) == 2, "the next day did not re-offer the group"
    assert _row(bad) == ("failed", 2, 0)


def test_a_requeued_row_is_eligible_the_SAME_day(kb):
    """The owner's requeue (and the enqueue re-arm) resets processed_at to NULL:
    a deliberate retry is a fresh grant, not tomorrow's business."""
    bad = _enqueue("day-requeue", "User: poison\nAssistant: payload")
    ingest_queue.mark_failed(bad, "boom")
    assert ingest_queue.pending_groups(limit=10) == [], "failed TODAY must be skipped"

    ingest_queue.requeue_failed(reason="pipeline fixed")

    groups = ingest_queue.pending_groups(limit=10)
    assert [r["id"] for g in groups for r in g["rows"]] == [bad]


def test_mark_failed_reports_whether_the_cap_was_hit(kb):
    """The bool is what lets the drain count a retirement without a second
    query; sub-cap failures must stay False."""
    qid = _enqueue("cap-bool", "User: a\nAssistant: b")
    for expect in (False,) * (ingest_queue.MAX_ATTEMPTS - 1) + (True,):
        assert ingest_queue.mark_failed(qid, "boom") is expect

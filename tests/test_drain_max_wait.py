"""KB 57972: an always-on session drains once its oldest row has waited the bound.

The quiet window (``queue_groups.COALESCE_QUIET_MINUTES``) keeps a session that is
still being written from being extracted in halves. kb-flush-sweep backfills a live
session every 1800 s (+120 s jitter), so an always-on session gains a row every
30-32 minutes and its newest row is almost never 30 minutes old: trader session
c30a5bed had rows waiting at 11 of 34 nightly drains with none taken (2026-09-30 to
11-02). The oldest row's age now bounds the wait (``COALESCE_MAX_WAIT_HOURS``). These
tests pin the bound, the flag and counter that report it, the property that makes
draining a live session safe (rows that land during the drain stay pending), and the
checkpoint pin that lets an interrupted live group resume although it has grown.
"""

from __future__ import annotations

import pytest

from kbcore import ingest_queue, queue_groups
from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.errors import ExtractionFailed, ExtractorUnavailable
from kbworker import consolidate, prep, sessions

URI = "session:always-on"
W = queue_groups.COALESCE_MAX_WAIT_HOURS


@pytest.fixture(autouse=True)
def _no_stand_down(monkeypatch):
    """Disarm the wall-clock/power guard (same reason as tests/test_drain_refill.py)."""
    monkeypatch.setattr(settings, "kb_drain_deadline", "")
    monkeypatch.setattr(settings, "kb_drain_deadline_days", "")
    monkeypatch.setattr(settings, "kb_power_db", "")
    consolidate.reset_stop_cache()
    consolidate._STOP_LOG.clear()
    yield
    consolidate.reset_stop_cache()
    consolidate._STOP_LOG.clear()


def _row_aged(project: str, body: str, minutes: int, uri: str | None = URI) -> int:
    res = ingest_queue.enqueue(project, body, kind="session", source_uri=uri)
    assert res.id is not None, f"setup failed to queue {body!r}"
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET created_at = now() - make_interval(mins => %s) WHERE id = %s",
            (minutes, res.id),
        )
    return res.id


def _always_on(hours: float, newest_minutes: int = 5) -> list[int]:
    """What the sweep leaves for a session that never stops: a row every 31 minutes
    over ``hours``, the newest ``newest_minutes`` old. Returned oldest first."""
    ages = sorted(range(newest_minutes, int(hours * 60) + 1, 31), reverse=True)
    return [
        _row_aged("always-on", f"User: turn {n}\nAssistant: reply {n}\n", age)
        for n, age in enumerate(ages)
    ]


def _session_groups(**kw) -> list[dict]:
    return [g for g in ingest_queue.pending_groups(limit=500, **kw) if g["group_uri"] == URI]


def _status(ids: list[int]) -> dict[int, str]:
    with connect() as conn:
        return dict(
            conn.execute(
                "SELECT id, status FROM ingest_queue WHERE id = ANY(%s)", (ids,)
            ).fetchall()
        )


def test_the_bound_sits_between_the_quiet_window_and_the_nightly_period():
    # One drain a day (kb-consolidate.timer, 01:30). Last night's leftovers land up to one
    # sweep (1800 s + 120 s jitter + 1 min accuracy) after its fetch: a bound at or above
    # 24 h minus that gap drained an always-on session only every other night.
    assert queue_groups.COALESCE_QUIET_MINUTES < W * 60 < 24 * 60 - 33


def test_a_live_session_younger_than_the_bound_still_waits_for_quiet(kb):
    _always_on(hours=W - 4)
    assert _session_groups() == [], "the quiet window must still hold a young live session"


def test_an_always_on_session_drains_once_its_oldest_row_has_waited(kb):
    ids = _always_on(hours=W + 2)
    (group,) = _session_groups()
    assert [r["id"] for r in group["rows"]] == ids, "the whole backlog, in byte order"
    assert group["overdue"] is True


def test_without_the_bound_the_same_session_starves(kb):
    """The KB 57972 failure, reproduced on the same rows with the bound out of reach."""
    _always_on(hours=W + 2)
    assert _session_groups(max_wait_hours=10**6) == []


def test_a_quiet_group_and_a_lone_row_are_not_overdue(kb):
    for n, age in enumerate((1600, 1500, 60)):  # quiet for an hour: drains on quiet alone
        _row_aged("always-on", f"User: q{n}\nAssistant: a{n}\n", age)
    lone = _row_aged("lone", "User: one old note\nAssistant: x\n", 2000, uri=None)

    groups = {g["group_uri"]: g for g in ingest_queue.pending_groups(limit=500)}
    assert groups[URI]["overdue"] is False
    assert groups[f"queue:session:{lone}"]["overdue"] is False


def test_rows_that_land_during_the_drain_stay_pending(kb, monkeypatch):
    """Draining a live session is safe only because the flip is BY ID."""
    ids = _always_on(hours=W + 2)
    late: list[int] = []

    def extract(text, project, uri, **kw):
        if not late:  # the sweep lands a row while the drain is extracting
            late.append(_row_aged("always-on", "User: after the fetch\nAssistant: z\n", 0))
        return 1, 0

    monkeypatch.setattr(sessions, "extract_and_ingest", extract)
    assert consolidate._drain_ingest_queue()[0] == len(ids)
    status = _status([*ids, *late])
    assert {status[i] for i in ids} == {"extracted"}
    assert status[late[0]] == "pending", "a row the drain never read was flipped"


def test_the_drain_takes_the_overdue_group_and_reports_it(kb, monkeypatch):
    ids = _always_on(hours=W + 2)
    _row_aged("lone", "User: a fresh note\nAssistant: x\n", 5, uri=None)  # NOT overdue
    monkeypatch.setattr(sessions, "extract_and_ingest", lambda text, project, uri, **kw: (1, 0))

    drained, _saved = consolidate._drain_ingest_queue()

    assert drained == len(ids) + 1
    assert set(_status(ids).values()) == {"extracted"}
    assert consolidate._COHORT_STATS["cohort_groups_considered"] == 2
    assert consolidate._COHORT_STATS["cohort_groups_overdue"] == 1


def test_an_always_on_session_drains_on_consecutive_nights(kb, monkeypatch):
    """Night 1's leftovers start landing minutes after its fetch, so at the next 01:30 the
    oldest is just UNDER 24 h old: a 24 h bound skipped every other night (0 == 47)."""
    monkeypatch.setattr(sessions, "extract_and_ingest", lambda text, project, uri, **kw: (1, 0))
    night1 = _always_on(hours=W + 2)
    assert consolidate._drain_ingest_queue()[0] == len(night1)
    night2 = [  # first row 10 min after night 1's fetch, then every 31 min; newest 4 min old
        _row_aged("always-on", f"User: n2 {k}\nAssistant: r\n", 24 * 60 - 10 - 31 * k)
        for k in range(47)
    ]
    assert consolidate._drain_ingest_queue()[0] == len(night2)


def _four_windows(text: str) -> list[str]:
    n = max(1, len(text) // 4)
    return [text[i * n : (i + 1) * n] for i in range(3)] + [text[3 * n :]]


def test_an_interrupted_overdue_group_resumes_although_the_session_grew(kb, monkeypatch):
    """The checkpoint fingerprints the WHOLE assembled text, and a live session has always
    grown by the next run: without the pin every banked window was extracted again."""
    monkeypatch.setattr(prep, "_fit_windows", _four_windows)
    monkeypatch.setattr(settings, "kb_drain_window_checkpoint", True)
    ids = _always_on(hours=W + 2)
    night, seen = [1], []

    def extract(text, project, uri, **kw):
        seen.append(kw["extra_detail"]["window"])
        if night[0] == 1 and seen[-1] == 2:
            raise ExtractorUnavailable("ollama restarting")  # night 1 defers at window 2
        return 1, 0

    monkeypatch.setattr(sessions, "extract_and_ingest", extract)
    assert consolidate._drain_ingest_queue()[0] == 0
    late = _row_aged("always-on", "User: the next sweep\nAssistant: z\n", 1)
    night[0], seen[:] = 2, []

    assert consolidate._drain_ingest_queue()[0] == len(ids)
    assert seen == [2, 3], "windows 0-1 were banked on the night before"
    assert consolidate._COHORT_STATS["cohort_windows_resumed"] == 2
    status = _status([*ids, late])
    assert {status[i] for i in ids} == {"extracted"} and status[late] == "pending"
    assert consolidate._drain_ingest_queue()[0] == 1, "the late row drains as the next piece"


def test_a_partial_attempts_cap_retirement_reaches_the_digest(kb, monkeypatch):
    """An always-on group's rows join on different nights, so a content failure retires
    its oldest rows while the newer ones retry. The group-level line counts only a group
    whose EVERY row went terminal; the partial retirement must not be silent."""
    from kbworker import tail_steps

    ids = _always_on(hours=W + 2)
    old = ids[: len(ids) // 2]
    with transaction() as conn:  # the older half has already failed twice
        conn.execute(
            "UPDATE ingest_queue SET attempts = %s WHERE id = ANY(%s)",
            (ingest_queue.MAX_ATTEMPTS - 1, old),
        )

    def poison(text, project, uri, **kw):
        raise ExtractionFailed("unparseable")

    monkeypatch.setattr(sessions, "extract_and_ingest", poison)
    consolidate._drain_ingest_queue()

    assert set(_status(old).values()) == {"failed"}
    assert consolidate._COHORT_STATS["attempts_cap_retirements"] == 0
    assert consolidate._COHORT_STATS["attempts_cap_rows_retired"] == len(old)
    lines = tail_steps.digest_lines(dict(consolidate._COHORT_STATS))
    assert f"drain: {len(old)} row(s) of a still-retrying group retired at attempts cap" in lines

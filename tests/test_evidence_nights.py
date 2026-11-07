"""Evidence streaks count NIGHTS, not passes (KB 61594).

2026-11-07: two clean reboots each started a catch-up consolidation pass. The first gave five
rows the second refresh of their suggestion about 10 hours after the 01:30 nightly gave the
first, so they were auto-resolved the same day, though the owner's rule asks for two nightly
refreshes. Since then a streak step needs a pass that STARTED ``evidence.STREAK_MIN_GAP``
(23 h) after the pass that took the step before. Real DB (kb_test); the evidence judge is a
fixed suggestion, as the recency tests' is.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kbcore import db, evidence
from kbcore.consolidation import record_contradiction
from kbcore.ingest import ingest
from kbworker import consolidate

T0 = datetime(2026, 11, 6, 20, 30, tzinfo=UTC)  # the 01:30 PKT nightly of 2026-11-07
H = timedelta(hours=1)
STAMP = evidence.STREAK_STAMP


def _sugg(keep: int = 1, retire: int = 2) -> dict:
    return {"action": "supersede", "keep": keep, "retire": retire, "rule": "evidence: test"}


def _prior(streak: int, **extra) -> dict:
    return {"suggested_resolution": _sugg(), "suggestion_streak": streak, **extra}


# ---- the rule (kbcore.evidence.next_streak) -------------------------------------------


def test_the_gap_is_just_under_a_day():
    assert evidence.STREAK_MIN_GAP == timedelta(hours=23)


def test_no_suggestion_is_zero_and_unstamped():
    assert evidence.next_streak(_prior(1, **{STAMP: T0.isoformat()}), None, T0 + 30 * H) == (
        0,
        None,
    )


def test_a_new_suggestion_starts_at_one_stamped_with_this_pass():
    assert evidence.next_streak({}, _sugg(), T0) == (1, T0.isoformat())


def test_a_changed_suggestion_starts_again_at_one():
    prior = {"suggested_resolution": _sugg(2, 1), "suggestion_streak": 3, STAMP: "x"}
    assert evidence.next_streak(prior, _sugg(), T0) == (1, T0.isoformat())


def test_streak_zero_with_the_same_suggestion_starts_at_one():
    assert evidence.next_streak(_prior(0), _sugg(), T0) == (1, T0.isoformat())


def test_the_same_suggestion_a_night_later_advances():
    later = T0 + 24 * H
    assert evidence.next_streak(_prior(1, **{STAMP: T0.isoformat()}), _sugg(), later) == (
        2,
        later.isoformat(),
    )


@pytest.mark.parametrize(
    "gap", [10 * H, 13 * H + 21 * timedelta(minutes=1), 19 * H, 23 * H - timedelta(seconds=1)]
)
def test_a_pass_sooner_than_the_gap_keeps_the_streak_and_its_stamp(gap):
    prior = _prior(1, **{STAMP: T0.isoformat()})
    assert evidence.next_streak(prior, _sugg(), T0 + gap) == (1, T0.isoformat())


def test_a_too_soon_pass_keeps_a_due_row_due():
    """A row already at streak 2 (a gate retry, a battery-broken phase 2) stays a candidate in
    a same-night pass: the rule never takes a streak down."""
    prior = _prior(2, **{STAMP: T0.isoformat()})
    assert evidence.next_streak(prior, _sugg(), T0 + 10 * H) == (2, T0.isoformat())


def test_a_pass_exactly_at_the_gap_advances():
    assert evidence.next_streak(_prior(1, **{STAMP: T0.isoformat()}), _sugg(), T0 + 23 * H)[0] == 2


def test_a_row_streaked_before_the_stamp_is_judged_by_its_last_refresh():
    refreshed = T0 + 6 * H  # the old code set the streak at the refresh, mid-pass
    prior = _prior(1, evidence_updated=refreshed.isoformat())
    # the next nightly starts 18 h after that refresh: not yet, and the stamp is kept
    assert evidence.next_streak(prior, _sugg(), T0 + 24 * H) == (1, refreshed.isoformat())
    stamped = _prior(1, **{STAMP: refreshed.isoformat()})
    assert evidence.next_streak(stamped, _sugg(), refreshed + 23 * H)[0] == 2


def test_a_row_with_neither_time_advances_as_before():
    assert evidence.next_streak(_prior(1), _sugg(), T0)[0] == 2


def test_an_unreadable_stamp_advances_as_before():
    assert evidence.next_streak(_prior(1, **{STAMP: "not a time"}), _sugg(), T0)[0] == 2


def test_a_naive_stamp_reads_as_utc():
    naive = T0.replace(tzinfo=None).isoformat()
    assert evidence.next_streak(_prior(1, **{STAMP: naive}), _sugg(), T0 + 10 * H) == (1, naive)


# ---- the evidence pass (kbworker.evidence_step._evidence) ------------------------------


@pytest.fixture()
def row(kb, monkeypatch):
    """``(keep, retire, queue_id)``: a contradiction row whose evidence always suggests
    retiring ``retire``; both items were created together, so the recency guard stays out."""
    keep = ingest(
        "nights", "qa", "mode 0 is current", "use mode 0", uri="n-keep", trust="unverified"
    )
    retire = ingest(
        "nights", "qa", "mode 1 is current", "use mode 1", uri="n-ret", trust="unverified"
    )
    assert record_contradiction(keep, retire) is True
    with db.connect() as conn:
        qid = conn.execute(
            "SELECT id FROM review_queue WHERE reason = 'contradiction' "
            "AND (detail->>'a_id')::bigint = %s",
            (keep,),
        ).fetchone()[0]

    def gather(a: int, b: int) -> dict:
        return {
            "a_id": a,
            "b_id": b,
            "evidence_updated": datetime.now(UTC).isoformat(),
            "suggested_resolution": _sugg(keep, retire),
        }

    monkeypatch.setattr(evidence, "gather_evidence", gather)
    monkeypatch.setattr(consolidate, "EVIDENCE_PAIR_CAP", 10)
    return keep, retire, qid


def _detail(qid: int) -> tuple[bool, dict]:
    with db.connect() as conn:
        return conn.execute(
            "SELECT resolved, detail FROM review_queue WHERE id = %s", (qid,)
        ).fetchone()


def _status(item_id: int) -> str:
    with db.connect() as conn:
        return conn.execute("SELECT status FROM items WHERE id = %s", (item_id,)).fetchone()[0]


def _pass(at: datetime) -> tuple[int, int, int]:
    return consolidate._evidence(pass_at=at.isoformat())


def test_catch_up_passes_the_same_day_refresh_but_never_resolve(row):
    """The 2026-11-07 timeline: the 01:30 nightly, catch-ups at 14:51 and 20:32 PKT, and
    the next 01:30 nightly. Only the last may resolve."""
    keep, retire, qid = row
    assert _pass(T0) == (1, 0, 0)
    for catch_up in (T0 + 13 * H + timedelta(minutes=21), T0 + 19 * H + timedelta(minutes=2)):
        assert _pass(catch_up) == (1, 0, 0), "a catch-up pass resolved on the same night"
        resolved, detail = _detail(qid)
        assert not resolved and detail["suggestion_streak"] == 1
        assert detail[STAMP] == T0.isoformat(), "a catch-up moved the stamp"
    assert _pass(T0 + 24 * H) == (1, 1, 0), "the next nightly did not resolve"
    assert _status(retire) == "superseded" and _status(keep) == "active"


def test_a_suggestion_first_seen_in_a_late_catch_up_waits_a_full_night(row):
    keep, retire, qid = row
    first = T0 + 19 * H  # a reboot's catch-up in the evening
    assert _pass(first) == (1, 0, 0)
    assert _pass(T0 + 24 * H) == (1, 0, 0), "resolved 5 h after the first sighting"
    assert _detail(qid)[1]["suggestion_streak"] == 1
    assert _pass(T0 + 48 * H) == (1, 1, 0)
    assert _status(retire) == "superseded"


def test_a_pass_without_a_start_counts_from_now(row):
    _keep, _retire, qid = row
    assert consolidate._evidence() == (1, 0, 0)
    assert consolidate._evidence() == (1, 0, 0), "two passes seconds apart are one night"
    assert _detail(qid)[1]["suggestion_streak"] == 1


def test_main_hands_its_start_to_the_evidence_pass(kb, monkeypatch):
    seen: dict = {}

    def spy(stop=None, pass_at=None):
        seen["pass_at"] = pass_at
        return 0, 0, 0

    monkeypatch.setattr(consolidate, "_evidence", spy)
    result = consolidate.main()
    assert seen["pass_at"] == result["run_started_at"]

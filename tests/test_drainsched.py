"""Day-aware deadline: weekdays protect the daytime GPU; weekends run unbounded."""

from __future__ import annotations

from datetime import datetime

import pytest

from kbcore import drainsched
from kbcore.config import settings

MON = datetime(2026, 8, 3, 9, 0)
SAT = datetime(2026, 8, 1, 9, 0)
SUN = datetime(2026, 8, 2, 9, 0)


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "kb_drain_deadline", "12:00")
    monkeypatch.setattr(settings, "kb_drain_deadline_days", "mon,tue,wed,thu,fri")


def test_weekdays_keep_the_deadline():
    assert drainsched.effective_deadline(MON) == "12:00"


def test_weekends_run_unbounded():
    """Owner decision 2026-08-02: the noon boundary protects the WORKDAY GPU;
    on weekends the box is free and the tail finally gets its full turn."""
    assert drainsched.effective_deadline(SAT) == ""
    assert drainsched.effective_deadline(SUN) == ""


def test_a_sunday_run_still_stops_at_monday_noon():
    """The check is evaluated live, so a weekend run that survives into Monday
    picks the deadline back up — the weekend must not leak into the work week."""
    assert drainsched.effective_deadline(SUN) == ""
    assert drainsched.effective_deadline(datetime(2026, 8, 3, 11, 59)) == "12:00"


def test_empty_days_means_every_day(monkeypatch):
    monkeypatch.setattr(settings, "kb_drain_deadline_days", "")
    assert drainsched.effective_deadline(SAT) == "12:00"


def test_malformed_days_fail_toward_gpu_protection(monkeypatch, caplog):
    """A typo must not silently grant a 24h workday run — the failure mode the
    deadline exists to prevent. Apply every day and say so."""
    import logging

    monkeypatch.setattr(settings, "kb_drain_deadline_days", "mon,tues,banana")
    with caplog.at_level(logging.WARNING, logger="kbcore.drainsched"):
        assert drainsched.effective_deadline(SAT) == "12:00"
    assert "unknown day" in caplog.text


def test_no_deadline_configured_stays_no_deadline(monkeypatch):
    monkeypatch.setattr(settings, "kb_drain_deadline", "")
    assert drainsched.effective_deadline(MON) == ""


def test_is_unbounded_day_is_the_deadline_read_the_other_way_round():
    """The nightly's weekend triage top-up asks THIS, so that "the box is free
    tonight" has exactly one definition and cannot drift from the deadline."""
    assert drainsched.is_unbounded_day(SAT) is True
    assert drainsched.is_unbounded_day(SUN) is True
    assert drainsched.is_unbounded_day(MON) is False


def test_is_unbounded_day_follows_the_deadline_being_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "kb_drain_deadline", "")
    assert drainsched.is_unbounded_day(MON) is True


def test_is_unbounded_day_fails_toward_gpu_protection_on_a_typo(monkeypatch):
    """A malformed day list applies the deadline every day, so no day is
    'unbounded' — the top-up must not be what a typo silently switches on."""
    monkeypatch.setattr(settings, "kb_drain_deadline_days", "mon,tues,banana")
    assert drainsched.is_unbounded_day(SAT) is False


def test_the_consolidate_guard_routes_through_the_schedule(monkeypatch):
    """_past_deadline must consult the day-aware value, or the weekend feature
    exists only in this test file."""
    from kbworker import consolidate

    monkeypatch.setattr("kbcore.drainsched.effective_deadline", lambda now=None: "")
    assert consolidate._past_deadline() is False
    monkeypatch.setattr("kbcore.drainsched.effective_deadline", lambda now=None: "00:00")
    assert consolidate._past_deadline() is True

"""The advisory lock is only as good as every process agreeing on its key.

Changing the derivation once silently disabled mutual exclusion: a worker started
before the schema-scoping commit kept legacy key 42 in resident bytecode while a
new process took 2764317, so pg_try_advisory_lock never contended and two
consolidations ran on one GPU for 30 hours. Nothing logged, nothing errored.
These tests pin the signal that would have caught it.
"""

from __future__ import annotations

import logging

import pytest

from kbcore import db

pytestmark = pytest.mark.usefixtures("kb")


def test_a_foreign_advisory_key_is_reported_as_a_mismatch(caplog):
    """A DIFFERENT key held concurrently means mutual exclusion is not in effect."""

    class FakeConn:
        def execute(self, sql, params=None):
            class R:
                @staticmethod
                def fetchall():
                    return [(42, 154388)]  # the legacy key, as in the real incident

            return R()

    with caplog.at_level(logging.WARNING, logger="kbcore.db"):
        db._warn_on_foreign_advisory_keys(FakeConn(), 2764317, True)

    msg = caplog.text
    assert "MISMATCH" in msg, "a key split was not flagged"
    assert "42" in msg and "2764317" in msg, "the message names neither key"
    assert "154388" in msg, "the message does not identify the other holder"


def test_no_warning_when_every_holder_agrees(caplog):
    """The normal case must stay quiet, or the warning becomes noise and is ignored."""

    class FakeConn:
        def execute(self, sql, params=None):
            class R:
                @staticmethod
                def fetchall():
                    return []

            return R()

    with caplog.at_level(logging.DEBUG, logger="kbcore.db"):
        db._warn_on_foreign_advisory_keys(FakeConn(), 2764317, True)

    assert "MISMATCH" not in caplog.text
    assert "2764317" in caplog.text, "the key we took is not recorded at all"


def test_the_peer_check_never_breaks_the_lock_path(caplog):
    """Observability must not be able to fail a consolidation."""

    class ExplodingConn:
        def execute(self, sql, params=None):
            raise RuntimeError("pg_locks unavailable")

    with caplog.at_level(logging.DEBUG, logger="kbcore.db"):
        db._warn_on_foreign_advisory_keys(ExplodingConn(), 1, False)  # must not raise


def test_the_real_lock_path_still_grants_and_releases():
    """The added read must not disturb acquisition semantics."""
    with db.advisory_lock(4242) as first:
        assert first is True
        with db.advisory_lock(4242) as second:
            assert second is False, "a second holder was not refused"
    with db.advisory_lock(4242) as again:
        assert again is True, "the lock was not released"

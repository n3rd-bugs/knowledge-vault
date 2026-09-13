"""kbcore.db._connect_retrying — a REFUSED connection is retried for a bounded
window, everything else raises at once (2026-09-13: a dockerd restart under the
running nightly refused connections for ~10 s and killed the run mid-step)."""

from __future__ import annotations

import psycopg
import pytest

from kbcore import db
from kbcore.config import settings

REFUSED = (
    'connection failed: connection to server at "127.0.0.1", port 5433 failed: Connection refused'
)


class _Conn:
    pass


def _script(monkeypatch, outcomes: list):
    """psycopg.connect stand-in: pops one outcome per call (exception -> raise)."""
    calls: list[dict] = []

    def fake_connect(url, **kw):
        calls.append({"url": url, **kw})
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    sleeps: list[float] = []
    monkeypatch.setattr(db.psycopg, "connect", fake_connect)
    monkeypatch.setattr(db.time, "sleep", lambda s: sleeps.append(s))
    return calls, sleeps


def test_a_refused_connection_is_retried_until_it_succeeds(monkeypatch, caplog):
    monkeypatch.setattr(settings, "kb_db_connect_retry_secs", 60.0)
    conn = _Conn()
    calls, sleeps = _script(
        monkeypatch, [psycopg.OperationalError(REFUSED), psycopg.OperationalError(REFUSED), conn]
    )
    assert db._connect_retrying("postgresql://x") is conn
    assert len(calls) == 3 and sleeps == [db._CONNECT_RETRY_SLEEP] * 2
    warnings = [r for r in caplog.records if "refused the connection" in r.getMessage()]
    assert len(warnings) == 1, "one WARNING per retry episode, not one per attempt"


def test_a_zero_budget_fails_at_once(monkeypatch):
    monkeypatch.setattr(settings, "kb_db_connect_retry_secs", 0.0)
    calls, sleeps = _script(monkeypatch, [psycopg.OperationalError(REFUSED), _Conn()])
    with pytest.raises(psycopg.OperationalError):
        db._connect_retrying("postgresql://x")
    assert len(calls) == 1 and sleeps == []


def test_a_non_refusal_never_retries(monkeypatch):
    monkeypatch.setattr(settings, "kb_db_connect_retry_secs", 60.0)
    calls, sleeps = _script(
        monkeypatch,
        [psycopg.OperationalError('FATAL:  password authentication failed for user "kb"'), _Conn()],
    )
    with pytest.raises(psycopg.OperationalError):
        db._connect_retrying("postgresql://x")
    assert len(calls) == 1 and sleeps == []


def test_the_deadline_bounds_the_retry(monkeypatch):
    monkeypatch.setattr(settings, "kb_db_connect_retry_secs", 5.0)
    clock = iter([0.0, 0.0, 6.0, 6.0])  # first check inside the budget, second past it
    monkeypatch.setattr(db.time, "monotonic", lambda: next(clock))
    calls, sleeps = _script(
        monkeypatch, [psycopg.OperationalError(REFUSED), psycopg.OperationalError(REFUSED), _Conn()]
    )
    with pytest.raises(psycopg.OperationalError):
        db._connect_retrying("postgresql://x")
    assert len(calls) == 2 and len(sleeps) == 1


def test_connect_goes_through_the_retrying_helper(monkeypatch):
    """Both arms of db.connect (public and schema-scoped) use the helper."""
    seen: list[str] = []
    monkeypatch.setattr(db, "_connect_retrying", lambda url, **kw: seen.append(url) or _Fake())
    monkeypatch.setattr(settings, "kb_db_schema", "public")
    monkeypatch.delenv("KB_TEST_GUARD", raising=False)
    db.connect()
    assert seen == [settings.db_url]


class _Fake:
    def execute(self, *_a, **_k):
        class _R:
            def fetchone(self):
                return ("public",)

        return _R()

    def rollback(self):
        pass

    def close(self):
        pass

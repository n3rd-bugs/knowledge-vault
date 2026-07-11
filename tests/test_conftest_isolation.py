"""Proof for the autouse kb_test isolation guard (tests/conftest.py).

A DB-touching test that forgets the ``kb``/``kb_schema`` fixture must NEVER run
SQL against the live ``public`` schema. The session-autouse ``_force_test_schema``
fixture guarantees the pointer is ``kb_test`` throughout the session, so this
test asserts it WITHOUT requesting any DB fixture (and touches no DB itself).
"""

from __future__ import annotations

import pytest

from kbcore import db
from kbcore.config import settings


def test_schema_is_kb_test_without_any_db_fixture():
    assert settings.kb_db_schema == "kb_test"


def _db_reachable() -> bool:
    """True if the compose Postgres answers on the PUBLIC schema (the public path
    never trips the missing-schema guard, so this probes reachability cleanly)."""
    original = settings.kb_db_schema
    settings.kb_db_schema = "public"
    try:
        return db.ping()
    except Exception:
        return False
    finally:
        settings.kb_db_schema = original


def test_connect_refuses_a_missing_non_public_schema(monkeypatch):
    """The real closure of the isolation hole: db.connect() must RAISE (never fall
    through to public) when the configured non-public schema does not resolve —
    not merely leave settings.kb_db_schema flipped (which Postgres would silently
    ignore via the search_path public fallback)."""
    if not _db_reachable():
        pytest.skip("compose Postgres not reachable")
    monkeypatch.setattr(settings, "kb_db_schema", "kb_test_absent_schema_xyz")
    with pytest.raises(RuntimeError, match="does not resolve"):
        db.connect()

"""Proof for the autouse kb_test isolation guard (tests/conftest.py).

A DB-touching test that forgets the ``kb``/``kb_schema`` fixture must NEVER run
SQL against the live ``public`` schema. The session-autouse ``_force_test_schema``
fixture guarantees the pointer is ``kb_test`` throughout the session, so this
test asserts it WITHOUT requesting any DB fixture (and touches no DB itself).
"""

from __future__ import annotations

from kbcore.config import settings


def test_schema_is_kb_test_without_any_db_fixture():
    assert settings.kb_db_schema == "kb_test"

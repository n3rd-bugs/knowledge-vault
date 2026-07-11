"""Real-DB tests for the migration runner (against the compose Postgres).

Regression coverage for the Phase 0 rollback bug: a SELECT outside a
transaction block opened psycopg's implicit transaction, demoting the
migration's transaction() to a savepoint that conn.close() rolled back —
migrate printed "applied" but persisted nothing.
"""

from __future__ import annotations

import pytest

from kbcore import db
from kbcore.migrate import migrate


def _db_up() -> bool:
    try:
        return db.ping()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_up(), reason="compose Postgres not reachable")


def test_migrate_persists_and_is_idempotent(kb_schema):
    # Run against the ISOLATED kb_test schema (the kb_schema fixture already
    # created + migrated it). Asserting the layout in kb_test — not public — is
    # required now that db.connect() refuses to fall through a missing schema to
    # live public data (a test that touched public via that fallback is exactly
    # the isolation hole this batch closed).
    with db.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'kb_test'"
            ).fetchall()
        }
    expected = {
        "schema_migrations",
        "projects",
        "sources",
        "items",
        "chunks",
        "entities",
        "relations",
        "access_log",
        "review_queue",
        "session_checkpoints",
    }
    assert expected <= tables, f"missing tables: {expected - tables}"

    # Second run must be a no-op (idempotent).
    assert migrate() == []

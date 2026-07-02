"""kbcore.db — the only module that talks to Postgres (CLAUDE.md hard rule).

psycopg3 connections with explicit transactions. Parameterized SQL only — never
f-string user values into a query.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from kbcore.config import settings


def connect() -> psycopg.Connection:
    """Open a new connection. The caller owns its lifecycle.

    Prefer :func:`transaction` for write paths so commit/rollback is automatic.
    When ``kb_db_schema`` is not ``public`` (tests use ``kb_test``), the
    session search_path targets that schema first, falling back to public for
    the pgvector extension types.
    """
    kwargs: dict = {}
    if settings.kb_db_schema != "public":
        kwargs["options"] = f"-c search_path={settings.kb_db_schema},public"
    return psycopg.connect(settings.db_url, **kwargs)


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    """Yield a connection inside one transaction: commit on success, rollback on error."""
    conn = connect()
    try:
        with conn.transaction():
            yield conn
    finally:
        conn.close()


def ping() -> bool:
    """Return True if the database answers ``SELECT 1``. Used by health checks/tests."""
    with connect() as conn:
        return conn.execute("SELECT 1").fetchone()[0] == 1

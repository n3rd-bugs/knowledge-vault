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

    SAFETY (review finding): Postgres SILENTLY skips a nonexistent schema in
    search_path and resolves unqualified names in the NEXT entry — so a
    ``search_path=kb_test,public`` connection whose ``kb_test`` does not exist
    would fall straight through to LIVE ``public`` data. The autouse test guard
    that flips the schema pointer therefore protected nothing on its own. For a
    NON-public schema we verify it actually resolves and refuse the connection
    otherwise, closing the hole at the connection layer. The ``public`` fast path
    (production) adds no round trip and is unchanged.
    """
    schema = settings.kb_db_schema
    if schema == "public":
        return psycopg.connect(settings.db_url)
    conn = psycopg.connect(settings.db_url, options=f"-c search_path={schema},public")
    # current_schema() returns the FIRST EXISTING schema in the search_path, so it
    # equals the configured schema exactly when that schema exists, and reads
    # 'public' precisely when it is missing — the case we must reject loudly.
    current = conn.execute("SELECT current_schema()").fetchone()[0]
    conn.rollback()  # clear the implicit tx opened by the probe
    if current != schema:
        conn.close()
        raise RuntimeError(
            f"configured KB_DB_SCHEMA {schema!r} does not resolve "
            f"(current_schema()={current!r}); refusing to fall back to {current!r} "
            "to protect live data"
        )
    return conn


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


@contextmanager
def advisory_lock(key: int) -> Iterator[bool]:
    """Hold a session-level ``pg_advisory_lock`` for the block (ARCH-5).

    Yields whether the lock was acquired. Uses its OWN autocommit connection so
    the lock survives the whole ``with`` regardless of the transaction()
    connections the work opens (each opens/closes its own), and is released when
    this connection closes. Non-blocking: a second holder yields ``False`` so the
    caller can skip rather than queue.
    """
    conn = connect()
    conn.autocommit = True
    acquired = False
    try:
        acquired = bool(conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0])
        yield acquired
    finally:
        if acquired:
            conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
        conn.close()

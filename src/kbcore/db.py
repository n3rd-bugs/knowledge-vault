"""kbcore.db — the only module that talks to Postgres (CLAUDE.md hard rule).

psycopg3 connections with explicit transactions. Parameterized SQL only — never
f-string user values into a query.
"""

from __future__ import annotations

import os
import zlib
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from kbcore.config import settings

# Test-only live-data guard. When the test harness exports KB_TEST_GUARD=1, a
# connection that resolves to the LIVE ``public`` schema is REFUSED unless the
# caller explicitly opts in via ``allow_public=True`` (only the conftest fixture
# that creates/drops the throwaway ``kb_test`` schema does, and it only issues
# DDL on kb_test — never touches public data). This is what makes it IMPOSSIBLE
# for a stray test (one that forgot the kb fixture, or hit it while the schema
# pointer was mid-flip) to TRUNCATE live data — the failure mode that truncated
# the live corpus twice. The variable is NEVER set in production, so the public
# fast path below is byte-for-byte unchanged for the app.
_GUARD_ENV = "KB_TEST_GUARD"


def connect(schema: str | None = None, *, allow_public: bool = False) -> psycopg.Connection:
    """Open a new connection. The caller owns its lifecycle.

    Prefer :func:`transaction` for write paths so commit/rollback is automatic.
    ``schema`` overrides ``settings.kb_db_schema`` for THIS connection only,
    without mutating the shared global (the conftest kb_test DDL uses it so it
    never has to flip the process-wide pointer to ``public`` — that transient
    flip was the exact window in which a concurrent TRUNCATE hit live data).
    When the resolved schema is not ``public`` (tests use ``kb_test``), the
    session search_path targets it first, falling back to public for the
    pgvector extension types.

    SAFETY (review finding): Postgres SILENTLY skips a nonexistent schema in
    search_path and resolves unqualified names in the NEXT entry — so a
    ``search_path=kb_test,public`` connection whose ``kb_test`` does not exist
    would fall straight through to LIVE ``public`` data. For a NON-public schema
    we verify it actually resolves and refuse otherwise. Additionally, under the
    test guard (KB_TEST_GUARD), a ``public`` resolution is refused unless
    ``allow_public`` — closing the hole at the connection layer for good.
    """
    resolved = schema if schema is not None else settings.kb_db_schema
    if resolved == "public":
        if os.environ.get(_GUARD_ENV) and not allow_public:
            raise RuntimeError(
                "refusing a 'public' database connection under KB_TEST_GUARD — "
                "tests must operate on kb_test (live-data protection). If this is "
                "genuine kb_test setup DDL, pass allow_public=True."
            )
        return psycopg.connect(settings.db_url)
    conn = psycopg.connect(settings.db_url, options=f"-c search_path={resolved},public")
    # current_schema() returns the FIRST EXISTING schema in the search_path, so it
    # equals the configured schema exactly when that schema exists, and reads
    # 'public' precisely when it is missing — the case we must reject loudly.
    current = conn.execute("SELECT current_schema()").fetchone()[0]
    conn.rollback()  # clear the implicit tx opened by the probe
    if current != resolved:
        conn.close()
        raise RuntimeError(
            f"configured schema {resolved!r} does not resolve "
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
    # SCHEMA-SCOPED. A pg advisory lock is per-DATABASE, not per-schema, so
    # kb_test and public shared key 42: running the suite while a real nightly
    # consolidation was in flight made 28 tests fail with a bare
    # `assert skipped is False` — the tests were being correctly refused a lock
    # production held. Same class as tests writing into the production raw
    # store: shared state between test and prod, discovered only by coincidence.
    # Folding the schema into the key keeps the mutual exclusion exact within a
    # schema while making cross-schema contention impossible.
    from kbcore.config import settings

    scoped = (key << 16) ^ (zlib.crc32(settings.kb_db_schema.encode()) & 0xFFFF)
    conn = connect()
    conn.autocommit = True
    acquired = False
    try:
        acquired = bool(conn.execute("SELECT pg_try_advisory_lock(%s)", (scoped,)).fetchone()[0])
        yield acquired
    finally:
        if acquired:
            conn.execute("SELECT pg_advisory_unlock(%s)", (scoped,))
        conn.close()

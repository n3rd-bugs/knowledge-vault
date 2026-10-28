"""kbcore.db — the only module that talks to Postgres (CLAUDE.md hard rule).

psycopg3 connections with explicit transactions. Parameterized SQL only — never
f-string user values into a query.
"""

from __future__ import annotations

import logging
import os
import time
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import psycopg

from kbcore.config import settings
from kbcore.errors import DatabaseUnavailable

logger = logging.getLogger("kbcore.db")

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


# A REFUSED connection is retried for a bounded window; every other operational
# error (auth, DNS, a bad URL) raises at once. Why: on 2026-09-13 09:03 an
# `apt upgrade` restarted dockerd and with it kb-db for ~10 s, and the running
# nightly died at review_triaged_topup -> eval sentinel -> exit 1 with the whole
# tail (report, digest, weekly rider) never written. Ten seconds of patience is
# cheaper than a night. Budget = settings.kb_db_connect_retry_secs (0 = no retry).
_RETRYABLE_CONNECT = (
    "Connection refused",
    "the database system is starting up",
    "could not connect",
)
_CONNECT_RETRY_SLEEP = 2.0
# fail_fast()'s override of that budget. A ContextVar, not a settings write: it
# covers only the calling context (the worker thread a credential lookup runs on),
# while the nightly and every concurrent caller keep the full budget.
_retry_override: ContextVar[float | None] = ContextVar("kb_db_retry_override", default=None)


def _connect_retrying(url: str, **kw: object) -> psycopg.Connection:
    override = _retry_override.get()
    budget = float(settings.kb_db_connect_retry_secs) if override is None else override
    deadline = time.monotonic() + budget
    warned = False
    while True:
        try:
            return psycopg.connect(url, **kw)  # type: ignore[arg-type]
        except psycopg.OperationalError as e:
            msg = str(e)
            retryable = any(t in msg for t in _RETRYABLE_CONNECT)
            if budget <= 0 or not retryable or time.monotonic() >= deadline:
                raise
            if not warned:
                logger.warning(
                    "database refused the connection; retrying for up to %ss: %s",
                    int(budget),
                    msg.splitlines()[0],
                )
                warned = True
            time.sleep(_CONNECT_RETRY_SLEEP)


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
        return _connect_retrying(settings.db_url)
    conn = _connect_retrying(settings.db_url, options=f"-c search_path={resolved},public")
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
def fail_fast() -> Iterator[None]:
    """Run the block with the connect retry OFF; database failures raise DatabaseUnavailable.

    For request-path lookups that must not wait out ``kb_db_connect_retry_secs``.
    The bearer gate resolves ``kbg_`` tokens and enrolment codes against the DB,
    and while kb-db was down on 2026-10-28 (13:44-14:28 PKT) each such lookup in
    kb-rest retried for 60 s, then escaped as an unhandled 500. Inside this block a
    refused connection fails at once, and any psycopg operational error (refused,
    restarting, dropped mid-query) surfaces as :class:`DatabaseUnavailable`, so the
    caller can answer a clean, retryable 503.
    """
    token = _retry_override.set(0.0)
    try:
        yield
    except psycopg.OperationalError as e:
        raise DatabaseUnavailable(str(e).splitlines()[0] if str(e) else type(e).__name__) from e
    finally:
        _retry_override.reset(token)


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


def _warn_on_foreign_advisory_keys(conn, scoped: int, acquired: bool) -> None:
    """Log our advisory key, and WARN if another connection holds a DIFFERENT one.

    Mutual exclusion is only as good as every live process agreeing on the key.
    They agree right up until the derivation changes: an already-running process
    keeps the old key in resident bytecode, the two become invisible to each
    other, and there is no error and no contention -- just two workers doing the
    same job. That is exactly how two consolidations shared one GPU for 30 hours
    (2026-07-29, legacy key 42 vs scoped 2764317). One catalog read per lock
    attempt is the only cheap signal that would have caught it.
    """
    try:
        others = conn.execute(
            "SELECT objid, pid FROM pg_locks "
            "WHERE locktype = 'advisory' AND granted AND objid <> %s",
            (scoped,),
        ).fetchall()
    except Exception:  # observability must never break the lock path
        logger.debug("advisory key %s (acquired=%s); peer check unavailable", scoped, acquired)
        return
    if others:
        logger.warning(
            "advisory key %s (acquired=%s) but OTHER advisory keys are held: %s -- "
            "a key MISMATCH means mutual exclusion is NOT in effect; suspect a process "
            "started before the key derivation last changed",
            scoped,
            acquired,
            ", ".join(f"objid={o[0]} pid={o[1]}" for o in others),
        )
    else:
        logger.info("advisory key %s (acquired=%s)", scoped, acquired)


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
        _warn_on_foreign_advisory_keys(conn, scoped, acquired)
        yield acquired
    finally:
        if acquired:
            conn.execute("SELECT pg_advisory_unlock(%s)", (scoped,))
        conn.close()

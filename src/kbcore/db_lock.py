"""kbcore.db_lock — the hold :func:`kbcore.db.advisory_lock` yields, kept alive (KB 61595).

Split from :mod:`kbcore.db` for the 300-line ratchet. It opens no connection of its own: the
lock's connection and any replacement come from ``kbcore.db.connect``, handed in.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import psycopg

logger = logging.getLogger("kbcore.db")

# How often a held advisory lock checks that its connection is still alive.
LOCK_KEEPALIVE_SECS = 30.0
# Refused re-takes in a row after which the lock counts as another session's.
LOCK_LOST_AFTER = 2


class LockHold:
    """What :func:`kbcore.db.advisory_lock` yields: truthy iff the lock was taken (the bool
    it replaces), and kept alive while it is held.

    A session lock lasts exactly as long as its connection. On 2026-11-07 an ``apt upgrade``
    restarted docker 9 minutes into a consolidation pass: Postgres ended the lock's connection
    and the lock with it, the pass's own connections reconnected, and it ran on unlocked for
    2 h 51 min, then failed at its final unlock. So a keepalive thread asks the connection
    every :data:`LOCK_KEEPALIVE_SECS`, and once it has gone :meth:`check` takes the lock again
    on a fresh one (``retaken``). If another session took it in the gap, ``lost`` turns True
    and :func:`kbcore.db.check_lock` raises at the holder's next step. Release tolerates a
    dead connection: the lock ended with it.
    """

    def __init__(
        self, scoped: int, conn: psycopg.Connection, reconnect: Callable[[], psycopg.Connection]
    ) -> None:
        self.scoped, self.conn, self.owner = scoped, conn, threading.get_ident()
        self._reconnect = reconnect
        self.acquired = self.held = self.lost = False
        self.retaken = self.refusals = 0
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def __bool__(self) -> bool:
        return self.acquired

    def keep_alive(self) -> None:
        thread = threading.Thread(target=self._run, name="kb-lock-keepalive", daemon=True)
        thread.start()
        self._thread = thread  # only a thread that started is ever joined

    def _run(self) -> None:
        while not self._done.wait(LOCK_KEEPALIVE_SECS):
            self.check()

    def check(self) -> None:
        """One keepalive probe: take the lock again if its connection has gone.

        A failed query on a session that is still up keeps the lock, so it is left alone. A
        refused re-take is tried again at the next probe: a try-and-release probe holds the key
        for milliseconds. Only :data:`LOCK_LOST_AFTER` refusals in a row mean another session
        really holds it.
        """
        if self.lost or not self.acquired:
            return
        if self.held:
            try:
                self.conn.execute("SELECT 1")
                return
            except psycopg.Error:
                if not (self.conn.closed or self.conn.broken):
                    return  # a failed query on a live session: it still holds the lock
            self.held = False  # the connection has gone, and the lock with it
        if self.conn.closed or self.conn.broken:
            fresh = None
            try:
                fresh = self._reconnect()
                fresh.autocommit = True
            except Exception as exc:  # noqa: BLE001 — Postgres not back yet: next probe retries
                if fresh is not None:
                    close_quietly(fresh)
                logger.warning("advisory lock %s: no connection yet (%s)", self.scoped, exc)
                return
            close_quietly(self.conn)
            self.conn = fresh
        try:
            got = self.conn.execute("SELECT pg_try_advisory_lock(%s)", (self.scoped,)).fetchone()[0]
        except psycopg.Error as exc:
            logger.warning("advisory lock %s: re-take failed (%s)", self.scoped, exc)
            return
        if got:
            self.held, self.refusals = True, 0
            self.retaken += 1
            logger.warning("advisory lock %s: its connection dropped; taken again", self.scoped)
            return
        self.refusals += 1
        if self.refusals >= LOCK_LOST_AFTER:
            self.lost = True
            logger.error("advisory lock %s: another session holds it now", self.scoped)
        else:
            logger.warning("advisory lock %s: held elsewhere; trying again", self.scoped)

    def release(self) -> None:
        self._done.set()
        if self._thread is not None:
            self._thread.join()
        try:
            if self.held:
                self.conn.execute("SELECT pg_advisory_unlock(%s)", (self.scoped,))
        except psycopg.Error as exc:
            logger.warning(
                "advisory lock %s ended with its connection before release (%s)", self.scoped, exc
            )
        finally:
            close_quietly(self.conn)


def close_quietly(conn: psycopg.Connection) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001 — closing a connection that already died must not raise
        logger.debug("closing a dead connection failed", exc_info=True)

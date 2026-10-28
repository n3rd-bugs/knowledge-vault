"""kbweb.lookups — the web UI's credential lookups, run OFF the event loop.

kbweb serves on one event loop. Its session gate re-checks a grantee cookie in the
database on every request, /login resolves a kbg_ token, and the Google callback
exchanges the code with Google (sync httpx) and resolves the email — all sync
calls inside async code. Inline, a down database held each such request, and every
other request the loop was serving, for the 60 s connect retry: the kb-rest
failure of 2026-10-28 (see kbmcp.bearer). Each lookup now runs on a worker thread
with the retry off (kbcore.db.fail_fast); DatabaseUnavailable reaches the caller,
which answers :func:`unavailable`.
"""

from __future__ import annotations

import anyio
from anyio.lowlevel import RunVar
from starlette.responses import PlainTextResponse

from kbcore import db

# A few slots of their own, as in kbmcp.principal_ctx: a flood of bogus kbg_ logins
# must not become one Postgres connection per thread of the shared limiter.
_SLOTS = 4
_limiter: RunVar[anyio.CapacityLimiter] = RunVar("kbweb_lookup_limiter")


def _slots() -> anyio.CapacityLimiter:
    try:
        return _limiter.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(_SLOTS)
        _limiter.set(limiter)
        return limiter


def _fail_fast(fn, args):
    with db.fail_fast():
        return fn(*args)


async def off_loop(fn, *args):
    """``fn(*args)`` on a worker thread, with the database connect retry off."""
    return await anyio.to_thread.run_sync(_fail_fast, fn, args, limiter=_slots())


def unavailable() -> PlainTextResponse:
    """The answer while the database cannot be reached: retryable, and identical
    for every path and identity, so it says nothing about either."""
    return PlainTextResponse("Service Unavailable", status_code=503, headers={"Retry-After": "5"})

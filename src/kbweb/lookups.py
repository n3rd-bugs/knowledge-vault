"""kbweb.lookups — the web UI's credential lookups, run OFF the event loop.

kbweb serves on one event loop. Its session gate re-checks a grantee cookie in the
database on every request, /login resolves a kbg_ token, and the Google callback
exchanges the code with Google (sync httpx) and resolves the email — all sync
calls inside async code. Inline, a down database held each such request, and every
other request the loop was serving, for the 60 s connect retry: the kb-rest
failure of 2026-10-28 (see kbmcp.bearer). Each DB lookup now runs on a worker
thread with the retry off (kbcore.db.fail_fast); DatabaseUnavailable reaches the
caller, which answers :func:`unavailable`. Google round-trips run on a worker
thread too, in slots of their OWN: the callback is reachable before any login, so
anyone replaying a signed state cookie could otherwise fill the DB lookups' slots
with slow exchanges and stall every grantee's session check behind them.
"""

from __future__ import annotations

import anyio
from anyio.lowlevel import RunVar
from starlette.responses import PlainTextResponse

from kbcore import db

# A few slots of their own, as in kbmcp.principal_ctx: a flood of bogus kbg_ logins
# must not become one Postgres connection per thread of the shared limiter.
_SLOTS = 4
_db_limiter: RunVar[anyio.CapacityLimiter] = RunVar("kbweb_db_lookup_limiter")
_google_limiter: RunVar[anyio.CapacityLimiter] = RunVar("kbweb_google_call_limiter")


def _slots(var: RunVar) -> anyio.CapacityLimiter:
    try:
        return var.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(_SLOTS)
        var.set(limiter)
        return limiter


def _fail_fast(fn, args):
    with db.fail_fast():
        return fn(*args)


async def off_loop(fn, *args):
    """``fn(*args)`` — a DB lookup — on a worker thread, with the connect retry off."""
    return await anyio.to_thread.run_sync(_fail_fast, fn, args, limiter=_slots(_db_limiter))


async def google_call(fn, *args):
    """``fn(*args)`` — a round-trip to Google — on a worker thread in its own slots."""
    return await anyio.to_thread.run_sync(fn, *args, limiter=_slots(_google_limiter))


def unavailable() -> PlainTextResponse:
    """The answer while the database cannot be reached: retryable, and identical
    for every path and identity, so it says nothing about either."""
    return PlainTextResponse("Service Unavailable", status_code=503, headers={"Retry-After": "5"})

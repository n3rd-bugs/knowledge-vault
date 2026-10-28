"""kbrest.auth_alert — in-process 401-burst alerting (Package O, observability).

The public surface is SILENT on failed auth by design: a bad bearer gets the
byte-identical no-oracle 401 (Invariants 10/12), which is exactly why a box left
on stale hooks and a broken Windows installer could probe the gate for days
without producing any signal. This adds ONE lightweight signal without touching
that contract.

:class:`AuthAlertMiddleware` is an observe-only ASGI wrapper installed OUTSIDE the
bearer middleware. It counts the 401s the bearer layer already emits in a sliding
10-minute window and, when a window crosses ``settings.kb_auth_alert_threshold``
(0 = off), pushes ONE ntfy line via the existing digest channel
(:func:`kbcore.notify.push_digest`). The alert is rate-limited to at most one per
hour so a sustained probe cannot spam it, and it is dispatched on a daemon thread
so it can NEVER block — or fail — the request that triggered it.

No DB, no new table, no request-body or token logging: the counter holds only
event timestamps and client IPs for the current window (bounded), and the
response bytes are forwarded untouched.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from kbcore.config import settings
from kbmcp.bearer import client_ip

log = logging.getLogger("kbrest.auth_alert")

WINDOW_SECONDS = 600.0  # the "10-min bucket": the sliding window 401s are counted in
ALERT_COOLDOWN_SECONDS = 3600.0  # at most one alert per hour, however long a probe runs
# Hard cap on retained events so a genuine flood cannot grow the window without
# bound; far above any sane threshold, and the reported count saturates here.
MAX_WINDOW_EVENTS = 10_000


class AuthAlertCounter:
    """Sliding-window 401 counter with an hourly-rate-limited alert line.

    :meth:`record` is pure apart from its own bounded state, so the alert decision
    is unit-testable without threads, sockets, or a real clock: it returns the
    alert STRING exactly on the request that crosses the threshold (subject to the
    cooldown) and ``None`` on every other request.
    """

    def __init__(self) -> None:
        self._events: deque[tuple[float, str]] = deque()
        # -inf, not 0.0: "never alerted" must permit the first fire regardless of
        # the clock's epoch (a 0.0 seed suppresses any alert while now < cooldown).
        self._last_alert = float("-inf")

    def record(self, client: str, now: float) -> str | None:
        """Register one failed-auth event at time ``now``; return an alert line
        iff this event pushes the 10-min window over the threshold and the hourly
        cooldown has elapsed. ``client`` is the peer IP (``""`` when unknown)."""
        threshold = settings.kb_auth_alert_threshold
        if threshold <= 0:  # kill switch — do not even retain state
            return None
        cutoff = now - WINDOW_SECONDS
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        if len(self._events) < MAX_WINDOW_EVENTS:
            self._events.append((now, client))
        count = len(self._events)
        if count < threshold or now - self._last_alert < ALERT_COOLDOWN_SECONDS:
            return None
        self._last_alert = now
        ips = len({ip for _, ip in self._events if ip})
        return f"AUTH: {count} failed-auth in 10min from {ips} IPs"


def push_alert(line: str) -> None:
    """Best-effort ntfy push of one alert line; swallows every failure.

    Runs on a daemon thread (see :class:`AuthAlertMiddleware`), so a dead tunnel
    or a slow ntfy can never stall or fail the request that triggered it. The
    credential stays inside :mod:`kbcore.notify` — that is where "never log the
    token" lives — and this never logs the request body either.

    Pushed under its own title at high priority (2026-08-23): the first real
    firing rode the default 'KB nightly' title and the owner read it as the
    NIGHTLY reporting an auth failure — a security signal must name itself.
    """
    try:
        from kbcore import notify

        notify.push_digest([line], title="KB ALERT", priority="high")
    except Exception:  # noqa: BLE001 — an alert must never disturb the server
        log.warning("auth-alert push unavailable", exc_info=True)


class AuthAlertMiddleware:
    """Observe-only ASGI wrapper that counts 401s and fires alerts off the path.

    It intercepts the response-start the wrapped app sends, records the ones
    carrying status 401, and — when a window crosses the threshold — dispatches an
    alert on a daemon thread. The response messages are forwarded byte-for-byte,
    so the no-oracle 401 contract is unchanged. Must sit OUTSIDE the bearer
    middleware so its ``send`` wrapper observes the 401s the bearer emits.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.counter = AuthAlertCounter()

    async def __call__(self, asgi_scope, receive, send):
        if asgi_scope["type"] != "http":
            await self.app(asgi_scope, receive, send)
            return

        async def _send(message):
            if message["type"] == "http.response.start" and message.get("status") == 401:
                line = self.counter.record(_client_ip(asgi_scope), time.time())
                if line:
                    # Journald is the DURABLE firing record (review #6): the ntfy
                    # push below is best-effort and its history is lossy. The line
                    # holds only window/IP COUNTS — no token, no body, no client
                    # payload — so logging it leaks nothing the alert would not.
                    log.info("auth alert fired: %s", line)
                    threading.Thread(target=push_alert, args=(line,), daemon=True).start()
            await send(message)

        await self.app(asgi_scope, receive, _send)


# The true client IP for the alert's distinct-IP count — the same function the
# bearer gate's 401 line uses (moved to kbmcp.bearer on 2026-10-28; see there).
_client_ip = client_ip

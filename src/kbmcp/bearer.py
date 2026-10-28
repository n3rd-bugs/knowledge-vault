"""kbmcp.bearer — the pure-ASGI bearer gate shared by kb-mcp and kb-rest (SEC-1).

Every HTTP request needs ``Authorization: Bearer <credential>``; auth is never
disabled, not even "temporarily" (Invariant 6). Moved out of kbmcp.server on
2026-10-28 with two fixes from that day's log review:

* The credential lookup no longer runs ON the event loop. A ``kbg_`` token or an
  enrolment code needs the database, and while kb-db was down (13:44-14:28 PKT)
  each such lookup retried for 60 s inside this async middleware — freezing every
  other request kb-rest held (even 401s for no credential at all queued behind
  it) — then escaped as an unhandled 500. Lookups now go through
  :func:`kbmcp.principal_ctx.aresolve_bearer`, and a database that cannot be
  reached gets a clean 503: never a 401, since a down database is not a bad
  credential (and must not trip the 401-burst alert).
* A rejection names the REAL client. Every tunneled request arrives from the
  cloudflared container, so ``401 unauthorized from ('172.26.0.8', 44934)`` could
  not attribute the 283-request scan of 2026-10-27 04:12 PKT.
"""

from __future__ import annotations

import ipaddress
import logging

from kbcore import scope
from kbcore.errors import DatabaseUnavailable
from kbmcp import principal_ctx

# The logger the 401 line always used, so a `kbmcp WARNING 401` journald filter
# still finds it.
log = logging.getLogger("kbmcp")

_PATH_MAX = 200
_UA_MAX = 80


def client_ip(asgi_scope) -> str:
    """The true client IP: Cloudflare's CF-Connecting-IP when present, else the peer.

    The socket peer is useless behind the tunnel: every tunneled request arrives
    from the cloudflared container's bridge address, so the first real alert
    firing (2026-08-23, a 536-request scan burst) reported '1 IPs' no matter how
    many sources probed. Cloudflare stamps the real client into CF-Connecting-IP
    and OVERWRITES any client-supplied value, and these servers are reachable only
    via the tunnel or localhost (KB_BIND, Invariant 6) — so the header is
    trustworthy when present and the peer is already right when it is not. A value
    that is not a plain IP address was not written by Cloudflare and is ignored —
    including an IPv6 scope id (``::1%<anything>``), which ipaddress accepts
    verbatim. X-Forwarded-For is deliberately NOT consulted: its first hop is
    client-controlled.
    """
    for name, value in asgi_scope.get("headers") or []:
        if name == b"cf-connecting-ip":
            try:
                addr = ipaddress.ip_address(value.decode("latin-1").strip())
            except ValueError:
                break
            if getattr(addr, "scope_id", None) is None:
                return str(addr)
            break
    client = asgi_scope.get("client")
    return client[0] if client else ""


def _printable(text: str, limit: int) -> str:
    # Escape control characters: a percent-decoded path or a user agent must not be
    # able to forge a second journald line.
    return text[:limit].encode("unicode_escape").decode("ascii")


def describe(asgi_scope) -> str:
    """Who asked for what, log-safe: the real client, the socket peer, the method
    and path (never the query string), and the user agent. Never a credential."""
    client = _printable(client_ip(asgi_scope), 64) or "-"
    peer = asgi_scope.get("client")
    peer_text = _printable(f"{peer[0]}:{peer[1]}", 80) if peer else "-"
    ua = ""
    for name, value in asgi_scope.get("headers") or []:
        if name == b"user-agent":
            ua = value.decode("latin-1")
            break
    method = asgi_scope.get("method", "-")
    path = _printable(asgi_scope.get("path", "-"), _PATH_MAX)
    return f"client={client} peer={peer_text} {method} {path} ua={_printable(ua, _UA_MAX) or '-'}"


async def _reply(send, status: int, body: bytes, extra_headers: list) -> None:
    headers = [(b"content-type", b"application/json"), *extra_headers]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class BearerAuthMiddleware:
    """Pure-ASGI bearer check on every request (SEC-1). No streaming buffering.

    Per-project sharing: the master ``KB_TOKEN`` resolves to OWNER (constant-time,
    exactly as before); a live ``kbg_`` grantee token resolves to that grantee; any
    other value is rejected with the SAME 401 (no revoked-vs-unknown oracle). The
    resolved principal is set in ``kbcore.scope`` for the request (REST sync routes
    inherit it via anyio's context copy; MCP tools are also stamped by the FastMCP
    PrincipalMiddleware in their own execution context). The parameter is
    ``asgi_scope`` — NOT ``scope`` — so it never shadows the ``kbcore.scope`` module.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, asgi_scope, receive, send):
        if asgi_scope["type"] == "http":
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in asgi_scope.get("headers", [])
            }
            try:
                principal = await principal_ctx.aresolve_bearer(headers.get("authorization", ""))
            except DatabaseUnavailable:
                # kbcore.db logged the cause; this line says whose request it cost.
                log.warning("503 database unavailable %s", describe(asgi_scope))
                await _reply(
                    send, 503, b'{"error": "service unavailable"}', [(b"retry-after", b"5")]
                )
                return
            if principal is None:
                log.warning("401 unauthorized %s", describe(asgi_scope))
                await _reply(
                    send, 401, b'{"error": "unauthorized"}', [(b"www-authenticate", b"Bearer")]
                )
                return
            token = scope.set_principal(principal)
            try:
                await self.app(asgi_scope, receive, send)
            finally:
                scope.reset_principal(token)
            return
        await self.app(asgi_scope, receive, send)


class RejectLogMiddleware:
    """Observe-only: log every 401 the wrapped app sends, naming the real client.

    For the public OAuth instance, where FastMCP's own auth layer rejects instead
    of BearerAuthMiddleware — so its 401s carry the same attribution as the bearer
    gate's. Response messages are forwarded untouched.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, asgi_scope, receive, send):
        if asgi_scope["type"] != "http":
            await self.app(asgi_scope, receive, send)
            return

        async def _send(message):
            if message["type"] == "http.response.start" and message.get("status") == 401:
                log.warning("401 unauthorized %s", describe(asgi_scope))
            await send(message)

        await self.app(asgi_scope, receive, _send)

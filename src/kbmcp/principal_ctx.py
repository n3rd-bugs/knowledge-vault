"""kbmcp.principal_ctx — resolve a request's credential to a scope.Principal.

Shared by every surface so authorization is one function of the resolved identity
(kbcore.scope), regardless of HOW the request authenticated:

- **Bearer** (local MCP + REST, ``BearerAuthMiddleware``): the master ``KB_TOKEN``
  matches OWNER constant-time; else a ``kbg_`` token resolves to a grantee (SHA-256
  + DB lookup, TTL-cached); else None (→ the same 401 as an unknown token).
- **Google OAuth** (public MCP MultiAuth): an allowlisted owner email
  (``KB_WEB_ALLOWED_EMAILS``) maps to OWNER; else a ``kind='google'`` principal is a
  grantee; else a walled empty-grant identity (sees nothing — "reject" without a
  hard crash).

The FastMCP :class:`PrincipalMiddleware` stamps the resolved principal into
``kbcore.scope`` in the TOOL-EXECUTION context (it runs inside FastMCP's request
handling, where a ContextVar set before ``call_next`` reaches the tool), covering
the ASGI→tool propagation gap the fail-closed guard also backstops.
"""

from __future__ import annotations

import logging
import secrets

import anyio
from anyio.lowlevel import RunVar
from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.middleware import Middleware, MiddlewareContext

from kbcore import db, enrolment, principals, scope
from kbcore.config import settings

log = logging.getLogger("kbmcp.principal")

# Lookups that need the database run on worker threads (aresolve_bearer,
# PrincipalMiddleware). Without slots of their own, a flood of bogus kbg_ bearers
# (misses are never cached) would become one Postgres connection per thread of the
# shared 40-thread limiter — per process, against max_connections 100 — and take
# every thread from the sync routes and tools. One small limiter per event loop.
_LOOKUP_SLOTS = 4
_lookup_limiter: RunVar[anyio.CapacityLimiter] = RunVar("kb_credential_lookup_limiter")


def _lookup_slots() -> anyio.CapacityLimiter:
    try:
        return _lookup_limiter.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(_LOOKUP_SLOTS)
        _lookup_limiter.set(limiter)
        return limiter


# Common claim keys a Google-verified AccessToken may carry the email under.
_EMAIL_CLAIM_KEYS = ("email", "https://www.googleapis.com/auth/userinfo.email")


def _master_matches(token: str) -> bool:
    expected = settings.kb_token
    return bool(expected) and secrets.compare_digest(token.encode(), expected.encode())


ENROLLING_NAME = "enrolling-machine"
# A GRANTEE-issued enrolment code resolves to a DISTINCT sentinel. Any grantee can
# mint a code (kb_setup has no owner gate), but such a code carries only the
# grantee's own authority — it must never satisfy an OWNER-issued-only gate (the
# notify.env ntfy publish token). Both sentinels still reach /api/enrol and
# /api/setup/{script} (a grantee machine must be able to redeem + fetch its hooks);
# only the notify channel narrows to ENROLLING_NAME.
ENROLLING_GRANTEE_NAME = "enrolling-machine-grantee"
ENROLLING_NAMES = frozenset({ENROLLING_NAME, ENROLLING_GRANTEE_NAME})


def _bearer_token(header_or_token: str) -> str:
    if header_or_token[:7].lower() == "bearer ":
        return header_or_token[7:].strip()
    return header_or_token


def resolve_bearer(header_or_token: str | None) -> scope.Principal | None:
    """Map a bearer credential (raw token or ``Bearer <tok>`` header) to a Principal.

    Master → OWNER (constant-time). A live ``kbg_`` grantee token → that grantee.
    Anything else → None, which callers MUST treat exactly like an unknown token
    (identical 401; no revoked-vs-unknown oracle). Sync and DB-backed: async
    callers use :func:`aresolve_bearer`."""
    if not header_or_token:
        return None
    tok = _bearer_token(header_or_token)
    if _master_matches(tok):
        return scope.OWNER
    grantee = principals.resolve_token(tok)
    if grantee is not None:
        return grantee
    # An unredeemed ENROLMENT code authenticates too, as the narrowest identity
    # this system has: no projects, no capabilities. It exists so a brand-new
    # machine can reach the bootstrap endpoints (POST /api/enrol, GET /api/setup/*)
    # to exchange the code for its real credential, WITHOUT the bearer check being
    # disabled for an unauthenticated path (CLAUDE.md rule 6). Every other route
    # sees an identity that can read nothing and write nothing, which is what it is.
    # A PREFIX IS NOT A CREDENTIAL. This used to return a principal on the
    # `kbe_` prefix alone, which satisfied the bearer gate with four known
    # characters — the identity carried no authority, but "authenticates with no
    # secret" is not a state this system may be in (CLAUDE.md rule 6). The code
    # must be a live, unredeemed, unexpired row before it authenticates at all.
    # OWNER-issued and GRANTEE-issued codes resolve to DIFFERENT sentinels: a
    # grantee-minted code (any grantee can mint one) must not launder itself into
    # the owner-only notify channel, so only ENROLLING_NAME clears that gate.
    owner_issued = enrolment.code_is_owner_issued(tok)
    if owner_issued is True:
        return scope.Principal(name=ENROLLING_NAME, is_owner=False)
    if owner_issued is False:
        return scope.Principal(name=ENROLLING_GRANTEE_NAME, is_owner=False)
    return None


def _resolve_bearer_fail_fast(header_or_token: str) -> scope.Principal | None:
    with db.fail_fast():
        return resolve_bearer(header_or_token)


async def aresolve_bearer(header_or_token: str | None) -> scope.Principal | None:
    """:func:`resolve_bearer` for ASYNC callers (the ASGI bearer gate).

    The master token resolves inline — constant-time, no DB — so the owner path
    never depends on database health. Anything else may need the DB (a ``kbg_``
    token, an enrolment code) and runs on a worker thread under
    :func:`kbcore.db.fail_fast`: a sync psycopg call on the event loop stalls
    EVERY request the server is holding, and a down database must cost one
    refused connect, not the 60 s retry budget. Raises
    :class:`~kbcore.errors.DatabaseUnavailable` when the lookup cannot reach the
    database — the caller answers 503, never 401.
    """
    if not header_or_token:
        return None
    inline = _inline_principal(None, header_or_token)
    if inline is not None:
        return inline
    return await anyio.to_thread.run_sync(
        _resolve_bearer_fail_fast, header_or_token, limiter=_lookup_slots()
    )


def _inline_principal(tok, raw: str | None) -> scope.Principal | None:
    """What resolves with no database at all, in _principal_for's precedence: the
    static bearer FastMCP tagged, the master token, a kbg_ grantee still in the TTL
    cache — so a known caller never queues for a lookup slot behind a flood of bogus
    tokens. None means "ask the database"."""
    if getattr(tok, "client_id", None) == "bearer":
        return scope.OWNER
    if not raw:
        return None
    token = _bearer_token(raw)
    if _master_matches(token):
        return scope.OWNER
    return principals.cached_principal(token)


def resolve_google_email(email: str | None) -> scope.Principal:
    """Map a VERIFIED Google email to a Principal.

    Owner allowlist → OWNER (unchanged owner semantics). A ``kind='google'``
    grantee row → that grantee. Otherwise a walled empty-grant identity (sees
    nothing) — the safe "reject the session" outcome without crashing the stream."""
    normalized = (email or "").strip().lower()
    if normalized and normalized in settings.web_allowed_emails:
        return scope.OWNER
    grantee = principals.resolve_email(normalized)
    if grantee is not None:
        return grantee
    # Authenticated but ungranted: no data, no write. (Google's Testing-mode
    # test-user list is the outer gate; this is the inner, data-level wall.)
    return scope.Principal(name=f"google:{normalized or 'unknown'}", is_owner=False)


def _email_from_access_token(tok) -> str | None:
    claims = getattr(tok, "claims", None) or {}
    for key in _EMAIL_CLAIM_KEYS:
        val = claims.get(key)
        if val:
            return str(val)
    subject = getattr(tok, "subject", None)
    if subject and "@" in str(subject):
        return str(subject)
    return None


def _request_inputs() -> tuple[object | None, str | None]:
    """The two request facts a principal is resolved from: FastMCP's verified
    access token (public MultiAuth instance) and the raw Authorization header.

    The header is read from the request itself, and it is the FIRST Authorization
    header — the one the auth layers check (Starlette's Headers.get, and
    kbmcp.bearer). FastMCP's get_http_headers() would not do: it drops
    ``authorization`` by default, so the header always read as None and on the local
    instance a tool ran under whatever principal the MCP session's task inherited —
    the one that CREATED the session (2026-10-28 review); and asked for by name it
    builds a dict, where the LAST duplicate header wins.
    """
    try:
        tok = get_access_token()
    except Exception:  # outside a request context
        tok = None
    try:
        raw = get_http_request().headers.get("authorization")
    except Exception:  # no HTTP request (e.g. the in-memory transport)
        raw = None
    return tok, raw


def _principal_for(tok, raw: str | None) -> scope.Principal | None:
    if tok is not None:
        # Public MultiAuth path. The StaticTokenVerifier tags the master 'bearer'.
        if getattr(tok, "client_id", None) == "bearer":
            return scope.OWNER
        # A grantee kbg_ token could also ride the raw header on this instance.
        via_bearer = resolve_bearer(raw)
        if via_bearer is not None:
            return via_bearer
        # Otherwise it is a Google identity — map its verified email.
        return resolve_google_email(_email_from_access_token(tok))
    # Local instance (FastMCP auth off): the ASGI BearerAuthMiddleware already
    # validated the header; resolve it here in the tool-execution context.
    return resolve_bearer(raw)


def resolve_current_principal() -> scope.Principal | None:
    """Resolve the principal for the CURRENT FastMCP request, or None if it cannot
    be determined (caller decides whether that is fail-closed)."""
    return _principal_for(*_request_inputs())


def _principal_fail_fast(tok, raw: str | None) -> scope.Principal | None:
    with db.fail_fast():
        return _principal_for(tok, raw)


class PrincipalMiddleware(Middleware):
    """Stamp the resolved principal into kbcore.scope for each MCP message."""

    async def on_message(self, context: MiddlewareContext, call_next):
        # The request context is read HERE, on the event loop; the resolution —
        # a DB read for a grantee token or a Google email (resolve_email is not
        # cached) — runs on a worker thread with the connect retry off, for the
        # same reason as aresolve_bearer. What needs no DB resolves inline.
        tok, raw = _request_inputs()
        principal = _inline_principal(tok, raw) or await anyio.to_thread.run_sync(
            _principal_fail_fast, tok, raw, limiter=_lookup_slots()
        )
        if principal is None and raw:
            # A credential the auth layer accepted no longer resolves (revoked or
            # redeemed in between). Never fall through to the principal the MCP
            # session's task inherited from whoever created it: a walled identity.
            principal = scope.Principal(name="unresolved-credential", is_owner=False)
        if principal is None:
            # No HTTP request at all (in-memory transport): nothing to stamp; in
            # server mode scope.current() fails closed.
            return await call_next(context)
        with scope.use_principal(principal):
            return await call_next(context)

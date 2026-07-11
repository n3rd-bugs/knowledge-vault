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

from fastmcp.server.dependencies import get_access_token, get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext

from kbcore import principals, scope
from kbcore.config import settings

log = logging.getLogger("kbmcp.principal")

# Common claim keys a Google-verified AccessToken may carry the email under.
_EMAIL_CLAIM_KEYS = ("email", "https://www.googleapis.com/auth/userinfo.email")


def _master_matches(token: str) -> bool:
    expected = settings.kb_token
    return bool(expected) and secrets.compare_digest(token.encode(), expected.encode())


def resolve_bearer(header_or_token: str | None) -> scope.Principal | None:
    """Map a bearer credential (raw token or ``Bearer <tok>`` header) to a Principal.

    Master → OWNER (constant-time). A live ``kbg_`` grantee token → that grantee.
    Anything else → None, which callers MUST treat exactly like an unknown token
    (identical 401; no revoked-vs-unknown oracle)."""
    if not header_or_token:
        return None
    tok = header_or_token
    if tok[:7].lower() == "bearer ":
        tok = tok[7:].strip()
    if _master_matches(tok):
        return scope.OWNER
    return principals.resolve_token(tok)


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


def resolve_current_principal() -> scope.Principal | None:
    """Resolve the principal for the CURRENT FastMCP request, or None if it cannot
    be determined (caller decides whether that is fail-closed)."""
    try:
        tok = get_access_token()
    except Exception:  # outside a request context
        tok = None
    if tok is not None:
        # Public MultiAuth path. The StaticTokenVerifier tags the master 'bearer'.
        if getattr(tok, "client_id", None) == "bearer":
            return scope.OWNER
        # A grantee kbg_ token could also ride the raw header on this instance.
        raw = get_http_headers().get("authorization")
        via_bearer = resolve_bearer(raw)
        if via_bearer is not None:
            return via_bearer
        # Otherwise it is a Google identity — map its verified email.
        return resolve_google_email(_email_from_access_token(tok))
    # Local instance (FastMCP auth off): the ASGI BearerAuthMiddleware already
    # validated the header; resolve it here in the tool-execution context.
    return resolve_bearer(get_http_headers().get("authorization"))


class PrincipalMiddleware(Middleware):
    """Stamp the resolved principal into kbcore.scope for each MCP message."""

    async def on_message(self, context: MiddlewareContext, call_next):
        principal = resolve_current_principal()
        if principal is None:
            # Unresolvable on a served request → fail closed via scope.current().
            # (Should not happen: auth gates upstream.) Leave the ContextVar unset.
            return await call_next(context)
        with scope.use_principal(principal):
            return await call_next(context)

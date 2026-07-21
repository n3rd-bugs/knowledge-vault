"""kbweb.auth — signed session cookies for the web UI (Phase 6b; Inv. 11 amended).

Login checks the supplied token against ``settings.kb_token`` (constant-time)
and, on success, issues an itsdangerous-signed cookie. Every signer (session,
CSRF, OAuth state) draws its secret from :func:`_signing_secret` —
``KB_WEB_SIGNING_SECRET`` when set, else ``KB_TOKEN`` — so cookie forgery and
API-master can be decoupled without breaking existing deployments. Anonymous
access is refused everywhere except /login and /static. Also home to the admin
CSRF token (the owner-only authz carve-out's POST guard).
"""

from __future__ import annotations

import logging
import secrets
from urllib.parse import urlencode

import httpx
from itsdangerous import BadData, URLSafeTimedSerializer

from kbcore.config import settings

log = logging.getLogger("kbweb.auth")

# __Host- prefix (A11-3 finding 23 / deliverable 6): the browser itself then
# ENFORCES Secure + Path=/ + no Domain on the write-authorizing session cookie, so
# it can never be set over http nor scoped to a sibling host. The set_cookie/
# delete_cookie call sites in kbweb.server pass secure=True to match; the old
# unprefixed names fail to parse and force one clean re-login (no downgrade).
COOKIE = "__Host-kb_session"
_SALT = "kbweb-session"
MAX_AGE = 60 * 60 * 12  # 12 hours

# --- Google OAuth (additional sign-in path, gated by an email allowlist) ------
STATE_COOKIE = "__Host-kb_oauth_state"
_STATE_SALT = "kbweb-oauth-state"
STATE_MAX_AGE = 600  # 10 minutes — the login→callback round trip is short
_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)  # short: no long-poll here

_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def _signing_secret() -> str:
    """The one secret behind EVERY web signer (session, CSRF, OAuth state).

    ``KB_WEB_SIGNING_SECRET`` when set — decoupling cookie/CSRF forgery from
    API-master, so a compromised web signing key can never authenticate against
    the MCP/REST bearer check (Phase 1 hardening) — falling back to
    ``KB_TOKEN`` when empty so existing deployments keep working until the
    owner sets the dedicated secret (one forced re-login at rollout).
    """
    return settings.kb_web_signing_secret or settings.kb_token


def _serializer() -> URLSafeTimedSerializer:
    # Built per call so a rotated secret (or a test override) takes effect.
    return URLSafeTimedSerializer(_signing_secret(), salt=_SALT)


# Cookie payload = the principal IDENTITY: "owner" for the owner (master token or
# an allowlisted email), or "g:<name>" for a grantee. The old literal "ok" cookies
# fail to parse as an identity and force one clean re-login (no security downgrade).
OWNER_IDENTITY = "owner"


def grantee_identity(name: str) -> str:
    return f"g:{name}"


def issue_cookie(identity: str = OWNER_IDENTITY) -> str:
    """A fresh signed session value carrying the principal identity."""
    return _serializer().dumps(identity)


def cookie_identity(value: str | None) -> str | None:
    """The signed principal identity ("owner" / "g:<name>"), or None if the cookie
    is absent, malformed, or expired."""
    if not value:
        return None
    try:
        identity = _serializer().loads(value, max_age=MAX_AGE)
    except BadData:
        return None
    return identity if isinstance(identity, str) and identity else None


def cookie_valid(value: str | None) -> bool:
    """True if the cookie is present, correctly signed, and unexpired."""
    return cookie_identity(value) is not None


def token_ok(supplied: str | None) -> bool:
    """Constant-time check of a login token against KB_TOKEN."""
    return bool(supplied) and secrets.compare_digest(supplied, settings.kb_token)


def _redirect_uri() -> str:
    """The redirect_uri Google calls back on; must match login and token calls."""
    return settings.kb_web_public_url.rstrip("/") + "/auth/google/callback"


def google_login_url(state: str) -> str:
    """Build the Google authorize URL to redirect the browser to (openid email)."""
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_google_code(code: str) -> str | None:
    """Swap an auth code for the caller's verified email, or None on any failure.

    Two hops: POST the code to Google's token endpoint, then GET userinfo with
    the returned access token. Every error path (network, HTTP status, malformed
    JSON, unverified/absent email) collapses to None — never raises to the caller.
    """
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            token_resp = client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": _redirect_uri(),
                },
            )
            token_resp.raise_for_status()
            access_token = token_resp.json().get("access_token")
            if not access_token:
                return None
            info_resp = client.get(
                _USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
            info_resp.raise_for_status()
            info = info_resp.json()
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.warning("google code exchange failed: %s: %s", e.__class__.__name__, e)
        return None
    email = info.get("email")
    verified = info.get("email_verified", True)  # absent ⇒ trust; present ⇒ must be truthy
    if isinstance(verified, str):
        verified = verified.strip().lower() not in ("", "false", "0")
    if not email or not verified:
        return None
    return str(email).lower()


def email_allowed(email: str) -> bool:
    """True iff email is on the (non-empty) allowlist. Empty list ⇒ deny all."""
    allowed = settings.web_allowed_emails
    return bool(allowed) and email.lower() in allowed


# --- Admin CSRF (the ONLY state-changing web forms — Inv. 11 carve-out) -------
#
# The session cookie is SameSite=lax, which already blocks the cookie on
# cross-site POSTs; this signed token is the primary explicit gate (and covers
# legacy/lax-bypass corners), bound to the OWNER identity so a token minted for
# one identity can never authorize another. Same-origin checking is layered on
# top in views_admin (defense-in-depth).
_CSRF_SALT = "kbweb-admin-csrf"
CSRF_MAX_AGE = 60 * 60 * 2  # 2 h — re-minted on every /admin render


def _csrf_serializer() -> URLSafeTimedSerializer:
    # Distinct salt: an admin CSRF token must never validate as a session cookie
    # or OAuth state (and vice versa). Same secret source as every web signer.
    return URLSafeTimedSerializer(_signing_secret(), salt=_CSRF_SALT)


def issue_csrf(identity: str) -> str:
    """A signed anti-CSRF token bound to the signed-in identity.

    Minted into the admin page; required on every mutating admin POST."""
    return _csrf_serializer().dumps({"n": secrets.token_urlsafe(8), "id": identity})


def csrf_ok(signed: str | None, identity: str) -> bool:
    """True iff the token is present, correctly signed, unexpired, and bound to
    ``identity``. Missing/malformed/expired/foreign-identity all collapse to False."""
    if not signed:
        return False
    try:
        payload = _csrf_serializer().loads(signed, max_age=CSRF_MAX_AGE)
    except BadData:
        return False
    if not isinstance(payload, dict):
        return False
    return secrets.compare_digest(str(payload.get("id") or ""), identity)


def _state_serializer() -> URLSafeTimedSerializer:
    # Distinct salt from the session cookie so the two token classes never mix.
    return URLSafeTimedSerializer(_signing_secret(), salt=_STATE_SALT)


def issue_state(state: str) -> str:
    """Sign a short-lived OAuth ``state`` value for the state cookie (CSRF guard)."""
    return _state_serializer().dumps(state)


def state_ok(signed: str | None, expected: str) -> bool:
    """True if the signed cookie is valid, unexpired, and matches the callback state."""
    if not signed or not expected:
        return False
    try:
        original = _state_serializer().loads(signed, max_age=STATE_MAX_AGE)
    except BadData:
        return False
    return secrets.compare_digest(str(original), expected)

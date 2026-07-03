"""kbweb.auth — signed session cookies for the read-only web UI (Phase 6b).

Login checks the supplied token against ``settings.kb_token`` (constant-time)
and, on success, issues an itsdangerous-signed cookie whose secret IS the token.
Anonymous access is refused everywhere except /login and /static.
"""

from __future__ import annotations

import logging
import secrets
from urllib.parse import urlencode

import httpx
from itsdangerous import BadData, URLSafeTimedSerializer

from kbcore.config import settings

log = logging.getLogger("kbweb.auth")

COOKIE = "kb_session"
_SALT = "kbweb-session"
MAX_AGE = 60 * 60 * 12  # 12 hours

# --- Google OAuth (additional sign-in path, gated by an email allowlist) ------
STATE_COOKIE = "kb_oauth_state"
_STATE_SALT = "kbweb-oauth-state"
STATE_MAX_AGE = 600  # 10 minutes — the login→callback round trip is short
_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)  # short: no long-poll here

_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def _serializer() -> URLSafeTimedSerializer:
    # Built per call so a rotated KB_TOKEN (or a test override) takes effect.
    return URLSafeTimedSerializer(settings.kb_token, salt=_SALT)


def issue_cookie() -> str:
    """A fresh signed session value to set in the ``kb_session`` cookie."""
    return _serializer().dumps("ok")


def cookie_valid(value: str | None) -> bool:
    """True if the cookie is present, correctly signed, and unexpired."""
    if not value:
        return False
    try:
        _serializer().loads(value, max_age=MAX_AGE)
        return True
    except BadData:
        return False


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


def _state_serializer() -> URLSafeTimedSerializer:
    # Distinct salt from the session cookie so the two token classes never mix.
    return URLSafeTimedSerializer(settings.kb_token, salt=_STATE_SALT)


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

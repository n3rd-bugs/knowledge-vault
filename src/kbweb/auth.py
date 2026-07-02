"""kbweb.auth — signed session cookies for the read-only web UI (Phase 6b).

Login checks the supplied token against ``settings.kb_token`` (constant-time)
and, on success, issues an itsdangerous-signed cookie whose secret IS the token.
Anonymous access is refused everywhere except /login and /static.
"""

from __future__ import annotations

import secrets

from itsdangerous import BadData, URLSafeTimedSerializer

from kbcore.config import settings

COOKIE = "kb_session"
_SALT = "kbweb-session"
MAX_AGE = 60 * 60 * 12  # 12 hours


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

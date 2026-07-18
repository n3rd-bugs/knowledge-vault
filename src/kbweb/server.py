"""kbweb.server — web UI: app assembly + auth (PLAN.md §10, Inv. 11 amended).

Views live in kbweb.views_search / views_items / views_meta (all GET); this
module owns the session middleware, login (token + Google OAuth), error
handlers and startup guards. NO KNOWLEDGE MUTATION by construction
(Invariant 11, amended 2026-07-18): every data route is GET/HEAD and no kbweb
module imports a kbcore knowledge-write function — a test asserts both
mechanically. The sole mutating routes are the OWNER-ONLY authz-config actions
in kbweb.views_admin (project-share grant/revoke); the session middleware
walls the whole /admin* subtree off from non-owners with the byte-identical
unknown-path 404. Run: ``python -m kbweb.server``.
"""

from __future__ import annotations

import logging
import secrets

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from kbcore import principals, scope
from kbcore.config import settings
from kbcore.errors import KBError, NotFound
from kbmcp.server import _validate_runtime_config
from kbweb import auth, views_admin, views_items, views_meta, views_search
from kbweb.ui import STATIC_DIR, code_css, templates

log = logging.getLogger("kbweb")


def _principal_for_identity(identity: str | None) -> scope.Principal | None:
    """Re-resolve the cookie's identity to a LIVE principal on every request.

    "owner" → the unrestricted owner; "g:<name>" → the grantee if still live
    (re-checked in the DB so a revoked grantee's valid cookie dies immediately).
    None ⇒ the request must bounce to /login."""
    if identity == auth.OWNER_IDENTITY:
        return scope.OWNER
    if identity and identity.startswith("g:"):
        return principals.resolve_name(identity[2:])
    return None


class SessionAuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /login (except /login, /static,
    /auth/google) and bind the resolved principal for the request (grantees see
    only their granted projects in every view). Walls the owner-only /admin*
    subtree off from non-owners with the router's byte-identical 404."""

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path == "/login" or path.startswith("/static") or path.startswith("/auth/google"):
            return await call_next(request)
        identity = auth.cookie_identity(request.cookies.get(auth.COOKIE))
        principal = _principal_for_identity(identity)
        if principal is None:
            return RedirectResponse("/login", status_code=302)
        if (path == "/admin" or path.startswith("/admin/")) and not principal.is_owner:
            # The owner-only authz surface (Inv. 11 carve-out) must be
            # INDISTINGUISHABLE from a nonexistent path for any grantee:
            # answered BEFORE routing so no method probe can elicit a 405,
            # byte-identical to the router's own unknown-path 404.
            return PlainTextResponse("Not Found", status_code=404)
        with scope.use_principal(principal):
            return await call_next(request)


# --- auth views ---------------------------------------------------------------


async def login(request):  # GET shows form, POST checks token
    error = None
    if request.method == "POST":
        form = await request.form()
        supplied = str(form.get("token") or "")
        if auth.token_ok(supplied):
            return _issue_session("/", auth.OWNER_IDENTITY)  # master → owner
        grantee = principals.resolve_token(supplied)  # a kbg_ token → grantee
        if grantee is not None:
            return _issue_session("/", auth.grantee_identity(grantee.name))
        error = "Invalid token."
    status = 401 if error else 200
    return templates.TemplateResponse(request, "login.html", {"error": error}, status_code=status)


def _issue_session(location: str, identity: str) -> RedirectResponse:
    """A redirect that also sets the signed session cookie carrying the principal
    identity (shared by both logins)."""
    resp = RedirectResponse(location, status_code=303)
    resp.set_cookie(
        auth.COOKIE,
        auth.issue_cookie(identity),
        httponly=True,
        samesite="lax",
        max_age=auth.MAX_AGE,
    )
    return resp


async def google_login(request):  # GET /auth/google/login
    # Random per-request state, signed into a short-lived cookie; the same value
    # travels to Google in the URL and comes back in the callback (CSRF guard).
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(auth.google_login_url(state), status_code=302)
    resp.set_cookie(
        auth.STATE_COOKIE,
        auth.issue_state(state),
        httponly=True,
        samesite="lax",
        max_age=auth.STATE_MAX_AGE,
    )
    return resp


async def google_callback(request):  # GET /auth/google/callback
    state = request.query_params.get("state") or ""
    code = request.query_params.get("code") or ""
    if not code or not auth.state_ok(request.cookies.get(auth.STATE_COOKIE), state):
        log.warning("google callback rejected: missing code or state mismatch")
    else:
        email = auth.exchange_google_code(code)
        identity = None
        if email and auth.email_allowed(email):
            identity = auth.OWNER_IDENTITY  # allowlisted owner email
        elif email:
            grantee = principals.resolve_email(email)  # a kind='google' grantee
            if grantee is not None:
                identity = auth.grantee_identity(grantee.name)
        if identity is not None:
            resp = _issue_session("/", identity)
            resp.delete_cookie(auth.STATE_COOKIE)
            return resp
        log.warning("google callback rejected: email not allowed or exchange failed: %r", email)
    # Never surface the email/token to the client — a generic message only.
    return templates.TemplateResponse(
        request, "login.html", {"error": "Google sign-in was not accepted."}, status_code=403
    )


# --- error handlers -----------------------------------------------------------


def _not_found(request, exc) -> Response:
    return HTMLResponse("<h1>404 — not found</h1><p><a href='/'>search</a></p>", status_code=404)


def _bad_request(request, exc) -> Response:
    return HTMLResponse(f"<h1>400 — {exc}</h1><p><a href='/'>search</a></p>", status_code=400)


def code_css_view(request) -> Response:  # GET /static/code.css
    """Pygments token styles, generated from the installed lib (see ui.code_css)."""
    return Response(code_css(), media_type="text/css", headers={"Cache-Control": "max-age=86400"})


ROUTES = [
    Route("/login", login, methods=["GET", "POST"]),
    Route("/auth/google/login", google_login),
    Route("/auth/google/callback", google_callback),
    # Declared before the Mount so it wins over the on-disk static lookup.
    Route("/static/code.css", code_css_view),
    Mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static"),
    *views_search.ROUTES,
    *views_items.ROUTES,
    *views_meta.ROUTES,
    # Owner-only authz-config carve-out (Inv. 11 amended) — the ONLY mutating routes.
    *views_admin.ROUTES,
]


def build_app() -> Starlette:
    _validate_runtime_config()
    # Google sign-in builds its redirect_uri from KB_WEB_PUBLIC_URL (auth._redirect_uri);
    # an empty base yields a relative "/auth/google/callback" that Google rejects.
    # Refuse to serve the OAuth path misconfigured (no hardcoded host, CLAUDE.md).
    if settings.google_client_id and not settings.kb_web_public_url.strip():
        raise SystemExit(
            "refusing to start: Google sign-in is configured (GOOGLE_CLIENT_ID set) "
            "but KB_WEB_PUBLIC_URL is empty — set the public web base URL "
            "(e.g. https://web.example.com) in .env"
        )
    return Starlette(
        routes=ROUTES,
        middleware=[Middleware(SessionAuthMiddleware)],
        exception_handlers={NotFound: _not_found, KBError: _bad_request},
    )


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # Fail-closed on the served process (see kbmcp.server.main): a request without a
    # resolved principal RAISES rather than defaulting to OWNER. SessionAuthMiddleware
    # binds it per request.
    scope.enter_server_mode()
    log.info("kb-web starting on %s:%s", settings.kb_bind, settings.kb_web_port)
    uvicorn.run(build_app(), host=settings.kb_bind, port=settings.kb_web_port, log_level="info")


if __name__ == "__main__":
    main()

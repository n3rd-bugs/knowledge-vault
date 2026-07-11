"""kbweb.server — read-only web UI: app assembly + auth (PLAN.md §10, Inv. 11).

Views live in kbweb.views_search / views_items / views_meta (all GET); this
module owns the session middleware, login (token + Google OAuth), error
handlers and startup guards. READ-ONLY by construction (Invariant 11): every
data route is GET/HEAD and no kbweb module imports a kbcore write function —
a test asserts both mechanically. Run: ``python -m kbweb.server``.
"""

from __future__ import annotations

import logging
import secrets

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from kbcore.config import settings
from kbcore.errors import KBError, NotFound
from kbmcp.server import _validate_runtime_config
from kbweb import auth, views_items, views_meta, views_search
from kbweb.ui import STATIC_DIR, code_css, templates

log = logging.getLogger("kbweb")


class SessionAuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /login (except /login, /static, /auth/google)."""

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path == "/login" or path.startswith("/static") or path.startswith("/auth/google"):
            return await call_next(request)
        if not auth.cookie_valid(request.cookies.get(auth.COOKIE)):
            return RedirectResponse("/login", status_code=302)
        return await call_next(request)


# --- auth views ---------------------------------------------------------------


async def login(request):  # GET shows form, POST checks token
    error = None
    if request.method == "POST":
        form = await request.form()
        if auth.token_ok(str(form.get("token") or "")):
            return _issue_session("/")
        error = "Invalid token."
    status = 401 if error else 200
    return templates.TemplateResponse(request, "login.html", {"error": error}, status_code=status)


def _issue_session(location: str) -> RedirectResponse:
    """A redirect that also sets the signed session cookie (shared by both logins)."""
    resp = RedirectResponse(location, status_code=303)
    resp.set_cookie(
        auth.COOKIE, auth.issue_cookie(), httponly=True, samesite="lax", max_age=auth.MAX_AGE
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
        if email and auth.email_allowed(email):
            resp = _issue_session("/")
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
    log.info("kb-web starting on %s:%s", settings.kb_bind, settings.kb_web_port)
    uvicorn.run(build_app(), host=settings.kb_bind, port=settings.kb_web_port, log_level="info")


if __name__ == "__main__":
    main()

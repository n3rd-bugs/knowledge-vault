"""kbweb.server — web UI: app assembly + auth (PLAN.md §10, Inv. 11 amended A11-3).

Views live in kbweb.views_search / views_items / views_meta (all GET); this
module owns the session middleware, login (token + Google OAuth), error
handlers and startup guards. Knowledge mutation is CONFINED (Invariant 11,
amended 2026-07-18/19, and A11-3 2026-07-21): the ONLY knowledge-write module is
kbweb.views_write, which imports EXACTLY the composed kbcore chokepoints
{web_write.save_for_review, web_write.supersede_for_review, graph.link,
review.reject, review.dismiss} — every trust/quarantine/authz guarantee already
lives in kbcore (Invariant 2). Web writes are capability-gated, project-scoped,
forced ``trust='unverified'`` and quarantined from retrieval until an OWNER
promotes; trust escalation (promote) stays owner-only and is never imported here.
The owner-only authz-config actions in kbweb.views_admin (project-share
grant/revoke + google-grantee creation, A11-2) remain authz, never knowledge.

Tiered access (WEBUI_TIERED_ACCESS_PLAN §2): the session middleware consults
the declarative :data:`ROUTE_CAPS` zone table BEFORE routing and answers an
under-capped principal with the router's byte-identical unknown-path 404 — a
feature zone a principal lacks is indistinguishable from a path that never
existed (the /admin wall generalized; /review and /setup moved here from their
old explanatory 400). A startup assertion enforces default-deny: every
registered route must carry a zone entry or be explicitly public.
Run: ``python -m kbweb.server``.
"""

from __future__ import annotations

import logging

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from kbcore import offsite_config, principals, scope
from kbcore.config import settings
from kbcore.errors import InvalidOperation, KBError, NotFound
from kbmcp.server import _validate_runtime_config
from kbweb import (
    auth,
    views_admin,
    views_backup,
    views_export,
    views_items,
    views_jobs,
    views_meta,
    views_search,
    views_write,
)
from kbweb.ui import STATIC_DIR, code_css, templates

log = logging.getLogger("kbweb")

# --- feature-zone wall (tiered access, WEBUI_TIERED_ACCESS_PLAN §2) -----------
#
# Leading path segment → capability required to ENTER the zone, consulted in
# SessionAuthMiddleware.dispatch before routing. OWNER_ONLY is a sentinel no
# ROLE_CAPS bundle ever contains — and 'owner' is not a storable role
# (migration 012's CHECK) — so only ``is_owner`` passes ``has_cap`` for those
# zones. The nav in templates/base.html renders from THIS table too (via
# kbweb.ui._identity_ctx), so a link can never outrun its wall.
OWNER_ONLY = "owner"

ROUTE_CAPS: dict[str, str] = {
    "/": scope.CAP_SEARCH,
    "/tags": scope.CAP_SEARCH,
    "/item": scope.CAP_SEARCH,
    "/timeline": scope.CAP_BROWSE,
    "/projects": scope.CAP_BROWSE,
    "/project": scope.CAP_BROWSE,
    "/stats": scope.CAP_BROWSE,
    "/trace": scope.CAP_BROWSE,
    "/similar": scope.CAP_BROWSE,
    # A11-3 confined web-write surface (amended Invariant 11): each mutating route
    # is its OWN top-level zone so the Layer-1 byte-404 wall gates it at its WRITE
    # capability BEFORE routing (finding 22) — never nested under /item or /review,
    # which would gate a write below its real cap (a 400-vs-404 feature oracle).
    "/save": scope.CAP_SAVE,  # POST contributor save (+ GET the Contribute form)
    "/supersede": scope.CAP_SAVE,  # POST /supersede/{id} — quarantined proposal
    "/link": scope.CAP_SAVE,  # POST /link/{id} — create a relation edge
    "/resolve": scope.CAP_REVIEW_RESOLVE,  # POST /resolve/{qid} — reject/dismiss only
    # /review is now a reviewer zone: the review_queue.detail scrub landed in
    # kbcore.review (A11-3 deliverable 4), so a non-owner reviewer may LIST safely.
    "/review": scope.CAP_REVIEW_READ,
    "/setup": OWNER_ONLY,  # renders the MASTER bearer — permanently owner-only
    "/admin": OWNER_ONLY,  # the Inv. 11 authz carve-out (same wall as before)
    # Owner-only web backup-setup surface (owner decision 2026-07-21): an owner
    # override of the read-only-web posture — writes HOST CONFIG (offsite backups),
    # never knowledge. Same owner-only byte-404 wall as /admin.
    "/backups": OWNER_ONLY,
    # Phase 3 (plan §5): worker jobs run under implicit OWNER scope and their
    # result JSONB carries whole-KB synthesis — owner-cap at launch.
    "/jobs": OWNER_ONLY,
    # Phase 3 (plan §3d/§6 Q4): export is in NO role bundle — owner + future
    # named grants only. Both format paths are their own zone key (the zone is
    # the leading path segment, and '.' is part of it).
    "/export.csv": scope.CAP_EXPORT,
    "/export.json": scope.CAP_EXPORT,
}

# Zones reachable WITHOUT a ROUTE_CAPS entry: the pre-auth surfaces (/login,
# /static, /auth/google) and /welcome (any authenticated principal's own
# getting-started page — it renders only that principal's caps). Everything
# else MUST appear in ROUTE_CAPS: the startup assertion enforces default-deny.
PUBLIC_ZONES = frozenset({"/login", "/static", "/auth", "/welcome"})


def _zone(path: str) -> str:
    """The leading path segment ('/', '/tags', …) — the ROUTE_CAPS key."""
    return path if path == "/" else "/" + path.split("/", 2)[1]


def assert_route_caps_total(routes) -> None:
    """Default-deny (plan §2): EVERY registered route needs a conscious tier
    decision — a ROUTE_CAPS zone entry or membership in the explicit public set.
    A new route without one refuses to serve instead of silently going open."""
    missing = sorted(
        {
            route.path
            for route in routes
            if _zone(route.path) not in ROUTE_CAPS and _zone(route.path) not in PUBLIC_ZONES
        }
    )
    if missing:
        raise SystemExit(f"routes without a ROUTE_CAPS entry (default-deny): {missing}")


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


# HSTS (A11-3 finding 23 / deliverable 6): Secure cookies stop cleartext REPLAY,
# but only HSTS stops the browser's initial plaintext emission (and MITM phishing
# of the login token) after the first visit — set on EVERY response, walls and
# data pages alike, so it is never a distinguishing header (the ghost harness
# compares body/status/content-type, not this).
_HSTS = "max-age=31536000"


class SessionAuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated requests to /login (except /login, /static,
    /auth/google), wall capability zones (ROUTE_CAPS), and bind the resolved
    principal for the request (grantees see only their granted projects in
    every view; chrome reads the LIVE principal off request.state).

    Also stashes the EXACT cookie identity string ('owner' / 'g:<name>') on
    ``request.state.identity`` (A11-3 findings 13/19/25): the confined web-write
    routes bind their CSRF token to THAT live value — never reconstructed, never
    the OWNER_IDENTITY constant — so grantee A's token can authorize neither
    grantee B nor the owner. Adds the Strict-Transport-Security header to every
    response."""

    async def dispatch(self, request, call_next):
        response = await self._dispatch(request, call_next)
        response.headers.setdefault("Strict-Transport-Security", _HSTS)
        return response

    async def _dispatch(self, request, call_next):
        path = request.url.path
        if path == "/login" or path.startswith("/static") or path.startswith("/auth/google"):
            return await call_next(request)
        identity = auth.cookie_identity(request.cookies.get(auth.COOKIE))
        principal = _principal_for_identity(identity)
        if principal is None:
            return RedirectResponse("/login", status_code=302)
        required = ROUTE_CAPS.get(_zone(path))
        if required is not None and not principal.has_cap(required):
            # A walled feature zone must be INDISTINGUISHABLE from a nonexistent
            # path for an under-capped principal: answered BEFORE routing so no
            # method probe can elicit a 405, byte-identical to the router's own
            # unknown-path 404 — the no-oracle discipline extended from data
            # existence to FEATURE existence (plan §2). Unknown zones fall
            # through to the router's genuine 404 (same bytes).
            return PlainTextResponse("Not Found", status_code=404)
        # The LIVE principal + its cookie identity ride request.state: nav/welcome
        # render from resolved caps (kbweb.ui._identity_ctx) and the write routes
        # bind CSRF to `identity` (the exact signed cookie value, never rebuilt).
        request.state.principal = principal
        request.state.identity = identity
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
        secure=True,  # __Host- prefix requires Secure + Path=/ + no Domain (finding 23)
        samesite="lax",
        max_age=auth.MAX_AGE,
        path="/",
    )
    return resp


async def google_login(request):  # GET /auth/google/login
    # Random per-request state, signed into a short-lived cookie; the same value
    # travels to Google in the URL and comes back in the callback (CSRF guard).
    # Unmarked (sign-in) state — the drive-connect flow marks its own.
    state = auth.new_oauth_state()
    resp = RedirectResponse(auth.google_login_url(state), status_code=302)
    resp.set_cookie(
        auth.STATE_COOKIE,
        auth.issue_state(state),
        httponly=True,
        secure=True,  # __Host- prefix (finding 23) — matches the delete_cookie below
        samesite="lax",
        max_age=auth.STATE_MAX_AGE,
        path="/",
    )
    return resp


async def google_callback(request):  # GET /auth/google/callback
    # Shared callback for BOTH OAuth flows on the SAME redirect_uri (owner decision
    # 2026-07-21): a drive-connect marker in the signed state distinguishes
    # connecting the owner's Google Drive (host-config write) from a plain sign-in.
    state = request.query_params.get("state") or ""
    code = request.query_params.get("code") or ""
    if not code or not auth.state_ok(request.cookies.get(auth.STATE_COOKIE), state):
        log.warning("google callback rejected: missing code or state mismatch")
    elif auth.state_is_drive(state):
        resp = _complete_drive_connect(request, code)
        if resp is not None:
            return resp
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
            resp.delete_cookie(auth.STATE_COOKIE, path="/", secure=True)
            return resp
        log.warning("google callback rejected: email not allowed or exchange failed: %r", email)
    # Never surface the email/token to the client — a generic message only.
    # Clear the one-time OAuth-state cookie on EVERY failure path too (security
    # review 2026-07-21 cookie-hygiene): only the success paths deleted it before.
    resp = templates.TemplateResponse(
        request, "login.html", {"error": "Google sign-in was not accepted."}, status_code=403
    )
    resp.delete_cookie(auth.STATE_COOKIE, path="/", secure=True)
    return resp


def _complete_drive_connect(request, code: str) -> Response | None:
    """Owner-only Google Drive connect completion (owner decision 2026-07-21):
    store the offline token as the WRITE-ONLY offsite backup credential and sign
    the owner in.

    SECURITY — a NON-OWNER must never connect a Drive. The gate is: the current
    session is ALREADY the owner, OR the just-authenticated Google email is the
    allowlisted owner (itself a valid owner sign-in). The token/refresh_token is
    handed straight to offsite_config.store_drive_token (rclone.conf, 0600) and is
    never logged or rendered. Returns a redirect on success, or None to fall
    through to the callback's generic (secret-free) rejection page."""
    token = auth.exchange_google_drive_code(code)
    if not token:
        log.warning("drive-connect rejected: token exchange failed")
        return None
    email = str(token.get("email") or "")
    session_is_owner = auth.cookie_identity(request.cookies.get(auth.COOKIE)) == auth.OWNER_IDENTITY
    if not (session_is_owner or auth.email_allowed(email)):
        log.warning("drive-connect rejected: connecting principal is not the owner")
        return None
    try:
        offsite_config.store_drive_token(token, email)
    except InvalidOperation as e:
        log.warning("drive-connect rejected: %s", e)  # message names no secret
        return None
    # The connect starts from the owner-only /backups page; sign the owner in (the
    # allowlisted-email path is itself a valid owner Google sign-in) and return to
    # the page with a success flag.
    resp = _issue_session("/backups?connected=1", auth.OWNER_IDENTITY)
    resp.delete_cookie(auth.STATE_COOKIE, path="/", secure=True)
    return resp


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
    *views_jobs.ROUTES,
    *views_export.ROUTES,
    # A11-3 confined knowledge-write surface (amended Invariant 11): capability-
    # gated, project-scoped, forced-unverified + quarantined-until-owner-promote.
    *views_write.ROUTES,
    # Owner-only authz-config carve-out (Inv. 11 amended) — authz, never knowledge.
    *views_admin.ROUTES,
    # Owner-only backup-setup surface (owner decision 2026-07-21) — host config,
    # never knowledge; secrets write-only, age private key shown once, never stored.
    *views_backup.ROUTES,
]


def build_app() -> Starlette:
    _validate_runtime_config()
    assert_route_caps_total(ROUTES)  # default-deny: no route without a tier decision
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

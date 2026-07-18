"""kbweb.views_admin — OWNER-ONLY project-share admin (the Inv. 11 carve-out).

The ONLY mutating routes in the web UI (Invariant 11, amended 2026-07-18 —
see ADVERSARIAL_REVIEW.md Amendment A11-1): authorization-config actions that
grant / revoke a READ-ONLY project share via ``kbcore.principals.grant`` /
``ungrant`` — ``rw=false`` is hardwired and the form field is never even read.
NEVER knowledge mutation; NEVER principal lifecycle (create/revoke/rotate stay
CLI-only). Do not broaden this module beyond share grant/revoke.

Enforcement layers (each independently sufficient):
1. ``SessionAuthMiddleware`` returns the router's byte-identical unknown-path
   404 for ANY /admin* request whose principal is not the OWNER — before
   routing, so method probes (405s) can't reveal the surface exists.
2. Every view here re-checks ``scope.current().is_owner`` → 404 (defense in
   depth if middleware ordering ever changes).
3. Mutating POSTs additionally require an itsdangerous-signed CSRF token bound
   to the owner identity (``kbweb.auth.issue_csrf``/``csrf_ok``; 403 when
   missing/invalid/expired) and pass a same-origin Origin/Referer check.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from kbcore import principals, scope
from kbcore.items import list_projects
from kbweb import auth
from kbweb.ui import templates


def _owner_or_404() -> None:
    """A non-owner must see the byte-identical 404 an unknown path yields.

    Never 403 — the admin surface's existence is not disclosed to grantees.
    Raised as the starlette HTTPException so the DEFAULT handler renders it,
    exactly like the router's own not-found (same body, same content-type)."""
    try:
        principal = scope.current()
    except RuntimeError:  # unset principal under server mode — never assume owner
        raise HTTPException(status_code=404) from None
    if not principal.is_owner:
        raise HTTPException(status_code=404)


def _same_origin_ok(request: Request) -> bool:
    """Defense-in-depth: an Origin/Referer naming a FOREIGN host rejects the POST.

    Absent headers pass — the signed CSRF token is the primary gate; this check
    catches the cross-site case where browsers DO attach Origin (including the
    literal ``Origin: null``, whose empty netloc fails the comparison)."""
    supplied = request.headers.get("origin") or request.headers.get("referer")
    if not supplied:
        return True
    return urlsplit(supplied).netloc == request.headers.get("host", "")


async def _authorized_form(request: Request) -> FormData:
    """Owner gate + same-origin + CSRF for a mutating admin POST → the form."""
    _owner_or_404()
    if not _same_origin_ok(request):
        raise HTTPException(status_code=403, detail="cross-origin request rejected")
    form = await request.form()
    if not auth.csrf_ok(str(form.get("csrf") or ""), auth.OWNER_IDENTITY):
        raise HTTPException(status_code=403, detail="missing or invalid CSRF token")
    return form


def admin_view(request: Request) -> Response:  # GET /admin
    """Owner dashboard: principals + their grants, and the grant/revoke forms."""
    _owner_or_404()
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "principals": principals.list_principals(),
            "projects": [p.name for p in list_projects()],
            "csrf": auth.issue_csrf(auth.OWNER_IDENTITY),
        },
    )


async def grant_share(request: Request) -> Response:  # POST /admin/grant
    form = await _authorized_form(request)
    name = str(form.get("name") or "").strip()
    project = str(form.get("project") or "").strip()
    # rw is HARDWIRED false (owner decision): the web admin only ever creates
    # read-only shares; no form field can change that.
    principals.grant(name, project, rw=False)
    return RedirectResponse("/admin", status_code=303)


async def revoke_share(request: Request) -> Response:  # POST /admin/revoke
    form = await _authorized_form(request)
    name = str(form.get("name") or "").strip()
    project = str(form.get("project") or "").strip()
    principals.ungrant(name, project)
    return RedirectResponse("/admin", status_code=303)


ROUTES = [
    Route("/admin", admin_view),
    Route("/admin/grant", grant_share, methods=["POST"]),
    Route("/admin/revoke", revoke_share, methods=["POST"]),
]

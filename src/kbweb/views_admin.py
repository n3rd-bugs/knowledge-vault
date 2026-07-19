"""kbweb.views_admin — OWNER-ONLY project-share admin (the Inv. 11 carve-out).

The ONLY mutating routes in the web UI (Invariant 11, amended 2026-07-18,
extended 2026-07-19 — see ADVERSARIAL_REVIEW.md Amendments A11-1/A11-2):
authorization-config actions that (a) grant / revoke a READ-ONLY project share
via ``kbcore.principals.grant`` / ``ungrant`` — ``rw=false`` is hardwired and
the form field is never even read — and (b) create a ``kind='google'``
email-identity grantee together with its initial read-only shares in one
submit (owner decision 2026-07-19). NEVER knowledge mutation; NEVER a secret
through the browser: the web cannot mint token principals (the kind is
hardwired "google" and never read from the form), and principal revoke /
rotate / expiry stay CLI-only. Do not broaden this module beyond share
grant/revoke/create-google-grantee.

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

import re
from urllib.parse import urlsplit

from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from kbcore import principals, scope
from kbcore.errors import InvalidOperation
from kbcore.items import list_projects
from kbweb import auth, views_admin_invite
from kbweb.ui import templates

# Server-side validation for the create-grantee form. The NAME renders into
# HTML and the invite text; the EMAIL additionally rides inside a mailto: URL —
# the conservative RFC-lite shape refuses header-injection material outright
# (%0a/%0d, commas, semicolons, whitespace and control characters can't match).
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+$")
_MAX_EMAIL_LEN = 254
_MAX_NOTE_LEN = 200


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


def _admin_page(request: Request, *, error: str | None = None, status_code: int = 200) -> Response:
    """Render the dashboard (also the re-render target for form errors — no
    oracle concern: only the owner can ever see this page)."""
    infos = principals.list_principals()
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "principals": infos,
            "projects": [p.name for p in list_projects()],
            "csrf": auth.issue_csrf(auth.OWNER_IDENTITY),
            "invites": views_admin_invite.invite_blocks(infos),
            "error": error,
        },
        status_code=status_code,
    )


def admin_view(request: Request) -> Response:  # GET /admin
    """Owner dashboard: principals + grants, the create/grant/revoke forms, and
    a copyable invite block per live google grantee."""
    _owner_or_404()
    return _admin_page(request)


async def grant_share(request: Request) -> Response:  # POST /admin/grant
    form = await _authorized_form(request)
    name = str(form.get("name") or "").strip()
    project = str(form.get("project") or "").strip()
    # Only grant a project that actually exists (same wall as create_grantee) —
    # an unknown string would otherwise upsert a dormant phantom grant that goes
    # live if a project of that name is ever created. rw is HARDWIRED false
    # (owner decision): the web admin only ever creates read-only shares.
    if project not in {p.name for p in list_projects()}:
        return _admin_page(request, error=f"unknown project {project!r}", status_code=400)
    principals.grant(name, project, rw=False)
    return RedirectResponse("/admin", status_code=303)


async def revoke_share(request: Request) -> Response:  # POST /admin/revoke
    form = await _authorized_form(request)
    name = str(form.get("name") or "").strip()
    project = str(form.get("project") or "").strip()
    principals.ungrant(name, project)
    return RedirectResponse("/admin", status_code=303)


def _grantee_form_error(name: str, email: str, note: str) -> str | None:
    """First validation failure of the create-grantee form, or None."""
    if not _NAME_RE.fullmatch(name):
        return "name must be a slug matching [a-z0-9][a-z0-9._-]{0,62}"
    if len(email) > _MAX_EMAIL_LEN or not _EMAIL_RE.fullmatch(email):
        return "email must be a plain address (no spaces, %, commas or semicolons)"
    if len(note) > _MAX_NOTE_LEN:
        return f"note must be {_MAX_NOTE_LEN} characters or fewer"
    return None


async def create_grantee(request: Request) -> Response:  # POST /admin/principal
    """Create a google grantee + its initial READ-ONLY shares in one submit.

    The kind is HARDWIRED "google" (owner decision 2026-07-19): the web never
    mints a token principal — no secret may ever transit a browser — and a
    forged ``kind`` form field is never even read. rw=False on every grant."""
    form = await _authorized_form(request)
    name = str(form.get("name") or "").strip()
    email = str(form.get("email") or "").strip().lower()
    note = str(form.get("note") or "").strip()
    error = _grantee_form_error(name, email, note)
    if error is not None:
        return _admin_page(request, error=error, status_code=400)
    try:
        principals.create_principal(name, kind="google", email=email, note=note or None)
    except InvalidOperation as e:  # e.g. duplicate name — re-render with the message
        return _admin_page(request, error=str(e), status_code=400)
    # Grant ONLY projects that actually exist (checked against list_projects,
    # owner context) — arbitrary submitted strings are silently skipped.
    known = {p.name for p in list_projects()}
    for value in form.getlist("projects"):
        project = str(value).strip()
        if project in known:
            principals.grant(name, project, rw=False)
    return RedirectResponse("/admin", status_code=303)


ROUTES = [
    Route("/admin", admin_view),
    Route("/admin/grant", grant_share, methods=["POST"]),
    Route("/admin/revoke", revoke_share, methods=["POST"]),
    Route("/admin/principal", create_grantee, methods=["POST"]),
]

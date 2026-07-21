"""kbweb.views_write — the CONFINED knowledge-write surface (Invariant 11, amended A11-3).

The ONLY kbweb module that mutates knowledge. It imports EXACTLY the composed
kbcore chokepoints — ``web_write.save_for_review``, ``web_write.supersede_for_review``,
``graph.link``, ``review.reject``, ``review.dismiss`` — and NEVER raw ingest/supersede,
never ``promote`` (trust escalation stays owner-only in kbcore.review). Every
trust/quarantine/authz/oracle guarantee already lives in kbcore (Invariant 2); this
layer adds only four things (A11-3 review):

  1. Capability ZONE WALLS — each route is its own top-level path segment
     (/save, /supersede, /link, /resolve) so the Layer-1 byte-404 wall in
     SessionAuthMiddleware gates it at its WRITE capability BEFORE routing
     (finding 22). ``_cap_or_404`` re-checks in-view (defense-in-depth).
  2. CSRF BOUND TO THE LIVE IDENTITY — :func:`_live_identity` reads the exact
     cookie identity the middleware stashed ('owner' / 'g:<name>'); every mint
     and check passes it, so grantee A's token authorizes neither B nor the
     owner (findings 13/19/25). The owner-identity constant is never referenced
     here (a trip-wire test pins that) — the live per-request value is the source
     of truth, never a reconstructed or hardcoded string.
  3. A CLOSED REQUEST SHAPE — /save parses through ``models.WebSaveRequest``
     (``extra='forbid'``): a forged trust/kind/uri/status field is a validation
     error → uniform 400, never a silently-ignored smuggle (finding 7).
  4. A UNIFORM WRITE-ERROR CONTRACT — kbcore ``NotFound`` → the byte-identical
     unknown-path 404 (a non-owner cannot tell missing from withheld);
     ``InvalidOperation`` (authz/withheld/read-only/cap) → a CONSTANT rejection
     that never echoes the raw message / project name (findings 17/20/22).

GET renders forms (mint CSRF); POST mutates. All SQL stays in kbcore.
"""

from __future__ import annotations

import re

from pydantic import ValidationError
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from kbcore import scope
from kbcore.errors import DuplicateContent, InvalidOperation, NotFound
from kbcore.graph import link
from kbcore.models import RELATIONS, WEB_ITEM_TYPES, WebSaveRequest
from kbcore.review import dismiss, reject
from kbcore.web_write import save_for_review, supersede_for_review
from kbweb import auth
from kbweb.ui import templates
from kbweb.views_admin import _same_origin_ok  # shared same-origin defense layer

_RESOLVE_ACTIONS = frozenset({"reject", "dismiss"})  # NEVER 'promote' (owner-only in kbcore)


# --- live-identity CSRF (findings 13/19/25) -----------------------------------


def _live_identity(request: Request) -> str:
    """The EXACT identity the middleware signed into the session cookie.

    'owner' or 'g:<name>', stashed on ``request.state.identity``. NEVER
    reconstruct it from Principal.name and NEVER reuse the constant owner-identity
    string — that per-request binding is precisely what stops a CSRF token minted
    for one grantee from authorizing another grantee or the owner.
    """
    return request.state.identity


def _csrf_ctx(request: Request) -> str:
    """A fresh CSRF token bound to the live identity — minted into every write form."""
    return auth.issue_csrf(_live_identity(request))


def _csrf_guard(request: Request, form: FormData) -> None:
    """Same-origin + live-identity CSRF on every mutating POST (403 on failure)."""
    if not _same_origin_ok(request):
        raise HTTPException(status_code=403, detail="cross-origin request rejected")
    if not auth.csrf_ok(str(form.get("csrf") or ""), _live_identity(request)):
        raise HTTPException(status_code=403, detail="missing or invalid CSRF token")


# --- the uniform error contract (findings 17/20/22) ---------------------------


def _cap_or_404(cap: str) -> None:
    """Layer-2 wall under the ROUTE_CAPS Layer-1 wall (the views_export pattern):
    an under-capped principal gets the router's byte-identical unknown-path 404,
    never a distinguishable 4xx — even if middleware ordering ever regresses."""
    try:
        principal = scope.current()
    except RuntimeError:  # unset principal under server mode — never assume owner
        raise HTTPException(status_code=404) from None
    if not principal.has_cap(cap):
        raise HTTPException(status_code=404)


# kbcore NotFound → the SAME bytes the zone wall / router emit (never the HTML
# _not_found handler): a non-owner cannot tell a missing id from a withheld one.
def _ghost_404() -> Response:
    raise HTTPException(status_code=404)


# A CONSTANT authz rejection — never the raw exception (which embeds the project
# name): withheld vs read-only vs cap-lacking all collapse to identical bytes.
_REJECT_BODY = (
    "<h1>Operation not permitted</h1>"
    "<p>This action is not available with your access.</p><p><a href='/'>search</a></p>"
)


def _uniform_reject() -> Response:
    return HTMLResponse(_REJECT_BODY, status_code=403)


# A malformed/forged submission (WebSaveRequest validation, bad int) — a constant
# 400 with NO per-field message (a per-field error is itself a probing oracle).
_INVALID_BODY = (
    "<h1>Invalid submission</h1><p>The form could not be accepted.</p><p><a href='/'>search</a></p>"
)


def _invalid() -> Response:
    return HTMLResponse(_INVALID_BODY, status_code=400)


def _tags(raw: str) -> list[str]:
    return [t for t in re.split(r"[\s,]+", raw.strip()) if t]


# --- affordance context for the item / review templates (rendered by other GETs) --


def item_write_ctx(request: Request, item) -> dict[str, object]:
    """Write-form context for /item/{id}: caps-gated supersede + link forms.

    A reader (no CAP_SAVE) or a contributor without WRITE on this item's project
    sees NEITHER form (``can_write`` False) — the caps × project-grant composition.
    The CSRF is bound to the live identity; the relation vocabulary drives the link
    dropdown. Called by views_items.item_view (a GET), so no kbcore write is reached.
    """
    p = getattr(request.state, "principal", None)
    can_write = (
        p is not None
        and p.has_cap(scope.CAP_SAVE)
        and (p.is_owner or item.project in p.write_projects)
    )
    return {
        "can_write": can_write,
        "write_csrf": _csrf_ctx(request) if can_write else "",
        "relations": RELATIONS,
    }


def review_resolve_ctx(request: Request) -> dict[str, object]:
    """Resolve-affordance context for /review: reject/dismiss buttons + CSRF.

    Only a principal holding CAP_REVIEW_RESOLVE (reviewer/owner) sees the buttons;
    the kbcore reject/dismiss chokepoints still AND an rw grant + unverified-target
    confinement on top (a reviewer with a read-only grant lists but cannot resolve).
    """
    p = getattr(request.state, "principal", None)
    can_resolve = p is not None and p.has_cap(scope.CAP_REVIEW_RESOLVE)
    return {
        "can_resolve": can_resolve,
        "resolve_csrf": _csrf_ctx(request) if can_resolve else "",
    }


# --- the four confined write routes -------------------------------------------


def _save_page(request: Request, *, saved: bool = False, status_code: int = 200) -> Response:
    """Render the Contribute form (also the pending-review confirmation)."""
    p = request.state.principal
    return templates.TemplateResponse(
        request,
        "save.html",
        {
            "saved": saved,
            "types": WEB_ITEM_TYPES,
            "writable": sorted(p.write_projects),
            "is_owner": p.is_owner,
            "csrf": _csrf_ctx(request),
        },
        status_code=status_code,
    )


async def save(request: Request) -> Response:  # GET renders the form, POST saves for review
    _cap_or_404(scope.CAP_SAVE)
    if request.method == "GET":
        return _save_page(request, saved=request.query_params.get("saved") == "1")
    form = await request.form()
    _csrf_guard(request, form)
    data = {key: form[key] for key in form.keys() if key != "csrf"}  # noqa: SIM118
    if "tags" in data:
        data["tags"] = _tags(str(data["tags"]))
    try:
        req = WebSaveRequest(**data)  # extra='forbid' rejects a forged trust/kind/status
    except ValidationError:
        return _invalid()
    try:
        save_for_review(
            req.project, req.type, req.title, req.body, tags=req.tags, code_ref=req.code_ref
        )
    except DuplicateContent:
        return _save_page(request, saved=True)  # a benign no-op; nothing new to queue
    except NotFound:
        return _ghost_404()
    except InvalidOperation:
        return _uniform_reject()
    return RedirectResponse("/save?saved=1", status_code=303)


async def supersede_item(request: Request) -> Response:  # POST /supersede/{id}
    _cap_or_404(scope.CAP_SAVE)
    item_id = int(request.path_params["id"])
    form = await request.form()
    _csrf_guard(request, form)
    new_body = str(form.get("new_body") or "")
    reason = str(form.get("reason") or "")
    if not new_body.strip():
        return _invalid()
    try:
        supersede_for_review(item_id, new_body=new_body, reason=reason)
    except DuplicateContent:
        return _save_page(request, saved=True)
    except NotFound:
        return _ghost_404()
    except InvalidOperation:
        return _uniform_reject()
    return RedirectResponse("/save?saved=1", status_code=303)


async def link_item(request: Request) -> Response:  # POST /link/{id}
    _cap_or_404(scope.CAP_SAVE)
    src_id = int(request.path_params["id"])
    form = await request.form()
    _csrf_guard(request, form)
    rel = str(form.get("rel") or "")
    try:
        dst_id = int(str(form.get("dst_id") or ""))
    except ValueError:
        return _invalid()
    try:
        link(src_id, rel, dst_id)
    except NotFound:
        return _ghost_404()
    except InvalidOperation:
        return _uniform_reject()
    return RedirectResponse(f"/item/{src_id}", status_code=303)


async def resolve(request: Request) -> Response:  # POST /resolve/{qid} — reject|dismiss ONLY
    _cap_or_404(scope.CAP_REVIEW_RESOLVE)
    qid = int(request.path_params["qid"])
    form = await request.form()
    _csrf_guard(request, form)
    action = str(form.get("action") or "")
    if action not in _RESOLVE_ACTIONS:
        # 'promote' (or anything else) is not a web action — kbcore.review.promote
        # is never imported, so trust escalation is structurally impossible here.
        return _invalid()
    # Do NOT pre-query review_queue (a per-case confirmation UI would leak
    # existence/state); let kbcore's WALLED reject/dismiss raise and map uniformly.
    try:
        (reject if action == "reject" else dismiss)(qid)
    except NotFound:
        return _ghost_404()
    except InvalidOperation:
        return _uniform_reject()
    return RedirectResponse("/review", status_code=303)


ROUTES = [
    Route("/save", save, methods=["GET", "POST"]),
    Route("/supersede/{id:int}", supersede_item, methods=["POST"]),
    Route("/link/{id:int}", link_item, methods=["POST"]),
    Route("/resolve/{qid:int}", resolve, methods=["POST"]),
]

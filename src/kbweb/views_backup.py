"""kbweb.views_backup — OWNER-ONLY web backup-setup surface (owner decision 2026-07-21).

An owner override of the read-only-web posture, in the SAME shape as the Inv. 11
authz carve-out (kbweb.views_admin): mutating, owner-only, CSRF- and
same-origin-guarded — but it writes HOST CONFIG (kbcore.offsite_config), never
knowledge (no items/sources/relations/review state). The no-knowledge-mutation
guard (test_phase6_web) stays green; the confined-write allowlist (test_web_caps)
gets a dedicated OWNER_CONFIG entry for the offsite_config chokepoints.

Enforcement (identical to views_admin, reused verbatim):
1. SessionAuthMiddleware walls the whole /backups zone (ROUTE_CAPS OWNER_ONLY)
   with the router's byte-identical unknown-path 404 for any non-owner — BEFORE
   routing, so a grantee/guest/anon cannot even detect the surface exists.
2. Every view re-checks ``_owner_or_404`` (defense in depth).
3. Mutating POSTs require the owner-bound CSRF token + a same-origin check
   (``_authorized_form``).

SECRETS ARE NEVER ECHOED. read_status() is masked ('••• configured' / 'not set');
the rclone credential and the age private key never render back. The age PRIVATE
identity is the sole value ever shown — EXACTLY ONCE on the keygen response,
never stored and never retrievable afterward.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from kbcore import offsite_config
from kbcore.errors import InvalidOperation
from kbweb import auth
from kbweb.ui import templates

# Reuse the EXACT admin gate (single source of truth for the owner-only + CSRF +
# same-origin discipline) — /backups is owner-only just like /admin.
from kbweb.views_admin import _authorized_form, _owner_or_404


def _truthy(value: object) -> bool:
    """Checkbox / flag parse: present-and-affirmative → True, else False."""
    return str(value or "").strip().lower() in ("1", "true", "on", "yes")


def _backups_page(
    request: Request, *, error: str | None = None, status_code: int = 200
) -> Response:
    """Render the /backups page from the MASKED status (also the form-error
    re-render target — only the owner can ever reach this view)."""
    return templates.TemplateResponse(
        request,
        "backups.html",
        {
            "status": offsite_config.read_status(),
            "csrf": auth.issue_csrf(auth.OWNER_IDENTITY),
            "error": error,
        },
        status_code=status_code,
    )


def backups_view(request: Request) -> Response:  # GET /backups
    """Owner dashboard: masked offsite status, the red residual-risk warning, the
    setup + rclone-credential + keygen forms, and the host-side checklist."""
    _owner_or_404()
    return _backups_page(request)


async def backups_config(request: Request) -> Response:  # POST /backups/config
    """Persist the four non-secret offsite keys via offsite_config.write_config.

    On a validation error re-render 400 with the FIELD message (never a secret);
    on success redirect to the masked /backups page."""
    form = await _authorized_form(request)
    keep_raw = str(form.get("keep") or "").strip()
    try:
        keep = int(keep_raw)
    except ValueError:
        return _backups_page(
            request, error="keep must be an integer between 1 and 365", status_code=400
        )
    try:
        offsite_config.write_config(
            enabled=_truthy(form.get("enabled")),
            rclone_remote=str(form.get("rclone_remote") or "").strip(),
            age_recipient=str(form.get("age_recipient") or "").strip(),
            keep=keep,
        )
    except InvalidOperation as e:
        return _backups_page(request, error=str(e), status_code=400)
    return RedirectResponse("/backups", status_code=303)


async def backups_keygen(request: Request) -> Response:  # POST /backups/keygen
    """Generate an age keypair, store ONLY the public recipient, and show the
    PRIVATE identity EXACTLY ONCE.

    The identity is rendered on THIS response and never persisted, never logged,
    never retrievable again (a later GET /backups shows only the public
    recipient). The recipient is merged into the existing config so the other
    settings survive."""
    await _authorized_form(request)  # owner + CSRF + same-origin
    try:
        recipient, identity = offsite_config.generate_age_keypair()
        current = offsite_config.read_status()
        offsite_config.write_config(
            enabled=current["enabled"],
            rclone_remote=current["rclone_remote"],
            age_recipient=recipient,  # only the PUBLIC key is stored
            keep=current["keep"],
        )
    except InvalidOperation as e:
        return _backups_page(request, error=str(e), status_code=400)
    # Dedicated one-time page: the private identity appears here and NOWHERE else.
    return templates.TemplateResponse(
        request,
        "backup_key.html",
        {"identity": identity, "recipient": recipient},
    )


async def backups_rclone(request: Request) -> Response:  # POST /backups/rclone
    """Store the pasted rclone credential WRITE-ONLY (offsite_config.store_rclone_cred).

    The pasted value never re-renders (redirect to the masked page shows only
    '••• configured'); on a validation error re-render 400 without echoing it."""
    form = await _authorized_form(request)
    try:
        offsite_config.store_rclone_cred(str(form.get("rclone_cred") or ""))
    except InvalidOperation as e:
        return _backups_page(request, error=str(e), status_code=400)
    return RedirectResponse("/backups", status_code=303)


ROUTES = [
    Route("/backups", backups_view),
    Route("/backups/config", backups_config, methods=["POST"]),
    Route("/backups/keygen", backups_keygen, methods=["POST"]),
    Route("/backups/rclone", backups_rclone, methods=["POST"]),
]

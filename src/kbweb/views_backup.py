"""kbweb.views_backup — OWNER-ONLY, EMAIL-CENTRIC web backup surface (2026-07-21).

Radically simplified per the owner request 2026-07-21: encrypted backups go to the
OWNER's OWN Google Drive via a ONE-CLICK OAuth connect (reusing the sign-in OAuth
client), and the page shows ONLY the account email — every rclone/age/retention
internal is HIDDEN and auto-set by the connect/keygen flows. Same security shape as
before (and as kbweb.views_admin): an owner override of the read-only-web posture
that writes HOST CONFIG (kbcore.offsite_config), never knowledge (no
items/sources/relations/review state). The no-knowledge-mutation guard
(test_phase6_web) stays green; the confined-write allowlist (test_web_caps) carries
a dedicated OWNER_CONFIG entry for the offsite_config chokepoints.

Enforcement (identical to views_admin, reused verbatim):
1. SessionAuthMiddleware walls the whole /backups zone (ROUTE_CAPS OWNER_ONLY)
   with the router's byte-identical unknown-path 404 for any non-owner — BEFORE
   routing, so a grantee/guest/anon cannot even detect the surface exists.
2. Every view re-checks ``_owner_or_404`` (defense in depth).
3. Mutating POSTs require the owner-bound CSRF token + a same-origin check
   (``_authorized_form``). ``/backups/connect`` is a GET that only STARTS the
   Google redirect; the signed __Host- state cookie is the callback's CSRF guard,
   and the callback additionally re-checks the connecting principal is the owner.

SECRETS ARE NEVER ECHOED. The Drive refresh token lives WRITE-ONLY in rclone.conf
(never read back; read_status reports only the connected email + a connected
bool). The age PRIVATE recovery key is DOWNLOADED as a file attachment on the
keygen response — never rendered into a page, never stored, never logged; only its
PUBLIC recipient is persisted.
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

# The downloaded recovery-key filename (the age private identity is the file BODY).
_RECOVERY_KEY_FILENAME = "kb-backup-recovery-key.txt"


def _truthy(value: object) -> bool:
    """Checkbox / flag parse: present-and-affirmative → True, else False."""
    return str(value or "").strip().lower() in ("1", "true", "on", "yes")


def _backups_page(
    request: Request, *, error: str | None = None, status_code: int = 200
) -> Response:
    """Render /backups from the MASKED status (also the form-error re-render target
    — only the owner can ever reach this view). ``connected`` flags the post-connect
    success banner (a plain query flag, never reflected content)."""
    return templates.TemplateResponse(
        request,
        "backups.html",
        {
            "status": offsite_config.read_status(),
            "csrf": auth.issue_csrf(auth.OWNER_IDENTITY),
            "error": error,
            "connected": request.query_params.get("connected") == "1",
        },
        status_code=status_code,
    )


def backups_view(request: Request) -> Response:  # GET /backups
    """Owner dashboard: the email-centric status + the three one-click actions."""
    _owner_or_404()
    return _backups_page(request)


def backups_connect(request: Request) -> Response:  # GET /backups/connect
    """Start the one-click Google Drive connect (owner-only).

    Redirects to Google's consent screen for the least-privilege ``drive.file``
    scope (offline, force-consent → refresh_token). The random state is MARKED for
    the drive-connect flow and signed into the short-lived __Host- state cookie —
    server.google_callback validates that cookie, so the marker is unforgeable and a
    non-owner cannot complete a connect. No CSRF token needed: this GET only starts
    the redirect; the state cookie guards the callback."""
    _owner_or_404()
    state = auth.new_oauth_state(drive=True)
    resp = RedirectResponse(auth.google_drive_connect_url(state), status_code=302)
    resp.set_cookie(
        auth.STATE_COOKIE,
        auth.issue_state(state),
        httponly=True,
        secure=True,  # __Host- prefix requires Secure + Path=/ + no Domain
        samesite="lax",
        max_age=auth.STATE_MAX_AGE,
        path="/",
    )
    return resp


async def backups_config(request: Request) -> Response:  # POST /backups/config
    """Toggle nightly backups. The form supplies ONLY ``enabled`` — the remote,
    recipient and retention are AUTO-SET by the connect/keygen flows and merely
    carried through here (the owner never sees them).

    Enablement guard (also enforced in the UI): refuse to ENABLE until BOTH a Drive
    is connected AND a recovery key exists — otherwise a restore would be
    impossible. Disabling is always allowed. On validation error re-render 400 with
    a plain message (never a secret); on success redirect to the masked page."""
    form = await _authorized_form(request)
    enabled = _truthy(form.get("enabled"))
    status = offsite_config.read_status()
    if enabled and not (status["drive_connected"] and status["age_recipient"]):
        return _backups_page(
            request,
            error="Connect your Google Drive and download your recovery key first.",
            status_code=400,
        )
    try:
        offsite_config.write_config(
            enabled=enabled,
            rclone_remote=status["rclone_remote"],  # auto-set by the Drive connect
            age_recipient=status["age_recipient"],  # auto-set by keygen
            keep=status["keep"],
        )
    except InvalidOperation as e:
        return _backups_page(request, error=str(e), status_code=400)
    return RedirectResponse("/backups", status_code=303)


async def backups_keygen(request: Request) -> Response:  # POST /backups/keygen
    """Generate an age recovery keypair, store ONLY the public recipient, and return
    the PRIVATE key as a file DOWNLOAD.

    The private identity is the response BODY (application/octet-stream, an
    ``attachment`` Content-Disposition, Cache-Control no-store) and appears NOWHERE
    else — never in an HTML page, never persisted, never logged. Only the public
    recipient is merged into the config (so the other settings survive). Losing the
    downloaded file means the *.age backups cannot be restored — which is the whole
    point of keeping it off this box."""
    await _authorized_form(request)  # owner + CSRF + same-origin
    try:
        recipient, identity = offsite_config.generate_age_keypair()
        status = offsite_config.read_status()
        offsite_config.write_config(
            enabled=status["enabled"],
            rclone_remote=status["rclone_remote"],
            age_recipient=recipient,  # only the PUBLIC key is stored
            keep=status["keep"],
        )
    except InvalidOperation as e:
        return _backups_page(request, error=str(e), status_code=400)
    # The crown-jewel private key is DOWNLOADED, never rendered: octet-stream +
    # attachment forces a save dialog; no-store keeps it out of bfcache/any cache.
    return Response(
        content=identity + "\n",
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{_RECOVERY_KEY_FILENAME}"',
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )


ROUTES = [
    Route("/backups", backups_view),
    Route("/backups/connect", backups_connect),
    Route("/backups/config", backups_config, methods=["POST"]),
    Route("/backups/keygen", backups_keygen, methods=["POST"]),
]

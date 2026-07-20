"""kbweb.views_admin_invite — invite-text/mailto helpers (Inv. 11, A11-2).

Pure FORMATTING for the owner-only admin surface and the /welcome page: no DB
write, no principal lifecycle, no route of its own. The server never sends an
email — the ``mailto:`` link opens the OWNER'S OWN mail client. Nothing here can
embed a secret by construction: a google grantee HAS no token, and the text is
built solely from the principal's name/email, its granted project list, and the
public URLs in settings (never hardcoded hosts — CLAUDE.md).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from urllib.parse import quote

from kbcore.config import settings
from kbcore.principals import PrincipalInfo

INVITE_SUBJECT = "You've been given access to the Knowledge Vault"

# Empty public URLs must DEGRADE (a placeholder telling the owner what to set),
# never crash — the admin page still renders on a half-configured instance.
_MCP_PLACEHOLDER = "(connector URL not published yet — owner: set KB_PUBLIC_URL)"
_WEB_PLACEHOLDER = "(web URL not published yet — owner: set KB_WEB_PUBLIC_URL)"


def public_urls() -> tuple[str, str]:
    """``(mcp_url, web_url)`` for display — placeholders when settings are empty."""
    mcp_base = settings.kb_public_url.strip().rstrip("/")
    web_base = settings.kb_web_public_url.strip().rstrip("/")
    return (
        f"{mcp_base}/mcp" if mcp_base else _MCP_PLACEHOLDER,
        web_base if web_base else _WEB_PLACEHOLDER,
    )


def invite_text(name: str, projects: Sequence[str], role: str = "reader") -> str:
    """The plain-text invite for one google grantee (personalized, ~15 lines).

    Contains ONLY the principal name, its granted project list, the two public
    URLs and fixed prose — no token, no secret, nothing recoverable (asserted
    by tests). The recipient signs in with their own Google account.
    ``role``-aware (Phase 3, plan §5): the what-you-can-do line reflects the
    grantee's OWN tier — a guest reads a search-only description, never a
    catalog of what other tiers could do.
    """
    mcp_url, web_url = public_urls()
    project_lines = "\n".join(f"  - {p}" for p in projects)
    if not project_lines:
        project_lines = "  (none yet — the owner will grant projects shortly)"
    if role == "guest":
        can_do = (
            "3. You can search and open items in the projects listed above\n"
            "   (search-only access). You cannot write or change anything, and\n"
            "   no other projects are visible to you."
        )
    else:
        can_do = (
            "3. You can search and read the projects listed above. You cannot write or\n"
            "   change anything, and no other projects are visible to you."
        )
    return f"""Hi {name},

You've been given READ-ONLY access to the Knowledge Vault. Projects you can read:
{project_lines}

Getting started:
1. In claude.ai: Settings -> Connectors -> Add custom connector -> URL:
   {mcp_url}
   then complete "Sign in with Google" using THIS email address.
2. In a browser: open {web_url} and click "Sign in with Google".
{can_do}
4. Access can be revoked by the owner at any time.

Questions? Just reply to this email.
"""


def mailto_href(email: str, text: str) -> str:
    """A ``mailto:`` URL — every component percent-encoded via urllib.parse.quote.

    Newlines in the body become %0A; the address itself was validated upstream
    (views_admin: no %, commas, semicolons, whitespace or control characters),
    so nothing here can smuggle extra headers into the owner's mail client."""
    return (
        "mailto:"
        + quote(email, safe="@+")
        + "?subject="
        + quote(INVITE_SUBJECT, safe="")
        + "&body="
        + quote(text, safe="")
    )


def invite_blocks(infos: Iterable[PrincipalInfo]) -> dict[str, dict[str, str]]:
    """``name -> {text, mailto}`` for every LIVE google principal (admin blocks)."""
    out: dict[str, dict[str, str]] = {}
    for p in infos:
        if p.kind != "google" or p.revoked_at is not None or not p.email:
            continue
        text = invite_text(p.name, [project for project, _rw in p.grants], role=p.role)
        out[p.name] = {"text": text, "mailto": mailto_href(p.email, text)}
    return out

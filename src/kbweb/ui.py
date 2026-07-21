"""kbweb.ui — shared template environment, markdown pipeline, param helpers (Inv. 11).

Owns the Jinja2Templates instance so view modules never import from server
(no circular imports) and presentation concerns stay in one place. HTML
rendering (markdown + pygments) lives HERE, not in kbcore — kbcore.render
stays plain-text so CLI/MCP output is unchanged.
"""

from __future__ import annotations

import hashlib
import html
from functools import lru_cache
from pathlib import Path

from markdown_it import MarkdownIt
from markupsafe import Markup
from pygments import highlight as _pyg_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound
from starlette.requests import Request
from starlette.templating import Jinja2Templates

_TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# Above this, a single fenced block is served escaped-but-unhighlighted:
# pygments on pathological fences is the only real CPU risk on this page.
_MAX_HL_CHARS = 20_000
_FORMATTER = HtmlFormatter(nowrap=True)


def _highlight(code: str, lang: str, attrs: str) -> str:
    """Fenced-code renderer for markdown-it: pygments or escaped fallback."""
    if lang and len(code) <= _MAX_HL_CHARS:
        try:
            lexer = get_lexer_by_name(lang)
            return f'<pre class="hl"><code>{_pyg_highlight(code, lexer, _FORMATTER)}</code></pre>'
        except ClassNotFound:
            pass
    return f'<pre class="hl"><code>{html.escape(code)}</code></pre>'


# html=False ESCAPES raw HTML in bodies — the XSS defense for stored content
# (SEC-3 spirit: item bodies are data, never markup). markdown-it's built-in
# link validator additionally refuses javascript:/vbscript:/data: hrefs.
_MD = MarkdownIt("commonmark", {"html": False, "highlight": _highlight})
_MD.enable("table")
_MD.enable("strikethrough")


@lru_cache(maxsize=256)  # bodies are immutable (Invariant 1) — cache by content
def render_markdown(body: str) -> Markup:
    """Item body → safe HTML (raw HTML escaped, fences highlighted)."""
    return Markup(_MD.render(body))  # noqa: S704 — html=False escapes raw HTML above


@lru_cache(maxsize=1)
def code_css() -> str:
    """Pygments token styles for both themes, generated from the installed lib.

    Light: ``default`` style; dark: ``lightbulb`` under the same [data-theme] /
    prefers-color-scheme conditions app.css uses. Served at /static/code.css —
    never committed, never drifts from the pygments version.
    """
    light = HtmlFormatter(style="default").get_style_defs(".hl")
    dark = HtmlFormatter(style="lightbulb").get_style_defs(".hl")
    dark_scoped = dark.replace(".hl", ':root[data-theme="dark"] .hl')
    dark_media = dark.replace(".hl", ':root:not([data-theme="light"]) .hl')
    return f"{light}\n{dark_scoped}\n@media (prefers-color-scheme: dark) {{\n{dark_media}\n}}\n"


def _asset_version() -> str:
    """Content hash of the static assets, used as a ?v= cache-buster.

    The public site sits behind Cloudflare, which edge-caches CSS/JS for hours;
    a content-derived version in the URL makes every deploy a cache MISS without
    manual purges. Computed once at import (assets are baked into the image).
    """
    h = hashlib.sha1()  # noqa: S324 — cache key, not security
    for name in ("app.css", "app.js"):
        p = STATIC_DIR / name
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:8]


ASSET_V = _asset_version()


def _identity_ctx(request: Request) -> dict[str, object]:
    """Template context: chrome flags from the LIVE resolved principal.

    ``SessionAuthMiddleware`` binds the request's resolved principal onto
    ``request.state``; ``nav`` maps each ROUTE_CAPS zone to whether THIS
    principal holds its capability — the nav renders strictly from the same
    table the middleware walls with, so a link can never outrun its wall and a
    user never sees a hint for a zone they lack (no-oracle for features; fixes
    the old bug where grantees saw review/setup links that 400'd). Pre-auth
    requests (/login, /static) carry no principal — anonymous chrome, zero
    links. Display-only: route AUTHORIZATION stays in SessionAuthMiddleware +
    the in-view guards, never these flags (tiered access, plan §2).
    """
    from kbweb.server import ROUTE_CAPS  # runtime import: server imports ui at load

    p = getattr(request.state, "principal", None)
    return {
        "signed_in": p is not None,
        "is_owner": p is not None and p.is_owner,
        # Chrome-only tier line (UX refactor): the principal's OWN role label and
        # the count of projects IT can read — both already surfaced verbatim on
        # /welcome, so this reveals nothing new and no secret. Owner's
        # read_projects is empty (unrestricted, not enumerated); the template
        # shows "owner" for it instead of a count.
        "role": p.role if p is not None else None,
        "read_project_count": len(p.read_projects) if p is not None else 0,
        "nav": {zone: p is not None and p.has_cap(cap) for zone, cap in ROUTE_CAPS.items()},
    }


def create_templates() -> Jinja2Templates:
    """The app-wide template environment; filters/globals are registered here."""
    t = Jinja2Templates(directory=str(_TEMPLATE_DIR), context_processors=[_identity_ctx])
    t.env.filters["markdown"] = render_markdown
    t.env.globals["asset_v"] = ASSET_V
    return t


templates = create_templates()


def int_param(request: Request, name: str, default: int, lo: int, hi: int) -> int:
    """Clamped integer GET param; malformed values fall back to the default."""
    raw = request.query_params.get(name)
    try:
        val = int(raw) if raw is not None else default
    except ValueError:
        val = default
    return max(lo, min(hi, val))


def str_param(request: Request, name: str, allowed: tuple[str, ...] | None = None) -> str | None:
    """Stripped string GET param; empty or not-in-``allowed`` collapses to None."""
    val = (request.query_params.get(name) or "").strip()
    if not val:
        return None
    if allowed is not None and val not in allowed:
        return None
    return val

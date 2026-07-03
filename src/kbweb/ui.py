"""kbweb.ui — shared template environment and request-param helpers (Inv. 11).

Owns the Jinja2Templates instance so view modules never import from server
(no circular imports) and presentation concerns stay in one place. The
markdown/pygments pipeline (phase web-2) also lives here — kbcore.render stays
plain-text for CLI/MCP parity.
"""

from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.templating import Jinja2Templates

_TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def create_templates() -> Jinja2Templates:
    """The app-wide template environment; filters/globals are registered here."""
    return Jinja2Templates(directory=str(_TEMPLATE_DIR))


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

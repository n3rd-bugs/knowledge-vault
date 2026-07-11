"""kbweb.views_meta — projects, dashboard, review viewer (read-only, Inv. 11).

``list_open_review`` is a pure read (the review WRITE functions — promote,
reject, resolve_via_supersede — are never imported here; the per-module
write-name test enforces it).
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from kbcore import scope
from kbcore.browse import list_tags
from kbcore.config import settings
from kbcore.errors import NotFound
from kbcore.items import list_projects, timeline
from kbcore.review import list_open_review
from kbcore.stats import stats
from kbweb.charts import bar_rows, stacked_segments
from kbweb.ui import templates

_STATUS_ORDER = ("active", "superseded", "duplicate", "archived")
_TRUST_ORDER = ("verified", "unverified")


def projects_view(request: Request) -> Response:  # GET /projects
    return templates.TemplateResponse(request, "projects.html", {"projects": list_projects()})


def project_view(request: Request) -> Response:  # GET /project/{name}
    name = request.path_params["name"]
    info = next((p for p in list_projects() if p.name == name), None)
    if info is None:
        # Withheld (public-only) and missing look identical — list_projects is
        # already scope-gated, so this leaks no existence signal.
        raise NotFound(f"project {name} does not exist")
    return templates.TemplateResponse(
        request,
        "project.html",
        {
            "p": info,
            "entries": timeline(name, limit=25),
            "tags": list_tags(project=name, limit=40),
        },
    )


def stats_view(request: Request) -> Response:  # GET /stats — the dashboard
    s = stats()
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "s": s,
            "type_rows": bar_rows(s["by_type"]),
            "project_rows": bar_rows(s["by_project"], href_fmt="/project/{}"),
            "status_segs": stacked_segments(s["by_status"], _STATUS_ORDER),
            "trust_segs": stacked_segments(s["by_trust"], _TRUST_ORDER),
        },
    )


def review_view(request: Request) -> Response:  # GET /review — list only, no actions
    # Owner-only (per-project sharing v1): review_queue.detail JSONB can carry
    # cross-project titles/ids that the row-level scope gate does not cover, so a
    # grantee must not see the review queue at all.
    scope.require_owner("the review queue")
    return templates.TemplateResponse(request, "review.html", {"entries": list_open_review()})


def setup_view(request: Request) -> Response:  # GET /setup — connect a target machine
    """Target-machine setup: the connector token + copy-paste steps.

    Owner-only: this page renders the MASTER bearer — a grantee must never see it
    (they use their own kbg_ token / Google identity). Auth-gated by
    SessionAuthMiddleware; still a pure read — Invariant 11 (no write imports) holds.
    """
    scope.require_owner("the setup page")
    base = settings.kb_public_url.rstrip("/")
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "token": settings.kb_token,
            # Empty base (KB_PUBLIC_URL unset) must not render a bare, misleading
            # "/mcp" — leave it blank so the page degrades instead of lying.
            "mcp_url": f"{base}/mcp" if base else "",
            "rest_url": (settings.kb_public_rest_url or "").rstrip("/"),
            "web_url": settings.kb_web_public_url.rstrip("/"),
        },
    )


ROUTES = [
    Route("/projects", projects_view),
    Route("/project/{name}", project_view),
    Route("/stats", stats_view),
    Route("/review", review_view),
    Route("/setup", setup_view),
]

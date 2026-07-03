"""kbweb.views_meta — projects, stats, review (read-only, Inv. 11).

``list_open_review`` is a pure read (the review WRITE functions — promote,
reject, resolve_via_supersede — are never imported here; the per-module
write-name test enforces it).
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from kbcore.browse import list_tags
from kbcore.errors import NotFound
from kbcore.items import list_projects, timeline
from kbcore.render import render_stats
from kbcore.stats import stats
from kbweb.ui import templates


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


def stats_view(request: Request) -> Response:  # GET /stats
    return templates.TemplateResponse(request, "stats.html", {"stats_text": render_stats(stats())})


ROUTES = [
    Route("/projects", projects_view),
    Route("/project/{name}", project_view),
    Route("/stats", stats_view),
]

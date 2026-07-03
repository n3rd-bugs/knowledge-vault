"""kbweb.views_search — search with filters + tag browsing (read-only, Inv. 11).

Name-level imports only: no kbcore write name may enter this module's
namespace (the read-only test asserts it for every kbweb module).
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from kbcore.browse import items_with_tag, list_tags
from kbcore.items import list_projects
from kbcore.models import ITEM_TYPES
from kbcore.search import search
from kbweb.ui import int_param, str_param, templates


def index(request: Request) -> Response:  # GET /
    q_raw = request.query_params.get("q")
    q = (q_raw or "").strip()
    # A submitted-but-blank phrase (q present, empty after strip) must not look
    # like the fresh landing page — the user asked for something and got nothing.
    submitted_empty = q_raw is not None and not q
    project = str_param(request, "project")
    domain = str_param(request, "domain")
    type_ = str_param(request, "type", tuple(ITEM_TYPES))
    tag = str_param(request, "tag")
    k = int_param(request, "k", 10, 1, 50)
    include_superseded = request.query_params.get("superseded") == "1"
    include_archived = request.query_params.get("archived") == "1"

    hits = (
        search(
            q,
            project=project,
            domain=domain,
            type=type_,
            tags=[tag] if tag else None,
            include_superseded=include_superseded,
            include_archived=include_archived,
            k=k,
            tool="web.search",
        )
        if q
        else []
    )
    projects = list_projects()
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "q": q,
            "hits": hits,
            "submitted_empty": submitted_empty,
            # Landing state: offer top tags as starting points instead of a blank page.
            "top_tags": list_tags(limit=24) if not q else [],
            "projects": projects,
            "domains": sorted({p.domain for p in projects if p.domain}),
            "item_types": ITEM_TYPES,
            "f_project": project,
            "f_domain": domain,
            "f_type": type_,
            "f_tag": tag,
            "f_k": k,
            "f_superseded": include_superseded,
            "f_archived": include_archived,
        },
    )


def tag_view(request: Request) -> Response:  # GET /tags/{tag}
    tag = request.path_params["tag"]
    project = str_param(request, "project")
    return templates.TemplateResponse(
        request,
        "tags.html",
        {
            "tag": tag,
            "project": project,
            "items": items_with_tag(tag, project=project),
            "top_tags": list_tags(project=project, limit=40),
        },
    )


ROUTES = [
    Route("/", index),
    Route("/tags/{tag}", tag_view),
]

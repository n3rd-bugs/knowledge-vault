"""kbweb.views_items — item detail, timeline, trace, similar (read-only, Inv. 11)."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from kbcore.browse import item_refs
from kbcore.graph import similar, trace
from kbcore.items import get_item, list_projects, timeline
from kbweb.ui import int_param, str_param, templates


def item_view(request: Request) -> Response:  # GET /item/{item_id}
    item = get_item(int(request.path_params["item_id"]))
    other_ids = {r.dst_id if r.direction == "out" else r.src_id for r in item.relations}
    return templates.TemplateResponse(
        request, "item.html", {"item": item, "refs": item_refs(other_ids)}
    )


def timeline_view(request: Request) -> Response:  # GET /timeline?project=
    project = (request.query_params.get("project") or "").strip()
    entries = timeline(project, limit=100) if project else []
    return templates.TemplateResponse(
        request,
        "timeline.html",
        {"project": project, "entries": entries, "projects": list_projects()},
    )


def trace_view(request: Request) -> Response:  # GET /trace/{node_id}
    node_id = int(request.path_params["node_id"])
    kind = str_param(request, "kind", ("item", "entity")) or "item"
    depth = int_param(request, "depth", 2, 1, 4)
    direction = str_param(request, "direction", ("both", "out", "in")) or "both"
    tree = trace(kind, node_id, direction=direction, depth=depth)
    return templates.TemplateResponse(
        request,
        "trace.html",
        {"node_id": node_id, "kind": kind, "depth": depth, "direction": direction, "tree": tree},
    )


def similar_view(request: Request) -> Response:  # GET /similar/{item_id}
    item_id = int(request.path_params["item_id"])
    hits = similar(item_id, k=8)
    return templates.TemplateResponse(request, "similar.html", {"item_id": item_id, "hits": hits})


ROUTES = [
    Route("/item/{item_id:int}", item_view),
    Route("/timeline", timeline_view),
    Route("/trace/{node_id:int}", trace_view),
    Route("/similar/{item_id:int}", similar_view),
]

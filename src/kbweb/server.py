"""kbweb.server — Phase 6b read-only web UI (Starlette + Jinja, PLAN.md §10).

READ-ONLY by construction (Invariant 11): every data route is GET and this module
imports only kbcore read surfaces + render — no ingest/supersede/link/review-write
is importable here (a test asserts it). All access is gated by a signed session
cookie; unauthenticated requests are redirected to /login. Run:
``python -m kbweb.server``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from kbcore import render
from kbcore.config import settings
from kbcore.errors import KBError, NotFound
from kbcore.graph import similar, trace
from kbcore.items import get_item, list_projects, timeline
from kbcore.search import search
from kbcore.stats import stats
from kbmcp.server import _validate_runtime_config
from kbweb import auth

log = logging.getLogger("kbweb")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class SessionAuthMiddleware(BaseHTTPMiddleware):
    """Redirect any unauthenticated request to /login (except /login and /static)."""

    async def dispatch(self, request, call_next):
        path = request.url.path
        if path == "/login" or path.startswith("/static"):
            return await call_next(request)
        if not auth.cookie_valid(request.cookies.get(auth.COOKIE)):
            return RedirectResponse("/login", status_code=302)
        return await call_next(request)


# --- views (all GET, read-only) ---------------------------------------------


def index(request):  # GET /
    q = (request.query_params.get("q") or "").strip()
    hits = search(q, k=10, tool="web.search") if q else []
    return templates.TemplateResponse(request, "search.html", {"q": q, "hits": hits})


def item_view(request):  # GET /item/{id}
    item = get_item(int(request.path_params["item_id"]))
    return templates.TemplateResponse(
        request, "item.html", {"item": item, "item_text": render.render_item(item)}
    )


def timeline_view(request):  # GET /timeline?project=
    project = (request.query_params.get("project") or "").strip()
    entries = timeline(project, limit=100) if project else []
    return templates.TemplateResponse(
        request,
        "timeline.html",
        {"project": project, "entries": entries, "projects": list_projects()},
    )


def trace_view(request):  # GET /trace/{id}
    node_id = int(request.path_params["node_id"])
    kind = (request.query_params.get("kind") or "item").strip()
    tree = trace(kind, node_id, depth=2)
    return templates.TemplateResponse(
        request, "trace.html", {"node_id": node_id, "trace_text": render.render_trace(tree)}
    )


def similar_view(request):  # GET /similar/{id}
    item_id = int(request.path_params["item_id"])
    hits = similar(item_id, k=8)
    return templates.TemplateResponse(request, "similar.html", {"item_id": item_id, "hits": hits})


def stats_view(request):  # GET /stats
    return templates.TemplateResponse(
        request, "stats.html", {"stats_text": render.render_stats(stats())}
    )


async def login(request):  # GET shows form, POST checks token
    error = None
    if request.method == "POST":
        form = await request.form()
        if auth.token_ok(str(form.get("token") or "")):
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(
                auth.COOKIE,
                auth.issue_cookie(),
                httponly=True,
                samesite="lax",
                max_age=auth.MAX_AGE,
            )
            return resp
        error = "Invalid token."
    status = 401 if error else 200
    return templates.TemplateResponse(request, "login.html", {"error": error}, status_code=status)


def _not_found(request, exc) -> Response:
    return HTMLResponse("<h1>404 — not found</h1><p><a href='/'>search</a></p>", status_code=404)


def _bad_request(request, exc) -> Response:
    return HTMLResponse(f"<h1>400 — {exc}</h1><p><a href='/'>search</a></p>", status_code=400)


ROUTES = [
    Route("/", index),
    Route("/login", login, methods=["GET", "POST"]),
    Route("/item/{item_id:int}", item_view),
    Route("/timeline", timeline_view),
    Route("/trace/{node_id:int}", trace_view),
    Route("/similar/{item_id:int}", similar_view),
    Route("/stats", stats_view),
]


def build_app() -> Starlette:
    _validate_runtime_config()
    return Starlette(
        routes=ROUTES,
        middleware=[Middleware(SessionAuthMiddleware)],
        exception_handlers={NotFound: _not_found, KBError: _bad_request},
    )


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log.info("kb-web starting on %s:%s", settings.kb_bind, settings.kb_web_port)
    uvicorn.run(build_app(), host=settings.kb_bind, port=settings.kb_web_port, log_level="info")


if __name__ == "__main__":
    main()

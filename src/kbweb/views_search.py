"""kbweb.views_search — search landing page (read-only, Inv. 11).

Name-level imports only: no kbcore write name may enter this module's
namespace (the read-only test asserts it for every kbweb module).
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from kbcore.search import search
from kbweb.ui import templates


def index(request: Request) -> Response:  # GET /
    q = (request.query_params.get("q") or "").strip()
    hits = search(q, k=10, tool="web.search") if q else []
    return templates.TemplateResponse(request, "search.html", {"q": q, "hits": hits})


ROUTES = [Route("/", index)]

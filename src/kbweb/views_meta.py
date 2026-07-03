"""kbweb.views_meta — stats (dashboard in web-4), projects/review to come (Inv. 11)."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from kbcore.render import render_stats
from kbcore.stats import stats
from kbweb.ui import templates


def stats_view(request: Request) -> Response:  # GET /stats
    return templates.TemplateResponse(request, "stats.html", {"stats_text": render_stats(stats())})


ROUTES = [Route("/stats", stats_view)]

"""kbweb.views_export — GET-only scoped export of the current result set (Inv. 11).

CAP_EXPORT-gated (WEBUI_TIERED_ACCESS_PLAN §3d / §6 Q4): NO role bundle carries
the capability at launch — owner plus future named grants only — and the
``ROUTE_CAPS`` zone wall answers everyone else with the router's byte-identical
unknown-path 404. The export reuses ``kbcore.search.search`` — the SAME
scope-gated read the index view calls — so an export can never contain a row
the principal's own search would not return, and search's ``_log_access``
stamps every exported item into ``access_log`` (tool='web.export') under the
requesting principal (audit AC). Safeguards: a HARD row cap
(``settings.kb_web_export_cap``) and CSV formula-injection neutralization (a
leading ``=``, ``+``, ``-`` or ``@`` cell is prefixed with a single quote).
GET/HEAD only; pure reads — Invariant 11 holds.
"""

from __future__ import annotations

import csv
import io

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from kbcore import scope
from kbcore.config import settings
from kbcore.models import ITEM_TYPES, SearchHit
from kbcore.search import search
from kbweb.ui import str_param

# CSV formula-injection guard: cells starting with any of these are executed as
# formulas by spreadsheet apps; a leading single quote forces text rendering.
_FORMULA_LEAD = ("=", "+", "-", "@")

_CSV_COLUMNS = (
    "item_id",
    "title",
    "type",
    "project",
    "domain",
    "status",
    "trust",
    "tags",
    "created_at",
    "source_uri",
    "superseded_by",
    "score",
    "snippet",
)


def _cap_or_404() -> None:
    """Defense-in-depth under the ROUTE_CAPS wall (the views_admin layering):
    if middleware ordering ever regresses, an un-capped principal still gets
    the default handler's byte-identical unknown-path 404."""
    try:
        principal = scope.current()
    except RuntimeError:  # unset principal under server mode — never assume owner
        raise HTTPException(status_code=404) from None
    if not principal.has_cap(scope.CAP_EXPORT):
        raise HTTPException(status_code=404)


def _neutralize(cell: str) -> str:
    """A leading formula trigger becomes literal text (single-quote prefix)."""
    return f"'{cell}" if cell.startswith(_FORMULA_LEAD) else cell


def _result_set(request: Request) -> list[SearchHit]:
    """The CURRENT search/filter result set (index-view param semantics).

    Scope-gating and per-principal access_log stamping both come from
    ``kbcore.search.search`` itself; ``k`` is pinned to the hard cap and the
    slice re-asserts it so the cap holds even if a future search ``k`` drifts.
    """
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return []
    cap = max(1, settings.kb_web_export_cap)
    tag = str_param(request, "tag")
    hits = search(
        q,
        project=str_param(request, "project"),
        domain=str_param(request, "domain"),
        type=str_param(request, "type", tuple(ITEM_TYPES)),
        tags=[tag] if tag else None,
        include_superseded=request.query_params.get("superseded") == "1",
        include_archived=request.query_params.get("archived") == "1",
        k=cap,
        tool="web.export",
    )
    return hits[:cap]


def export_csv(request: Request) -> Response:  # GET /export.csv
    _cap_or_404()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_COLUMNS)
    for h in _result_set(request):
        writer.writerow(
            [
                h.item_id,
                _neutralize(h.title),
                _neutralize(h.type),
                _neutralize(h.project),
                _neutralize(h.domain or ""),
                _neutralize(h.status),
                _neutralize(h.trust),
                _neutralize(" ".join(h.tags)),
                h.created_at.isoformat(),
                _neutralize(h.source_uri or ""),
                h.superseded_by if h.superseded_by is not None else "",
                h.score,
                _neutralize(h.snippet),
            ]
        )
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="kb-export.csv"'},
    )


def export_json(request: Request) -> Response:  # GET /export.json
    _cap_or_404()
    hits = _result_set(request)
    return JSONResponse(
        {
            "count": len(hits),
            "row_cap": settings.kb_web_export_cap,
            "items": [h.model_dump(mode="json") for h in hits],
        }
    )


ROUTES = [
    Route("/export.csv", export_csv),
    Route("/export.json", export_json),
]

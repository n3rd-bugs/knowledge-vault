"""kbweb.views_jobs — read-only worker-job status viewer (owner-cap, Inv. 11).

GET /jobs renders deep-search job STATUS ROWS via the new parameterized read
``kbcore.jobs.list_deep_search_jobs`` plus the latest consolidation request via
the existing poll read ``kbcore.jobs.latest_consolidation_status`` (WEBUI
TIERED_ACCESS_PLAN §5 Phase 3). OWNER-ONLY at launch: worker jobs run under
implicit OWNER scope and ``deep_search_jobs.result`` carries whole-KB
synthesis, so the zone is walled with the OWNER_ONLY sentinel in
``kbweb.server.ROUTE_CAPS`` — a non-owner gets the router's byte-identical
unknown-path 404 before routing ever happens. Status fields ONLY are rendered:
the stored report text never travels through this module (the SELECT does not
even fetch it) — the linked report item is read through the scope-gated
``/item/{id}`` path. Pure reads; Invariant 11 holds.
"""

from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from kbcore import scope
from kbcore.jobs import latest_consolidation_status, list_deep_search_jobs
from kbweb.ui import templates


def _owner_or_404() -> None:
    """Defense-in-depth under the ROUTE_CAPS wall (the views_admin layering):
    if middleware ordering ever regresses, a non-owner still sees the default
    handler's byte-identical unknown-path 404 — never a 400/403 that would
    disclose the zone exists."""
    try:
        principal = scope.current()
    except RuntimeError:  # unset principal under server mode — never assume owner
        raise HTTPException(status_code=404) from None
    if not principal.is_owner:
        raise HTTPException(status_code=404)


def jobs_view(request: Request) -> Response:  # GET /jobs
    """Deep-search + consolidation status. Field-by-field rendering only.

    The consolidation row's ``result`` payload is STRIPPED here (enumerated
    known-safe fields only — the same discipline as the /review projection):
    the template can then never leak a payload key it was not written for.
    """
    _owner_or_404()
    consolidation = latest_consolidation_status()
    if consolidation is not None:
        consolidation = {k: v for k, v in consolidation.items() if k != "result"}
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {"jobs": list_deep_search_jobs(limit=20), "consolidation": consolidation},
    )


ROUTES = [
    Route("/jobs", jobs_view),
]

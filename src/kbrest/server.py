"""kbrest.server — Phase 6a REST facade over kbcore (FastAPI, PLAN.md §10/§6).

A thin HTTP surface for non-MCP agents. Reuses kb-mcp's BearerAuthMiddleware and
the shared _validate_runtime_config() startup guard, so auth and bind rules are
identical to the MCP server. Routes call kbcore only (Invariant 2); route
handlers are sync (`def`) so FastAPI runs them in a threadpool — blocking psycopg
never stalls the event loop. Domain errors become clean JSON with no stack trace.
Run: ``python -m kbrest.server``.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from kbcore import (
    compress,
    graph,
    ingest,
    ingest_queue,
    items,
    jobs,
    review,
    search,
    stats,
    supersede,
)
from kbcore.config import settings
from kbcore.errors import (
    DuplicateContent,
    EmbedderUnavailable,
    ExtractorUnavailable,
    InvalidOperation,
    KBError,
    NotFound,
)
from kbcore.models import ItemDetail, ProjectInfo, ReviewEntry, SearchHit, SimilarHit
from kbmcp.server import BearerAuthMiddleware, _validate_runtime_config
from kbrest.schemas import (
    IngestSessionRequest,
    ItemCreate,
    LinkRequest,
    ReviewResolveRequest,
    SupersedeRequest,
)

log = logging.getLogger("kbrest")


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": msg})


def create_app() -> FastAPI:
    """The bare FastAPI app (no auth wrapper) — build_app() adds the bearer."""
    app = FastAPI(
        title="Knowledge Vault REST",
        version="0.1.0",
        description="Bearer-authenticated REST facade over kbcore (read + write).",
    )

    # Domain errors → clean HTTP codes; unexpected errors → generic 500. Never a
    # stack trace in the body. Starlette matches the most specific handler via
    # the exception's MRO, so subclasses of KBError hit their own handler first.
    app.add_exception_handler(NotFound, lambda r, e: _err(404, str(e)))
    app.add_exception_handler(DuplicateContent, lambda r, e: _err(409, str(e)))
    app.add_exception_handler(InvalidOperation, lambda r, e: _err(400, str(e)))
    app.add_exception_handler(ValueError, lambda r, e: _err(400, str(e)))
    app.add_exception_handler(EmbedderUnavailable, lambda r, e: _err(503, str(e)))
    app.add_exception_handler(ExtractorUnavailable, lambda r, e: _err(503, str(e)))
    app.add_exception_handler(KBError, lambda r, e: _err(500, str(e)))
    app.add_exception_handler(Exception, lambda r, e: _err(500, "internal server error"))

    _register_read(app)
    _register_write(app)
    return app


def _register_read(app: FastAPI) -> None:
    @app.get("/api/search")
    def api_search(
        q: str,
        project: str | None = None,
        domain: str | None = None,
        tags: list[str] | None = Query(None),
        type: str | None = None,
        as_of: str | None = None,
        k: int = 8,
    ) -> list[SearchHit]:
        as_of_dt = items._parse_instant(as_of, "as_of") if as_of else None
        return search.search(
            q,
            project=project,
            domain=domain,
            type=type,
            tags=tags,
            as_of=as_of_dt,
            k=k,
            tool="rest.search",
        )

    @app.get("/api/items/{item_id}")
    def api_get_item(item_id: int, with_relations: bool = True) -> ItemDetail:
        return items.get_item(item_id, with_relations=with_relations)

    @app.get("/api/projects")
    def api_projects() -> list[ProjectInfo]:
        return items.list_projects()

    @app.get("/api/timeline")
    def api_timeline(
        project: str,
        from_: str | None = Query(None, alias="from"),
        to: str | None = None,
        limit: int = 50,
    ) -> list[ItemDetail]:
        return items.timeline(project, from_date=from_, to_date=to, limit=limit)

    @app.get("/api/similar/{item_id}")
    def api_similar(item_id: int, k: int = 5) -> list[SimilarHit]:
        return graph.similar(item_id, k=k)

    @app.get("/api/trace/{node_id}")
    def api_trace(node_id: int, kind: str = "item", depth: int = 2) -> dict:
        return graph.trace(kind, node_id, depth=depth)

    @app.get("/api/stats")
    def api_stats() -> dict:
        return stats.stats()

    @app.get("/api/review")
    def api_review() -> list[ReviewEntry]:
        return review.list_open_review()


def _register_write(app: FastAPI) -> None:
    @app.post("/api/ingest-session", status_code=202)
    def api_ingest_session(payload: IngestSessionRequest) -> dict:
        # Scrub + store NOW (no GPU); the 14B extracts in the nightly drain.
        queue_id = ingest_queue.enqueue(
            payload.project,
            compress.resolve_transcript(payload.transcript, payload.transcript_gz),
            kind=payload.kind,
            repo=payload.repo,
            branch=payload.branch,
            commit=payload.commit,
        )
        return {"queue_id": queue_id, "duplicate": queue_id is None}

    @app.post("/api/items", status_code=201)
    def api_create_item(payload: ItemCreate) -> dict:
        code_ref = payload.code_ref.model_dump(exclude_none=True) if payload.code_ref else None
        item_id = ingest.ingest(
            payload.project,
            payload.type,
            payload.title,
            payload.body,
            kind="chat",
            trust="verified",
            tags=payload.tags,
            code_ref=code_ref,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
        )
        if item_id is None:  # content-hash duplicate — point at the existing item
            existing = ingest.find_duplicate(payload.title, payload.body)
            return {"item_id": existing, "duplicate": True}
        return {"item_id": item_id, "duplicate": False}

    @app.post("/api/items/{item_id}/supersede")
    def api_supersede(item_id: int, payload: SupersedeRequest) -> dict:
        new_id = supersede.supersede(
            item_id,
            new_item_id=payload.new_item_id,
            new_body=payload.new_body,
            reason=payload.reason,
        )
        return {"old_item_id": item_id, "new_item_id": new_id}

    @app.post("/api/links", status_code=201)
    def api_link(payload: LinkRequest) -> dict:
        rel_id = graph.link(
            payload.src_item_id,
            payload.rel,
            payload.dst_item_id,
            evidence_item=payload.evidence_item_id,
        )
        return {"relation_id": rel_id}

    @app.post("/api/consolidate", status_code=202)
    def api_consolidate() -> dict:
        # Enqueue only — the host worker (GPU) runs it (Invariant 8). Deduped
        # against an in-flight run so repeated clicks don't stack.
        req_id, created = jobs.request_consolidation("rest")
        return {
            "request_id": req_id,
            "created": created,
            "status": jobs.latest_consolidation_status(),
        }

    @app.get("/api/consolidate")
    def api_consolidate_status() -> dict:
        return {"status": jobs.latest_consolidation_status()}

    @app.post("/api/review/{queue_id}/resolve")
    def api_review_resolve(queue_id: int, payload: ReviewResolveRequest) -> dict:
        action = payload.action
        if action == "promote":
            return {"item_id": review.promote(queue_id), "action": action}
        if action == "reject":
            return {"item_id": review.reject(queue_id), "action": action}
        if action == "supersede":
            if payload.new_item_id is None:
                raise ValueError("action='supersede' requires new_item_id")
            old = review.resolve_via_supersede(queue_id, payload.new_item_id)
            return {"item_id": old, "new_item_id": payload.new_item_id, "action": action}
        raise ValueError(f"unknown action {action!r}; one of promote|reject|supersede")


def build_app():
    """FastAPI app wrapped in the bearer middleware, after the startup guard.

    CORS is layered OUTSIDE the bearer check so the read-only web UI (a different
    origin) can call the write endpoints — notably POST /api/consolidate for the
    "run now" button — with its bearer. Preflight OPTIONS carry no auth, so CORS
    must answer them before the bearer middleware sees them. Origin is pinned to
    the configured web UI host (never ``*``).
    """
    _validate_runtime_config()
    app = BearerAuthMiddleware(create_app())
    origin = settings.kb_web_public_url.rstrip("/")
    if origin:
        app = CORSMiddleware(
            app,
            allow_origins=[origin],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        )
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log.info("kb-rest starting on %s:%s", settings.kb_bind, settings.kb_rest_port)
    uvicorn.run(build_app(), host=settings.kb_bind, port=settings.kb_rest_port, log_level="info")


if __name__ == "__main__":
    main()

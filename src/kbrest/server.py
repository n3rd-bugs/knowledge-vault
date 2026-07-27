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

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from kbcore import (
    compress,
    enrolment,
    graph,
    ingest,
    ingest_queue,
    items,
    jobs,
    review,
    scope,
    search,
    search_expand,
    stats,
    supersede,
)
from kbcore.config import settings
from kbcore.errors import (
    DuplicateContent,
    EmbedderUnavailable,
    ExtractionFailed,
    ExtractorUnavailable,
    InvalidOperation,
    KBError,
    NotFound,
)
from kbcore.models import ItemDetail, ProjectInfo, ReviewEntry, SearchHit, SimilarHit
from kbmcp import principal_ctx
from kbmcp.server import BearerAuthMiddleware, _validate_runtime_config
from kbrest.schemas import (
    EnrolRequest,
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
    # ExtractionFailed → 503, NOT 422: on the REST facade this only ever comes from
    # a synchronous embed under load (a busy-GPU read timeout on /api/search or
    # /api/items) — a retryable server condition. 422 wrongly tells clients the
    # request payload is invalid and not to retry. The attempts-cap "content
    # failure" meaning of ExtractionFailed is a queue-DRAIN concept and never
    # surfaces here — REST defers extraction to the nightly drain (finding).
    app.add_exception_handler(ExtractionFailed, lambda r, e: _err(503, str(e)))
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
        expand: bool | None = None,
        k: int = 8,
    ) -> list[SearchHit]:
        # expand unset → server default; true → force the top hits' linked
        # neighbors in (WP2); false → force expansion OFF.
        as_of_dt = items.parse_instant(as_of, "as_of") if as_of else None
        return search.search(
            q,
            project=project,
            domain=domain,
            type=type,
            tags=tags,
            as_of=as_of_dt,
            hops=search_expand.hops_from_expand(expand),
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
        # Owner-only (v1): review detail JSONB can carry cross-project titles/ids.
        scope.require_owner("the review queue")
        return review.list_open_review()


def _register_write(app: FastAPI) -> None:
    @app.post("/api/ingest-session", status_code=202)
    def api_ingest_session(payload: IngestSessionRequest) -> dict:
        # Scrub + store NOW (no GPU); the 14B extracts in the nightly drain.
        res = ingest_queue.enqueue(
            payload.project,
            compress.resolve_transcript(payload.transcript, payload.transcript_gz),
            kind=payload.kind,
            repo=payload.repo,
            branch=payload.branch,
            commit=payload.commit,
        )
        return {
            "queue_id": res.id,
            "duplicate": res.id is None,
            "requeued": res.requeued,
            "permanently_failed": res.permanently_failed,
        }

    @app.post("/api/enrol")
    def api_enrol(payload: EnrolRequest, request: Request) -> dict:
        """Exchange a one-time enrolment code for this machine's credential.

        This is the ONLY route an enrolment code can usefully reach. The code
        authenticated like any other bearer (so the SEC-1 check is untouched and
        no path is exempt), but it resolves to an identity with no projects and
        no capabilities — every other route therefore sees a caller that can
        read nothing and write nothing.

        Redemption is single-use and atomic in kbcore; unknown, expired and
        already-redeemed codes are refused identically.
        """
        current = scope.current()
        if current.name != principal_ctx.ENROLLING_NAME:
            # A real bearer does not need to enrol — and must not be able to use
            # this route to mint a second credential for itself.
            raise HTTPException(status_code=404, detail="not found")
        raw = request.headers.get("authorization", "")
        code = raw[7:].strip() if raw[:7].lower() == "bearer " else raw.strip()
        client = request.client.host if request.client else None
        cred = enrolment.redeem(code, machine=payload.machine, from_addr=client)
        return {
            "token": cred.token,
            "is_owner": cred.is_owner,
            "principal": cred.principal_name,
            "expires_at": cred.expires_at.isoformat() if cred.expires_at else None,
        }

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
            existing = ingest.find_duplicate(
                payload.title,
                payload.body,
                valid_from=payload.valid_from,
                valid_to=payload.valid_to,
            )
            return {"item_id": existing, "duplicate": True}
        return {"item_id": item_id, "duplicate": False}

    @app.post("/api/items/{item_id}/supersede")
    def api_supersede(item_id: int, payload: SupersedeRequest) -> dict:
        new_id = supersede.supersede(
            item_id,
            new_item_id=payload.new_item_id,
            new_body=payload.new_body,
            reason=payload.reason,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
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
        # against an in-flight run so repeated clicks don't stack. Owner-only:
        # a grantee cannot spend the owner's GPU on a whole-KB run.
        scope.require_owner("consolidation")
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
        scope.require_owner("the review queue")  # owner-only (v1), see GET /api/review
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
    elif settings.kb_public_only or settings.kb_oauth_enabled:
        # kb-rest NEEDS this origin functionally (its only CORS pin), so an empty
        # value on a public-facing instance silently drops the header and the web
        # UI's cross-origin writes (POST /api/consolidate "run now") break with no
        # server-side error. It cannot hard-fail (local no-web deploys legitimately
        # leave it unset), so warn loudly instead (review finding).
        log.warning(
            "KB_WEB_PUBLIC_URL is empty on a public-facing kb-rest instance: no CORS "
            "allow-origin will be sent, so the web UI's cross-origin writes "
            "(e.g. POST /api/consolidate) will be blocked by the browser. "
            "Set KB_WEB_PUBLIC_URL to the web UI's public origin."
        )
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # Fail-closed on the served process (see kbmcp.server.main): a request without a
    # resolved principal RAISES rather than defaulting to OWNER. The shared
    # BearerAuthMiddleware sets it; anyio copies the ContextVar into the sync-route
    # threadpool.
    scope.enter_server_mode()
    log.info("kb-rest starting on %s:%s", settings.kb_bind, settings.kb_rest_port)
    uvicorn.run(build_app(), host=settings.kb_bind, port=settings.kb_rest_port, log_level="info")


if __name__ == "__main__":
    main()

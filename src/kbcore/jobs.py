"""kbcore.jobs — on-demand worker job control tables (consolidation + deep-search).

A web button / MCP tool / CLI enqueues a 'pending' request; the host worker
(the only process with the GPU) claims it atomically and runs the job, recording
the result. The trigger surfaces are thus decoupled from execution: nothing here
runs the extractor (Invariant 8) — it only records intent. All SQL lives here
(Invariant 2). Two independent queues share this module: consolidation_requests
and deep_search_jobs.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation

_ACTIVE = ("pending", "running")


def request_consolidation(requested_by: str = "unknown") -> tuple[int, bool]:
    """Enqueue a run request, deduping against an already pending/running one.

    Returns ``(request_id, created)``. If a run is already pending or running,
    that row's id is returned with ``created=False`` — button spam can never
    stack multiple runs.
    """
    with transaction() as conn:
        existing = conn.execute(
            "SELECT id FROM consolidation_requests WHERE status = ANY(%s) ORDER BY id LIMIT 1",
            (list(_ACTIVE),),
        ).fetchone()
        if existing:
            return existing[0], False
        row = conn.execute(
            "INSERT INTO consolidation_requests (requested_by) VALUES (%s) RETURNING id",
            (requested_by,),
        ).fetchone()
    return row[0], True


def claim_pending_consolidation() -> int | None:
    """Atomically claim the oldest pending request (→ 'running'). Worker-only.

    ``FOR UPDATE SKIP LOCKED`` so two workers never double-claim the same row.
    Returns the claimed id, or None when nothing is pending.
    """
    with transaction() as conn:
        row = conn.execute(
            """
            UPDATE consolidation_requests SET status = 'running', started_at = now()
            WHERE id = (
                SELECT id FROM consolidation_requests WHERE status = 'pending'
                ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
            )
            RETURNING id
            """
        ).fetchone()
    return row[0] if row else None


def finish_consolidation(request_id: int, result: dict[str, Any], ok: bool = True) -> None:
    """Record a claimed request's outcome (status flip — never deleted)."""
    with transaction() as conn:
        conn.execute(
            "UPDATE consolidation_requests SET status = %s, finished_at = now(), result = %s "
            "WHERE id = %s",
            ("done" if ok else "failed", Jsonb(result), request_id),
        )


def latest_consolidation_status() -> dict[str, Any] | None:
    """The most recent request's status, for the button/CLI/tool to poll."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id, requested_by, status, requested_at, started_at, finished_at, result "
            "FROM consolidation_requests ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "requested_by": row[1],
        "status": row[2],
        "requested_at": row[3].isoformat() if row[3] else None,
        "started_at": row[4].isoformat() if row[4] else None,
        "finished_at": row[5].isoformat() if row[5] else None,
        "result": row[6],
    }


# --- deep-search jobs (async kb_deep_search — survives the mcp-proxy timeout) ---


def enqueue_deep_search(
    question: str,
    project: str | None = None,
    save: bool = True,
    requested_by: str = "unknown",
) -> int:
    """Enqueue a 'pending' deep-search job; the host worker runs it. Returns the id.

    Each call is a distinct job (deep-search is question-specific and cheap to
    re-ask); unlike consolidation there is no in-flight dedupe. Rejects an empty
    question up front so a doomed job is never queued.
    """
    if not question or not question.strip():
        raise InvalidOperation("deep search needs a non-empty question")
    params = {"project": project, "save": save}
    with transaction() as conn:
        row = conn.execute(
            "INSERT INTO deep_search_jobs (question, params, requested_by) "
            "VALUES (%s, %s, %s) RETURNING id",
            (question, Jsonb(params), requested_by),
        ).fetchone()
    return row[0]


def claim_pending_deep_search() -> dict[str, Any] | None:
    """Atomically claim the oldest pending deep-search job (→ 'running'). Worker-only.

    ``FOR UPDATE SKIP LOCKED`` so two workers never double-claim. Returns
    ``{id, question, params}``, or None when nothing is pending.
    """
    with transaction() as conn:
        row = conn.execute(
            """
            UPDATE deep_search_jobs SET status = 'running', started_at = now()
            WHERE id = (
                SELECT id FROM deep_search_jobs WHERE status = 'pending'
                ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
            )
            RETURNING id, question, params
            """
        ).fetchone()
    if row is None:
        return None
    return {"id": row[0], "question": row[1], "params": row[2] if isinstance(row[2], dict) else {}}


def finish_deep_search(job_id: int, result: dict[str, Any], ok: bool = True) -> None:
    """Record a claimed deep-search job's outcome (status flip — never deleted).

    ``result`` holds the saved report item id + text (or an ``error`` on failure).
    """
    with transaction() as conn:
        conn.execute(
            "UPDATE deep_search_jobs SET status = %s, finished_at = now(), result = %s "
            "WHERE id = %s",
            ("done" if ok else "failed", Jsonb(result), job_id),
        )


def list_deep_search_jobs(limit: int = 20) -> list[dict[str, Any]]:
    """Recent deep-search jobs — STATUS FIELDS ONLY, never the stored report text.

    Read surface for the owner-only web /jobs viewer (WEBUI_TIERED_ACCESS_PLAN
    §5 Phase 3): worker jobs run under implicit OWNER scope and ``result``
    carries whole-KB synthesis, so this SELECT deliberately extracts only the
    saved report's ITEM ID (``result->>'item_id'``) — a UI links through the
    scope-gated ``/item/{id}`` read path and the report BODY never travels via
    this function. Parameterized SELECT, newest first; ``limit`` is clamped so
    a caller can never turn the list into a bulk dump.
    """
    lim = max(1, min(int(limit), 100))
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, question, status, requested_by, requested_at, started_at, "
            "finished_at, (result->>'item_id')::bigint "
            "FROM deep_search_jobs ORDER BY id DESC LIMIT %s",
            (lim,),
        ).fetchall()
    return [
        {
            "id": r[0],
            "question": r[1],
            "status": r[2],
            "requested_by": r[3],
            "requested_at": r[4].isoformat() if r[4] else None,
            "started_at": r[5].isoformat() if r[5] else None,
            "finished_at": r[6].isoformat() if r[6] else None,
            "report_item_id": r[7],
        }
        for r in rows
    ]


def deep_search_status(job_id: int) -> dict[str, Any] | None:
    """A deep-search job's current state + stored result, for kb_deep_search_status."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id, question, status, requested_at, started_at, finished_at, result "
            "FROM deep_search_jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "question": row[1],
        "status": row[2],
        "requested_at": row[3].isoformat() if row[3] else None,
        "started_at": row[4].isoformat() if row[4] else None,
        "finished_at": row[5].isoformat() if row[5] else None,
        "result": row[6],
    }

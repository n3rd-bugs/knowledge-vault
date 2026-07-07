"""kbcore.jobs — on-demand consolidation trigger (control table).

A web button / MCP tool / CLI enqueues a 'pending' request; the host worker
(the only process with the GPU) claims it atomically and runs
``consolidate.main()``, recording the result. The trigger surfaces are thus
decoupled from execution: nothing here runs the extractor (Invariant 8) — it
only records intent. All SQL lives here (Invariant 2).
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from kbcore.db import connect, transaction

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

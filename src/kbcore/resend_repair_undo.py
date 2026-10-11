"""kbcore.resend_repair_undo — the undo trail of ``kb repair resend-dups``.

Split from the rows/items steps (ratchet). ``--undo`` reverses EXACTLY what a
previous apply marked — the ``meta.repair`` marker (``by = 'resend-dups'``) on
queue rows and on items — and deletes nothing (Inv 5):

* rows ``'duplicate'`` -> their ``prev_status`` / ``prev_error`` (``pending``, or the
  parked ``failed`` shape), marker dropped; ``duplicate_of`` / ``chunk_key`` stay —
  they are facts about the row, not verdicts, and they are what lets a redo pair
  the row again;
* items ``'archived'`` -> ``'active'``, ``meta.archived`` + ``meta.repair`` dropped,
  the review rows the apply resolved re-opened.

``since`` under undo is the MARKER time (``meta.repair.at``) on BOTH sides — the
run being reversed, not the rows' enqueue time.

The tier-3 REPLAY (:mod:`kbcore.resend_repair_replay`) marks with ``meta.repair.mode
= 'replay'``: its rows keep their status (the marker is audit only) and its items
carry the re-send instant in ``meta.repair.since``. The plain undo above never
touches a replay marker (a replay row has no ``prev_status`` — restoring one would
re-arm it as ``'pending'``); the ``replay_*`` functions below reverse exactly one
replay run, keyed by ``(session uri, since)`` — the selector the apply was run with.
"""

from __future__ import annotations

from datetime import UTC, datetime

from kbcore.db import connect, transaction
from kbcore.resend_repair import REPAIR_BY, SESSION_ROWS_SQL, RepairSelector, selector_sql

REPLAY_MODE = "replay"
_NOT_REPLAY = "{col}meta->'repair'->>'mode' IS DISTINCT FROM 'replay'"


def row_candidates(sel: RepairSelector) -> list[int]:
    where, params = selector_sql(sel.model_copy(update={"since": None}))
    if sel.since:
        where += " AND (meta->'repair'->>'at')::timestamptz >= %(since)s"
        params["since"] = sel.since
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id FROM ingest_queue
             WHERE status = 'duplicate' AND meta->'repair'->>'by' = %(by)s AND ({where})
               AND {_NOT_REPLAY.format(col="")}
             ORDER BY id
            """,  # noqa: S608 — parameterised selector fragment + constant
            {**params, "by": REPAIR_BY},
        ).fetchall()
    return [int(r[0]) for r in rows]


def undo_rows(ids: list[int]) -> int:
    """Restore the marked rows' previous status/error and drop the marker."""
    if not ids:
        return 0
    with transaction() as conn:
        cur = conn.execute(
            """
            UPDATE ingest_queue
               SET status = coalesce(meta->'repair'->>'prev_status', 'pending'),
                   error = meta->'repair'->>'prev_error',
                   meta = meta - 'repair'
             WHERE id = ANY(%(ids)s::bigint[]) AND status = 'duplicate'
               AND meta->'repair'->>'by' = %(by)s
               AND meta->'repair'->>'mode' IS DISTINCT FROM 'replay'
            """,
            {"ids": list(ids), "by": REPAIR_BY},
        )
    return cur.rowcount or 0


def item_candidates(sel: RepairSelector) -> list[int]:
    """Items a previous apply archived, reached by the same selector (project / session
    prefix / uri map onto the item's source; ``since`` onto the marker's time; a
    rows file onto the groups of the listed rows)."""
    parts = [
        "i.status = 'archived'",
        "i.meta->'repair'->>'by' = %(by)s",
        _NOT_REPLAY.format(col="i."),
    ]
    params: dict[str, object] = {"by": REPAIR_BY}
    if sel.session:
        parts.append("starts_with(s.uri, %(sess)s)")
        params["sess"] = "session:" + sel.session.strip()
    if sel.project:
        parts.append("i.project = %(project)s")
        params["project"] = sel.project
    if sel.uri:
        parts.append("s.uri = %(uri)s")
        params["uri"] = sel.uri.strip()
    if sel.since:
        parts.append("(i.meta->'repair'->>'at')::timestamptz >= %(since)s")
        params["since"] = sel.since
    if sel.row_ids is not None:
        where, rp = selector_sql(RepairSelector(row_ids=sel.row_ids), "q")
        parts.append(
            "(i.project, s.uri) IN (SELECT q.project, btrim(q.meta->>'source_uri') "
            f"FROM ingest_queue q WHERE {where})"
        )
        params.update(rp)
    with connect() as conn:
        rows = conn.execute(
            "SELECT i.id FROM items i JOIN sources s ON s.id = i.source_id WHERE "
            + " AND ".join(parts)
            + " ORDER BY i.id",  # noqa: S608 — parts are constant fragments, values are params
            params,
        ).fetchall()
    return [int(r[0]) for r in rows]


def undo_items(ids: list[int]) -> tuple[int, int]:
    """Re-activate the marked items and re-open the review rows the apply resolved.
    Returns ``(items restored, review rows reopened)``."""
    if not ids:
        return 0, 0
    with transaction() as conn:
        rows = conn.execute(
            """
            UPDATE items
               SET status = 'active', updated_at = now(),
                   meta = items.meta - 'archived' - 'repair'
              FROM (SELECT id AS rid, meta->'repair'->'review_ids' AS review_ids
                      FROM items WHERE id = ANY(%(ids)s::bigint[])) AS old
             WHERE items.id = old.rid AND items.status = 'archived'
               AND items.meta->'repair'->>'by' = %(by)s
             RETURNING items.id, old.review_ids
            """,
            {"ids": list(ids), "by": REPAIR_BY},
        ).fetchall()
        review_ids = [int(r) for _iid, rids in rows for r in (rids or [])]
        reopened = 0
        if review_ids:
            cur = conn.execute(
                "UPDATE review_queue SET resolved = false WHERE id = ANY(%(ids)s::bigint[])",
                {"ids": review_ids},
            )
            reopened = cur.rowcount or 0
    return len(rows), reopened


# --- tier 3: the replay run (meta.repair.mode = 'replay') ---------------------


def replay_row_candidates(uri: str, since: datetime) -> list[int]:
    """Queue rows of ``uri`` a replay apply for ``since`` marked (status untouched)."""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id FROM ingest_queue
             WHERE {SESSION_ROWS_SQL} AND btrim(meta->>'source_uri') = %(uri)s
               AND meta->'repair'->>'by' = %(by)s AND meta->'repair'->>'mode' = %(mode)s
               AND (meta->'repair'->>'since')::timestamptz = %(since)s
             ORDER BY id
            """,  # noqa: S608 — SESSION_ROWS_SQL is a constant fragment
            {"uri": uri, "by": REPAIR_BY, "mode": REPLAY_MODE, "since": since},
        ).fetchall()
    return [int(r[0]) for r in rows]


def replay_item_candidates(uri: str, since: datetime) -> list[int]:
    """Items of ``uri`` (any project) a replay apply for ``since`` archived."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT i.id FROM items i JOIN sources s ON s.id = i.source_id
             WHERE s.kind = 'session' AND s.uri = %(uri)s AND i.status = 'archived'
               AND i.meta->'repair'->>'by' = %(by)s AND i.meta->'repair'->>'mode' = %(mode)s
               AND (i.meta->'repair'->>'since')::timestamptz = %(since)s
             ORDER BY i.id
            """,
            {"uri": uri, "by": REPAIR_BY, "mode": REPLAY_MODE, "since": since},
        ).fetchall()
    return [int(r[0]) for r in rows]


def undo_replay_rows(ids: list[int]) -> int:
    """Drop the replay marker from the rows — their status was never changed."""
    if not ids:
        return 0
    with transaction() as conn:
        cur = conn.execute(
            """
            UPDATE ingest_queue SET meta = meta - 'repair'
             WHERE id = ANY(%(ids)s::bigint[])
               AND meta->'repair'->>'by' = %(by)s AND meta->'repair'->>'mode' = %(mode)s
            """,
            {"ids": list(ids), "by": REPAIR_BY, "mode": REPLAY_MODE},
        )
    return cur.rowcount or 0


def replay_since_values(uri: str) -> list[str]:
    """The ``--since`` keys the replay markers on ``uri`` DO carry (rows and items,
    distinct, oldest first) — what a replay ``--undo`` that matched nothing names."""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT (since)::timestamptz FROM (
                SELECT meta->'repair'->>'since' AS since FROM ingest_queue
                 WHERE {SESSION_ROWS_SQL} AND btrim(meta->>'source_uri') = %(uri)s
                   AND meta->'repair'->>'by' = %(by)s AND meta->'repair'->>'mode' = %(mode)s
                UNION
                SELECT i.meta->'repair'->>'since' FROM items i JOIN sources s ON s.id = i.source_id
                 WHERE s.kind = 'session' AND s.uri = %(uri)s
                   AND i.meta->'repair'->>'by' = %(by)s AND i.meta->'repair'->>'mode' = %(mode)s
            ) m WHERE since IS NOT NULL
             ORDER BY 1
            """,  # noqa: S608 — SESSION_ROWS_SQL is a constant fragment
            {"uri": uri, "by": REPAIR_BY, "mode": REPLAY_MODE},
        ).fetchall()
    return [r[0].astimezone(UTC).isoformat() for r in rows]

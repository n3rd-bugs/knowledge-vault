"""kbcore.resend_repair_rows — step 1 of ``kb repair resend-dups``: the QUEUE rows.

Split from :mod:`kbcore.resend_repair` (ratchet). Two discovery paths feed the
same :class:`~kbcore.resend_repair.GroupPlan`:

* default — rows already marked ``duplicate_of`` (by ``--backfill-keys`` or the
  live guard's repair); only the still-``'pending'`` ones flip;
* ``--as-resend`` (brief amendment 4) — the owner's explicit assertion over a
  selection: rows that are ``'pending'`` OR ``'failed' AND error LIKE 'parked %'``
  are paired with a canonical — the row's existing ``duplicate_of`` mark when the
  backfill already stamped one (runbook order: backfill FIRST), else the best
  ``(uri, content_hash)`` match in ANY project (prefer ``'extracted'``, then
  lowest id); unpaired rows are reported and left alone. An undone row keeps its
  mark, so undo -> redo pairs it again.

``GroupPlan.dup_ids`` is the ONE duplicate set every later step reads (tier 1,
tier 2 and the archive audit stamp): the group's existing marks plus this run's
flips. Rows listed in a ``--rows-file`` that neither path can touch are reported
``keep: ...`` rather than silently dropped.

The flip is ``status='duplicate'`` + ``error`` + ``meta.repair = {at, by, reason,
prev_status, prev_error}`` — NEVER ``processed_at`` (the reaper's progress signal
and the per-day attempt boundary read it). ``'extracted'`` rows are never flipped.
The reverse lives in :mod:`kbcore.resend_repair_undo`.
"""

from __future__ import annotations

from psycopg.types.json import Jsonb

from kbcore.db import connect, transaction
from kbcore.resend_repair import (
    REPAIR_BY,
    SESSION_ROWS_SQL,
    GroupPlan,
    RepairSelector,
    now_iso,
    selector_sql,
    session_rows_sql,
)

PARKED_LIKE = "parked %"
IGNORED_MARKED = "not a marked pending duplicate"
IGNORED_AS_RESEND = "not pending/parked"
IGNORED_UNKNOWN = "unknown queue id"


def _group_totals(conn, keys: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[int, list]]:
    """``(project, uri) -> (rows, ids already marked duplicate_of)`` over ALL rows."""
    if not keys:
        return {}
    rows = conn.execute(
        f"""
        SELECT project, btrim(meta->>'source_uri') AS uri, count(*),
               array_remove(array_agg(CASE WHEN duplicate_of IS NOT NULL THEN id END), NULL)
          FROM ingest_queue
         WHERE {SESSION_ROWS_SQL}
           AND (project, btrim(meta->>'source_uri')) IN (
               SELECT p, u FROM unnest(%(ps)s::text[], %(us)s::text[]) AS t(p, u))
         GROUP BY 1, 2
        """,  # noqa: S608 — SESSION_ROWS_SQL is a constant fragment
        {"ps": [k[0] for k in keys], "us": [k[1] for k in keys]},
    ).fetchall()
    return {(p, u): (int(n), [int(i) for i in d]) for p, u, n, d in rows}


def _discover_marked(conn, sel: RepairSelector) -> tuple[list[GroupPlan], set[int]]:
    where, params = selector_sql(sel)
    rows = conn.execute(
        f"""
        SELECT project, btrim(meta->>'source_uri') AS uri,
               count(*) FILTER (WHERE status = 'pending'),
               count(*) FILTER (WHERE status = 'extracted'),
               coalesce(sum(octet_length(content)), 0),
               coalesce(sum(octet_length(content)) FILTER (WHERE status = 'pending'), 0),
               coalesce(sum(octet_length(content)) FILTER (WHERE status = 'extracted'), 0),
               array_agg(id ORDER BY id) FILTER (WHERE status = 'pending'),
               array_agg(duplicate_of ORDER BY id) FILTER (WHERE status = 'pending'),
               array_agg(DISTINCT duplicate_of),
               array_agg(id) FILTER (WHERE status = 'extracted')
          FROM ingest_queue
         WHERE {SESSION_ROWS_SQL} AND duplicate_of IS NOT NULL AND ({where})
         GROUP BY 1, 2
         ORDER BY 2, 1
        """,  # noqa: S608 — constant fragments + parameterised selector
        params,
    ).fetchall()
    plans: list[GroupPlan] = []
    seen: set[int] = set()
    for project, uri, pend, extr, nbytes, fbytes, ebytes, pend_ids, pend_canon, cids, eids in rows:
        flip = dict(zip(pend_ids or [], pend_canon or [], strict=True))
        seen |= set(flip) | {int(i) for i in (eids or [])}
        plans.append(
            GroupPlan(
                project=project,
                uri=uri,
                rows=0,
                dup=0,
                pend=int(pend),
                extr=int(extr),
                dup_bytes=int(nbytes),
                flip_bytes=int(fbytes),
                extr_bytes=int(ebytes),
                flip=flip,
                canon_ids=sorted(int(c) for c in cids),
            )
        )
    return plans, seen


def _discover_as_resend(conn, sel: RepairSelector) -> tuple[list[GroupPlan], set[int]]:
    where, params = selector_sql(sel, "q")
    rows = conn.execute(
        f"""
        SELECT q.id, q.project, btrim(q.meta->>'source_uri') AS uri, q.status,
               octet_length(q.content),
               coalesce(q.duplicate_of,
                (SELECT c.id FROM ingest_queue c
                  WHERE c.kind = 'session' AND c.content_hash = q.content_hash
                    AND btrim(c.meta->>'source_uri') = btrim(q.meta->>'source_uri')
                    AND c.duplicate_of IS NULL AND c.status <> 'duplicate'
                  ORDER BY (c.status = 'extracted') DESC, c.id
                  LIMIT 1)) AS canon
          FROM ingest_queue q
         WHERE {session_rows_sql("q")}
           AND (q.status = 'pending' OR (q.status = 'failed' AND q.error LIKE %(parked)s))
           AND ({where})
         ORDER BY uri, q.project, q.id
        """,  # noqa: S608 — constant fragments + parameterised selector
        {**params, "parked": PARKED_LIKE},
    ).fetchall()
    plans: dict[tuple[str, str], GroupPlan] = {}
    seen: set[int] = set()
    for rid, project, uri, status, nbytes, canon in rows:
        seen.add(int(rid))
        p = plans.setdefault(
            (project, uri), GroupPlan(project=project, uri=uri, rows=0, dup=0, pend=0, extr=0)
        )
        if canon is None or canon == rid:
            p.unpaired += 1
            continue
        p.flip[int(rid)] = int(canon)
        p.dup_bytes += int(nbytes)
        p.flip_bytes += int(nbytes)
        if status == "pending":
            p.pend += 1
        else:
            p.parked += 1
        if int(canon) not in p.canon_ids:
            p.canon_ids.append(int(canon))
    return list(plans.values()), seen


def discover(sel: RepairSelector) -> list[GroupPlan]:
    """The duplicate groups the selection reaches, with their row counts filled in."""
    with connect() as conn:
        plans, seen = (
            _discover_as_resend(conn, sel) if sel.as_resend else _discover_marked(conn, sel)
        )
        totals = _group_totals(conn, [(p.project, p.uri) for p in plans])
        # A group's existing duplicate_of marks also count as canonicals for the gate.
        existing = _existing_canonicals(conn, plans) if sel.as_resend else {}
        if sel.row_ids is not None:
            _report_ignored(conn, sel, plans, seen)
    for p in plans:
        n, marked = totals.get((p.project, p.uri), (0, []))
        p.rows = n
        p.dup_ids = sorted(set(marked) | set(p.flip))
        p.dup = len(p.dup_ids)
        for c in existing.get((p.project, p.uri), []):
            if c not in p.canon_ids:
                p.canon_ids.append(c)
        p.canon_ids.sort()
    return plans


def _existing_canonicals(conn, plans: list[GroupPlan]) -> dict[tuple[str, str], list[int]]:
    if not plans:
        return {}
    rows = conn.execute(
        f"""
        SELECT project, btrim(meta->>'source_uri'), array_agg(DISTINCT duplicate_of)
          FROM ingest_queue
         WHERE {SESSION_ROWS_SQL} AND duplicate_of IS NOT NULL
           AND (project, btrim(meta->>'source_uri')) IN (
               SELECT p, u FROM unnest(%(ps)s::text[], %(us)s::text[]) AS t(p, u))
         GROUP BY 1, 2
        """,  # noqa: S608 — SESSION_ROWS_SQL is a constant fragment
        {"ps": [p.project for p in plans], "us": [p.uri for p in plans]},
    ).fetchall()
    return {(p, u): [int(c) for c in cs] for p, u, cs in rows}


def _report_ignored(conn, sel: RepairSelector, plans: list[GroupPlan], seen: set[int]) -> None:
    """Listed ids the discovery could not touch -> a ``keep:`` count on their group
    (a synthetic ``-`` group for ids the queue does not hold), never silence."""
    missing = sorted(set(sel.row_ids or []) - seen)
    if not missing:
        return
    found = conn.execute(
        "SELECT id, project, coalesce(nullif(btrim(meta->>'source_uri'), ''), '-') "
        "FROM ingest_queue WHERE id = ANY(%(ids)s::bigint[])",
        {"ids": missing},
    ).fetchall()
    by_key: dict[tuple[str, str], GroupPlan] = {(p.project, p.uri): p for p in plans}
    reason = IGNORED_AS_RESEND if sel.as_resend else IGNORED_MARKED
    for _rid, project, uri in found:
        p = by_key.get((project, uri))
        if p is None:
            p = GroupPlan(project=project, uri=uri, rows=0, dup=0, pend=0, extr=0)
            by_key[(project, uri)] = p
            plans.append(p)
        p.keep[reason] = p.keep.get(reason, 0) + 1
    unknown = len(missing) - len(found)
    if unknown:
        p = by_key.get(("-", "-"))
        if p is None:
            p = GroupPlan(project="-", uri="-", rows=0, dup=0, pend=0, extr=0)
            plans.append(p)
        p.keep[IGNORED_UNKNOWN] = p.keep.get(IGNORED_UNKNOWN, 0) + unknown


def flip(p: GroupPlan, *, reason: str, as_resend: bool) -> int:
    """Flip ``p.flip`` rows to ``'duplicate'`` in ONE transaction. Returns the count.

    The WHERE re-checks the status the dry run saw, so a row drained between the
    two (now ``'extracted'``) is left alone rather than overwritten.
    """
    if not p.flip or p.reserved:
        return 0
    marker = {"at": now_iso(), "by": REPAIR_BY, "reason": reason}
    status_ok = (
        "(q.status = 'pending' OR (q.status = 'failed' AND q.error LIKE %(parked)s))"
        if as_resend
        else "q.status = 'pending'"
    )
    with transaction() as conn:
        cur = conn.execute(
            f"""
            UPDATE ingest_queue q
               SET status = 'duplicate',
                   duplicate_of = v.canon,
                   error = 'repair: resend duplicate of ' || v.canon::text,
                   meta = coalesce(q.meta, '{{}}'::jsonb) || jsonb_build_object(
                       'repair', %(marker)s::jsonb || jsonb_build_object(
                           'prev_status', q.status, 'prev_error', q.error))
              FROM unnest(%(ids)s::bigint[], %(canons)s::bigint[]) AS v(id, canon)
             WHERE q.id = v.id AND q.id <> v.canon AND {status_ok}
            """,  # noqa: S608 — status_ok is one of two constant fragments
            {
                "ids": list(p.flip),
                "canons": list(p.flip.values()),
                "marker": Jsonb(marker),
                "parked": PARKED_LIKE,
            },
        )
    return cur.rowcount or 0

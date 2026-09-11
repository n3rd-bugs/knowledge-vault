"""kbcore.resend_repair — the OWNER repair behind ``kb repair resend-dups`` (wave S, S5).

Root cause KB 33783: a resumed session re-sent its whole transcript from byte 0
under whatever project the firing-time cwd resolved to, and the only guard was
``UNIQUE (kind, project, content_hash)`` — so every resend became new rows and a
full GPU re-extraction under the wrong project. Migration 020 gave the queue a
project-less identity (``chunk_key``, ``duplicate_of``); this module puts that
identity onto the HISTORICAL rows and retires what they cost (design KB 33784,
proposal_v2 Appendix E; the wave S brief's amendments 4-5 are binding here).

Three steps, every one a column write or a STATUS FLIP (Invariant 5 — nothing is
ever deleted, ``trust`` is never touched, ``items.body`` is never touched):

* Step 0 ``backfill_keys`` — per pre-020 ``(session uri, content_hash)`` group the
  canonical (lowest id, or the row the guard already keyed) gets ``chunk_key``;
  later rows get ``duplicate_of``. Idempotent.
* Step 1 rows (:mod:`kbcore.resend_repair_rows`) — later duplicates still
  ``'pending'`` (and, under the owner's ``--as-resend`` assertion, the rows parked
  on 2026-09-10) flip to ``'duplicate'`` with ``meta.repair``; NEVER
  ``processed_at``. ``'extracted'`` duplicates keep their status.
* Step 2 items (:mod:`kbcore.resend_repair_items`) — a duplicate group's items are
  archived ONLY behind the canonical gate (every canonical row ``'extracted'`` AND
  the canonical group's ``sum(saved_count) > 0``).

``undo`` (:mod:`kbcore.resend_repair_undo`) reverses exactly what an apply marked
(the ``meta.repair`` marker on rows and items) — it deletes nothing either.
Owner-only (:func:`kbcore.scope.require_owner`); the CLI holds the consolidation
advisory lock around every write.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from kbcore import scope
from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation
from kbcore.ingest_queue import RESERVED_PROJECTS
from kbcore.queue_groups import _DEGENERATE_URI_SQL, normalise_source_uri
from kbcore.queue_guard import chunk_key
from kbcore.resend_repair_models import (  # noqa: F401 — re-exported I/O shapes
    BackfillPlan,
    GroupPlan,
    RepairSelector,
    UndoPlan,
)

logger = logging.getLogger("kbcore.resend_repair")

REPAIR_BY = "resend-dups"
ARCHIVE_REASON = "repair:resend-duplicate"


def session_rows_sql(alias: str = "") -> str:
    """The rows this repair may ever look at: session chunks with a real session
    identity. ``_DEGENERATE_URI_SQL`` is queue_groups' constant fragment (it spells
    an unaliased ``meta``, re-pointed at ``alias`` here)."""
    col = f"{alias}." if alias else ""
    degenerate = _DEGENERATE_URI_SQL.replace("meta->>", f"{col}meta->>")
    return (
        f"{col}kind = 'session' AND NOT ({degenerate})"
        f" AND starts_with(btrim({col}meta->>'source_uri'), 'session:')"
    )


SESSION_ROWS_SQL = session_rows_sql()


def require_selector(sel: RepairSelector) -> None:
    if not sel.is_set():
        raise InvalidOperation(
            "resend-dups needs a selector (--session, --project, --since, --all, --uri or "
            "--rows-file) — refusing to walk the whole queue implicitly"
        )
    if sel.uri is not None and normalise_source_uri(sel.uri) is None:
        raise InvalidOperation(f"--uri {sel.uri!r} is not a session identity")
    if sel.as_resend and sel.all:
        raise InvalidOperation(
            "--as-resend is an assertion over a BOUNDED selection "
            "(--rows-file, --uri, --session, --project/--since) — not --all"
        )


def selector_sql(sel: RepairSelector, alias: str = "") -> tuple[str, dict[str, Any]]:
    """WHERE fragment (+params) over ``ingest_queue`` for the owner's selection.

    Every predicate is parameterised; the session prefix uses ``starts_with`` so
    no LIKE wildcard in a sid can widen the match.
    """
    col = f"{alias}." if alias else ""
    uri_col = f"btrim({col}meta->>'source_uri')"
    parts: list[str] = []
    params: dict[str, Any] = {}
    if sel.session:
        parts.append(f"starts_with({uri_col}, %(sel_session)s)")
        params["sel_session"] = "session:" + sel.session.strip()
    if sel.project:
        parts.append(f"{col}project = %(sel_project)s")
        params["sel_project"] = sel.project
    if sel.since:
        parts.append(f"{col}created_at >= %(sel_since)s")
        params["sel_since"] = sel.since
    if sel.uri:
        parts.append(f"{uri_col} = %(sel_uri)s")
        params["sel_uri"] = normalise_source_uri(sel.uri)
    if sel.row_ids is not None:
        parts.append(f"{col}id = ANY(%(sel_ids)s::bigint[])")
        params["sel_ids"] = list(sel.row_ids)
    return (" AND ".join(parts) if parts else "TRUE"), params


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def gpu_nights(nbytes: int) -> float:
    """Nightly-drain nights ``nbytes`` of chunks cost, at KB_REPAIR_MB_PER_GPU_NIGHT."""
    per_night = max(1, int(settings.kb_repair_mb_per_gpu_night))
    return nbytes / 1_000_000 / per_night


# --- step 0: backfill keys ----------------------------------------------------


def backfill_keys(*, dry_run: bool = True) -> BackfillPlan:
    """Key every pre-020 ``(session uri, content_hash)`` group (column writes only).

    Canonical = the row the guard already keyed, else the lowest id AMONG THE ROWS
    THAT ARE NOT THEMSELVES DUPLICATES (``duplicate_of IS NULL`` and status not
    ``'duplicate'``): it gets ``chunk_key``; the others get ``duplicate_of``.
    Single-row groups get their key too (that is what makes a later resend of the
    E4 backlog idempotent against the lattice). A row already marked keeps its
    mark, and a group whose keyed row is itself a duplicate (a broken state) is
    skipped — the backfill can never mint a ``1 -> 2 -> 1`` cycle after an
    ``--as-resend`` flip. Idempotent: a second run plans nothing. No status touched.
    """
    scope.require_owner("resend repair")
    with connect() as conn:
        groups = conn.execute(
            f"""
            SELECT btrim(meta->>'source_uri') AS uri, content_hash,
                   array_agg(id ORDER BY id), array_agg(chunk_key ORDER BY id),
                   array_agg(duplicate_of ORDER BY id), array_agg(status ORDER BY id)
              FROM ingest_queue
             WHERE {SESSION_ROWS_SQL}
             GROUP BY 1, 2
            """  # noqa: S608 — SESSION_ROWS_SQL is a constant fragment
        ).fetchall()
    plan = BackfillPlan(groups=len(groups))
    for uri, content_hash, ids, keys, dups, statuses in groups:
        rows = list(zip(ids, keys, dups, statuses, strict=True))
        cands = [i for i, _k, d, st in rows if d is None and st != "duplicate"]
        keyed = [i for i, k, _d, _st in rows if k]
        if not cands or (keyed and keyed[0] not in cands):
            plan.skipped += 1  # every row already marked, or the keyed row is a duplicate
            continue
        canonical = keyed[0] if keyed else cands[0]
        if not keyed:
            plan.to_key[canonical] = chunk_key(uri, content_hash)
        for i in cands:
            if i != canonical:
                plan.to_mark[i] = canonical
    if dry_run:
        return plan
    with transaction() as conn:
        for rid, key in plan.to_key.items():
            cur = conn.execute(
                "UPDATE ingest_queue SET chunk_key = %s WHERE id = %s AND chunk_key IS NULL",
                (key, rid),
            )
            plan.keyed += cur.rowcount or 0
        for rid, canonical in plan.to_mark.items():
            cur = conn.execute(
                "UPDATE ingest_queue SET duplicate_of = %s WHERE id = %s AND duplicate_of IS NULL",
                (canonical, rid),
            )
            plan.marked += cur.rowcount or 0
    return plan


# --- steps 1 + 2: plan / apply / undo ----------------------------------------


def plan(sel: RepairSelector) -> list[GroupPlan]:
    """Dry run: one :class:`GroupPlan` per duplicate group. Writes nothing.

    ``limit`` is applied BEFORE the per-group item planning so it bounds the work,
    not just the output. Under ``rows_only`` the canonical gate is still read (the
    ``canon`` column must be honest on the run that decides tonight's GPU spend);
    only the item candidate search is skipped.
    """
    from kbcore import resend_repair_items as items_step
    from kbcore import resend_repair_rows as rows_step

    scope.require_owner("resend repair")
    require_selector(sel)
    plans = rows_step.discover(sel)
    if sel.limit is not None:
        plans = plans[: max(0, sel.limit)]
    for p in plans:
        p.reserved = p.project in RESERVED_PROJECTS
        items_step.plan_items(p, attribute_quotes=sel.attribute_quotes, rows_only=sel.rows_only)
    return plans


def apply(plans: list[GroupPlan], sel: RepairSelector) -> list[GroupPlan]:
    """Perform the flips the dry run listed — one transaction per group per step.
    Every group's outcome is logged (``--as-resend`` is an assertion; its reason
    lands in the log as well as in ``meta.repair``)."""
    from kbcore import resend_repair_items as items_step
    from kbcore import resend_repair_rows as rows_step

    scope.require_owner("resend repair")
    reason = "owner assertion --as-resend" if sel.as_resend else "resend duplicate"
    for p in plans:
        if not p.eligible:
            continue
        p.flipped = rows_step.flip(p, reason=reason, as_resend=sel.as_resend)
        if p.archive_ids:
            p.archived = items_step.archive_group(p)
        logger.info(
            "resend-dups %s/%s: flipped %d row(s), archived %d item(s) (as_resend=%s, reason=%r)",
            p.project,
            p.session,
            p.flipped,
            p.archived,
            sel.as_resend,
            reason,
        )
    return plans


def undo(sel: RepairSelector, *, dry_run: bool = True) -> UndoPlan:
    """Reverse exactly the rows/items a previous apply marked (``meta.repair``).
    Under undo ``since`` means the MARKER time (``meta.repair.at``) for rows and
    items alike — the run being reversed, not the rows' enqueue time."""
    from kbcore import resend_repair_undo as undo_step

    scope.require_owner("resend repair")
    require_selector(sel)
    plan_ = UndoPlan(row_ids=undo_step.row_candidates(sel), item_ids=undo_step.item_candidates(sel))
    if dry_run:
        return plan_
    plan_.rows_restored = undo_step.undo_rows(plan_.row_ids)
    plan_.items_restored, plan_.reviews_reopened = undo_step.undo_items(plan_.item_ids)
    return plan_


def duplicate_inventory() -> dict[str, Any]:
    """Read-only: rows/bytes by status among ``duplicate_of IS NOT NULL`` rows, plus the
    still-unkeyed pre-020 estimate (session groups with a NULL-keyed lowest row)."""
    with connect() as conn:
        by_status = conn.execute(
            "SELECT status, count(*), coalesce(sum(octet_length(content)), 0) FROM ingest_queue "
            "WHERE duplicate_of IS NOT NULL GROUP BY status ORDER BY status"
        ).fetchall()
        unkeyed = conn.execute(
            f"""
            SELECT count(*) FROM (
                SELECT 1 FROM ingest_queue WHERE {SESSION_ROWS_SQL}
                 GROUP BY btrim(meta->>'source_uri'), content_hash
                HAVING count(*) > 1 AND bool_and(chunk_key IS NULL)
            ) g
            """  # noqa: S608 — SESSION_ROWS_SQL is a constant fragment
        ).fetchone()[0]
    return {
        "by_status": {s: {"rows": int(n), "bytes": int(b)} for s, n, b in by_status},
        "unkeyed_groups": int(unkeyed),
    }

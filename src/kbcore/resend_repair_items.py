"""kbcore.resend_repair_items — step 2 of ``kb repair resend-dups``: the ITEMS.

Split from :mod:`kbcore.resend_repair` (ratchet). For a duplicate group
``(project B, uri U)`` the items B's re-extraction minted from U are archived
ONLY behind the canonical gate (proposal_v2 Appendix E, brief amendment 5):
every canonical row ``'extracted'`` AND the canonical GROUP's
``sum(saved_count) > 0`` — the total is booked on the canonical group's first id
(``ingest_queue.mark_group_extracted``), so it is read over the group, never the
referenced rows alone. Otherwise every item is ``keep: canonical-unmined(<why>)``.

Candidates: active, ``trust='unverified'`` session items of U under B that are not
summaries/reports and were never recalled (``access_log``); recalled ones are
listed ``keep: recalled``. Tier 1 archives them when EVERY row of B's group is a
duplicate; a mixed group is ``keep: mixed`` unless ``--attribute-quotes`` (tier 2)
finds the item's grounded quote ONLY in duplicate rows (assembled text, never raw
JSONL); found in one of B's own rows or nowhere -> ``keep: ambiguous`` (fail-safe).

The flip is the batched sibling of :func:`kbcore.items.archive_item` — the same
``status='active' -> 'archived'`` guard and ``meta.archived`` stamp, plus
``meta.repair`` and the items' open ``unverified_extraction`` review rows resolved
in the SAME transaction (one per group) — :func:`archive_items`, the one archive
path the tier-3 replay (:mod:`kbcore.resend_repair_replay`) shares, with its own
reason. Bodies (Inv 1), trust, relations and
reserved projects are never touched; nothing is deleted (Inv 5). The duplicate set
both tiers and the audit stamp read is ``GroupPlan.dup_ids`` (existing marks + this
run's flips), so an ``--as-resend`` run sees the rows it is about to flip.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

from psycopg.types.json import Jsonb

from kbcore.db import connect, transaction
from kbcore.resend_repair import ARCHIVE_REASON, REPAIR_BY, SESSION_ROWS_SQL, GroupPlan, now_iso

logger = logging.getLogger("kbcore.resend_repair")

# The SUCCESSOR keep, as a predicate on an ``items`` row named ``{t}``: another item's
# ``superseded_by`` points at it, or a ``'duplicate'`` item was folded into it
# (``derived_from``). Archiving one would leave the older fact superseded by (or
# folded into) an archived item. The replay's plan reads the same rule; the write
# re-checks it (review 2): the plan-to-prompt window, and tiers 1/2 never plan it.
SUCCESSOR_SQL = """(EXISTS (SELECT 1 FROM items sp WHERE sp.superseded_by = {t}.id)
    OR EXISTS (SELECT 1 FROM relations sr JOIN items sd ON sd.id = sr.src_id
                WHERE sr.rel = 'derived_from' AND sr.src_kind = 'item'
                  AND sr.dst_kind = 'item' AND sr.dst_id = {t}.id
                  AND sd.status = 'duplicate'))"""


class ArchiveResult(NamedTuple):
    archived: int  # items flipped active -> archived
    kept_successor: int  # still active at write time but a successor: left alone


_CANDIDATES_SQL = """
SELECT i.id, i.meta->>'quote',
       EXISTS (SELECT 1 FROM access_log a WHERE a.item_id = i.id) AS recalled
  FROM items i JOIN sources s ON s.id = i.source_id
 WHERE s.kind = 'session' AND s.uri = %(uri)s AND i.project = %(project)s
   AND i.status = 'active' AND s.trust = 'unverified'
   AND i.type NOT IN ('summary', 'report')
 ORDER BY i.id
"""


def canonical_gate(conn, canon_ids: list[int]) -> tuple[str, int, str | None]:
    """``(status, group saved total, canonical project)`` for the gate."""
    if not canon_ids:
        return "none", 0, None
    rows = conn.execute(
        "SELECT id, project, btrim(meta->>'source_uri'), status FROM ingest_queue "
        "WHERE id = ANY(%(ids)s::bigint[])",
        {"ids": list(canon_ids)},
    ).fetchall()
    if not rows:
        return "none", 0, None
    statuses = {r[3] for r in rows}
    status = statuses.pop() if len(statuses) == 1 else "mixed"
    if len(rows) != len(set(canon_ids)):
        status = "mixed"  # a referenced canonical no longer exists (never expected)
    keys = {(r[1], r[2]) for r in rows}
    saved = conn.execute(
        """
        SELECT coalesce(sum(saved_count), 0) FROM ingest_queue
         WHERE status = 'extracted'
           AND (project, btrim(meta->>'source_uri')) IN (
               SELECT p, u FROM unnest(%(ps)s::text[], %(us)s::text[]) AS t(p, u))
        """,
        {"ps": [k[0] for k in keys], "us": [k[1] for k in keys]},
    ).fetchone()[0]
    return status, int(saved), rows[0][1]


def _keep(p: GroupPlan, reason: str, n: int = 1) -> None:
    p.keep[reason] = p.keep.get(reason, 0) + n


def plan_items(p: GroupPlan, *, attribute_quotes: bool = False, rows_only: bool = False) -> None:
    """Fill the canon columns, then (unless ``rows_only``) ``p.items`` /
    ``p.archive_ids`` / ``p.keep``. The gate is always read so the dry-run table
    never shows a blank canonical for a group that has one."""
    with connect() as conn:
        p.canon_status, p.canon_saved, p.canon_project = canonical_gate(conn, p.canon_ids)
        if rows_only:
            return
        cands = conn.execute(_CANDIDATES_SQL, {"uri": p.uri, "project": p.project}).fetchall()
        p.items = len(cands)
        if not cands:
            return
        if p.reserved:
            _keep(p, "reserved project", len(cands))
            return
        gate_ok = p.canon_status == "extracted" and p.canon_saved > 0
        if not gate_ok:
            why = "zero-yield" if p.canon_status == "extracted" else p.canon_status
            _keep(p, f"canonical-unmined({why})", len(cands))
            return
        recalled = [int(r[0]) for r in cands if r[2]]
        if recalled:
            _keep(p, "recalled", len(recalled))
        fresh = [(int(r[0]), r[1]) for r in cands if not r[2]]
        if p.rows == p.dup:  # tier 1: every row of B's group re-mined the canonical's content
            p.archive_ids = [iid for iid, _q in fresh]
            return
        if not attribute_quotes:
            _keep(p, "mixed (use --attribute-quotes)", len(fresh))
            return
        _attribute_quotes(conn, p, fresh)


def _attribute_quotes(conn, p: GroupPlan, fresh: list[tuple[int, str | None]]) -> None:
    """Tier 2: an item is a candidate only when its quote lives ONLY in duplicate rows."""
    from kbworker.sessions import assemble_text  # the ASSEMBLED form, never raw JSONL

    dup_ids = set(p.dup_ids)  # the same set tier 1 used — includes this run's planned flips
    rows = group_rows(conn, p.project, p.uri)
    dup_text = [assemble_text(r["content"]) for r in rows if r["id"] in dup_ids]
    own_text = [assemble_text(r["content"]) for r in rows if r["id"] not in dup_ids]
    for iid, quote in fresh:
        q = (quote or "").strip()
        if not q:
            _keep(p, "ambiguous (no quote)")
            continue
        in_dup = any(q in t for t in dup_text)
        in_own = any(q in t for t in own_text)
        if in_dup and not in_own:
            p.archive_ids.append(iid)
        else:
            _keep(p, "ambiguous")


def archive_group(p: GroupPlan) -> int:
    """Archive ``p.archive_ids`` + resolve their open review rows, ONE transaction.
    Returns the archived count (a successor kept at write time is logged)."""
    if not p.archive_ids or p.reserved:
        return 0
    archived = {"canonical_project": p.canon_project, "source_uri": p.uri}
    return archive_items(
        p.archive_ids, reason=ARCHIVE_REASON, archived={**archived, "queue_rows": sorted(p.dup_ids)}
    ).archived


def archive_items(
    ids: list[int],
    *,
    reason: str,
    archived: dict[str, Any],
    repair: dict[str, Any] | None = None,
) -> ArchiveResult:
    """THE archive path of every resend-dups tier, ONE transaction per call.

    ``status 'active' -> 'archived'``; ``meta.archived = {at, reason} | archived`` and
    ``meta.repair = {at, by, reason, review_ids} | repair``; the items' open
    ``unverified_extraction`` review rows are resolved in the same transaction and
    their ids recorded in ``meta.repair.review_ids`` (what ``--undo`` re-opens). Only
    rows of items still ``'active'`` are resolved, so an item a concurrent writer
    already retired never loses a review row the undo could not give back.

    The SUCCESSOR keep (:data:`SUCCESSOR_SQL`) is enforced HERE, at write time, for
    every tier: such an item is neither archived nor has its review rows resolved,
    and is counted in ``kept_successor`` (and logged).
    """
    if not ids:
        return ArchiveResult(0, 0)
    at = now_iso()
    count = 0
    keep = SUCCESSOR_SQL.format(t="items")
    with transaction() as conn:
        kept = conn.execute(
            f"SELECT count(*) FROM items WHERE id = ANY(%(ids)s::bigint[]) "  # noqa: S608
            f"AND status = 'active' AND {keep}",  # constant fragment
            {"ids": list(ids)},
        ).fetchone()[0]
        reviews = conn.execute(
            f"""
            UPDATE review_queue SET resolved = true
             WHERE item_id = ANY(%(ids)s::bigint[]) AND resolved = false
               AND reason = 'unverified_extraction'
               AND item_id IN (SELECT id FROM items
                                WHERE id = ANY(%(ids)s::bigint[]) AND status = 'active'
                                  AND NOT {keep})
             RETURNING id, item_id
            """,  # noqa: S608 — SUCCESSOR_SQL is a constant fragment
            {"ids": list(ids)},
        ).fetchall()
        by_item: dict[int, list[int]] = {}
        for rid, iid in reviews:
            by_item.setdefault(int(iid), []).append(int(rid))
        for iid in ids:
            cur = conn.execute(
                f"""
                UPDATE items
                   SET status = 'archived', updated_at = now(),
                       meta = coalesce(meta, '{{}}'::jsonb) || jsonb_build_object(
                           'archived', jsonb_build_object(
                               'at', %(at)s::text, 'reason', %(reason)s::text) || %(arch)s::jsonb,
                           'repair', jsonb_build_object(
                               'at', %(at)s::text, 'by', %(by)s::text,
                               'reason', %(reason)s::text, 'review_ids', %(rids)s::jsonb)
                               || %(rep)s::jsonb)
                 WHERE id = %(id)s AND status = 'active' AND NOT {keep}
                """,  # noqa: S608 — SUCCESSOR_SQL is a constant fragment
                {
                    "at": at,
                    "reason": reason,
                    "arch": Jsonb(archived),
                    "by": REPAIR_BY,
                    "rids": Jsonb(by_item.get(iid, [])),
                    "rep": Jsonb(repair or {}),
                    "id": iid,
                },
            )
            count += cur.rowcount or 0
    if kept:
        logger.warning(
            "resend-dups archive: kept %d of %d planned item(s) at write time -- a successor "
            "(another item's superseded_by, or a folded duplicate's derived_from, points at it)",
            kept,
            len(ids),
        )
    return ArchiveResult(count, int(kept))


def group_rows(conn, project: str, uri: str) -> list[dict[str, Any]]:
    """Every row of ``(project, uri)`` with its content — tier 2's quote search."""
    rows = conn.execute(
        f"""
        SELECT id, content, duplicate_of IS NOT NULL
          FROM ingest_queue
         WHERE {SESSION_ROWS_SQL} AND project = %(project)s
           AND btrim(meta->>'source_uri') = %(uri)s
         ORDER BY id
        """,  # noqa: S608 — SESSION_ROWS_SQL is a constant fragment
        {"project": project, "uri": uri},
    ).fetchall()
    return [{"id": int(r[0]), "content": r[1], "dup": bool(r[2])} for r in rows]

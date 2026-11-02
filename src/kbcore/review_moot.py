"""kbcore.review_moot — the moot sweep for CONTRADICTION rows (KB 57973).

:func:`kbcore.review_auto.close_moot_rows` (WP-1, owner decision 2026-07-18) was
scoped to ``unverified_extraction`` rows, and its test pinned that "a contradiction
row for a retired item is NOT the moot sweep's business": other paths were meant to
close those. None did, for a pair that went moot OUTSIDE the evidence step's own
pass. :func:`kbcore.evidence.open_contradiction_rows` skips a pair unless both sides
are active ("judging it would waste GPU"), the evidence step's post-pass closes only
a pair that went moot during that same pass, and quiet expiry never reads
contradiction rows. On 2026-11-02 that was 269 of the 507 open contradiction rows,
the oldest from 2026-07-28: questions about items retrieval no longer serves.

The rule is the one the system already applies to a contradiction pair
(:func:`kbcore.review_recency.pair_moot`): either side no longer active = moot,
whatever retired it (supersede, dedupe 'duplicate', archive, reject). Closing is a
status flip (Invariant 5), stamped like the extraction twin with
``auto_resolution = {kind: 'item_retired', a_status, b_status, at}``.

A side that dedupe folded into a canonical item closes the row only when the
canonical pair is already settled: the canonical IS the other side, the pair has a
row of its own, or the scan has judged it. Otherwise the row stays OPEN for the
owner (:func:`_unasked_canonical`): the scan only offers same-project pairs inside
its lead-chunk band, and on 2026-11-02 all 51 such canonical pairs sat outside it
(48 below 0.80, 3 cross-project), so closing would end a live question for good. An
OWNER veto (``auto_resolve_hold.kind = 'owner_restore'``) is never overridden either.
The gauge (:func:`open_moot_contradictions`) counts both. A close is undone when an
owner path brings the retired side back (:func:`reopen_unmooted_in`): nothing would
ever re-queue a judged pair, so a close that outlived its premise would lose it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime

import psycopg

from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.review_recency import HOLD_RESTORED

logger = logging.getLogger("kbcore.review_moot")

# One predicate for the sweep and the gauge, so the two can never drift apart.
# The joins require BOTH sides to exist; items are never deleted (Invariant 5).
_MOOT_FROM = """
    FROM review_queue rq
    JOIN items ia ON ia.id = (rq.detail->>'a_id')::bigint
    JOIN items ib ON ib.id = (rq.detail->>'b_id')::bigint
    WHERE rq.resolved = false AND rq.reason = 'contradiction'
      AND (ia.status <> 'active' OR ib.status <> 'active')
"""
_NOT_VETOED = "COALESCE(rq.detail->'auto_resolve_hold'->>'kind', '') <> %(veto)s"


def close_moot_contradictions() -> int:
    """Close the open contradiction rows with a non-active side, except an owner veto
    and a question that would end for good (:func:`_unasked_canonical`).

    One transaction, idempotent (a second run finds nothing more to close). Returns
    the number of rows closed.
    """
    at = datetime.now(UTC).isoformat()
    with transaction() as conn:
        moot = conn.execute(
            f"SELECT rq.id, ia.id, ia.status, ib.id, ib.status {_MOOT_FROM} AND {_NOT_VETOED}",  # noqa: S608
            {"veto": HOLD_RESTORED},
        ).fetchall()
        kept = {m[0] for m in moot if _unasked_canonical(conn, *m[1:])}
        rows = conn.execute(
            f"""
            UPDATE review_queue rq
            SET resolved = true,
                detail = COALESCE(rq.detail, '{{}}'::jsonb) || jsonb_build_object(
                    'auto_resolution', jsonb_build_object(
                        'kind', 'item_retired',
                        'a_status', ia.status,
                        'b_status', ib.status,
                        'at', %(at)s::text))
            FROM items ia, items ib
            -- moot and unvetoed AGAIN, on the row version the UPDATE locks: a restore
            -- that commits while this waits is re-checked here, not in the snapshot
            WHERE rq.id = ANY(%(ids)s) AND rq.resolved = false AND {_NOT_VETOED}
              AND ia.id = (rq.detail->>'a_id')::bigint AND ib.id = (rq.detail->>'b_id')::bigint
              AND (ia.status <> 'active' OR ib.status <> 'active')
            RETURNING rq.id
            """,  # noqa: S608 — _NOT_VETOED is constant SQL; values are bound
            {"veto": HOLD_RESTORED, "at": at, "ids": [m[0] for m in moot if m[0] not in kept]},
        ).fetchall()
    if rows or kept:
        logger.info(
            "moot sweep: closed %d contradiction row(s) with a retired side; kept %d open "
            "(a duplicate's canonical never compared with the other side)",
            len(rows),
            len(kept),
        )
    return len(rows)


def _canonical(conn: psycopg.Connection, item_id: int) -> int | None:
    """The item a 'duplicate' was folded into (``consolidation.mark_duplicate``'s edge)."""
    row = conn.execute(
        "SELECT dst_id FROM relations WHERE src_kind = 'item' AND src_id = %s "
        "AND rel = 'derived_from' AND dst_kind = 'item' ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    return int(row[0]) if row else None


def _unasked_canonical(conn: psycopg.Connection, a: int, sa: str, b: int, sb: str) -> bool:
    """A 'duplicate' side whose canonical pair is live and was never compared: both
    canonical items active and distinct, and the pair has neither a contradiction row
    (any state, either orientation) nor a judgement at the current embed model."""
    if "duplicate" not in (sa, sb):
        return False
    pair = [_canonical(conn, a) if sa == "duplicate" else a]
    pair.append(_canonical(conn, b) if sb == "duplicate" else b)
    if None in pair or pair[0] == pair[1]:
        return False
    lo, hi = sorted(pair)
    (unasked,) = conn.execute(
        """
        SELECT (SELECT count(*) FROM items WHERE id IN (%(lo)s, %(hi)s) AND status = 'active') = 2
           AND NOT EXISTS (
               SELECT 1 FROM review_queue WHERE reason = 'contradiction'
                  AND ((detail->>'a_id')::bigint, (detail->>'b_id')::bigint)
                      IN ((%(lo)s, %(hi)s), (%(hi)s, %(lo)s)))
           AND NOT EXISTS (
               SELECT 1 FROM contradiction_judgements
                WHERE a_id = %(lo)s AND b_id = %(hi)s AND embed_model = %(model)s)
        """,
        {"lo": lo, "hi": hi, "model": settings.kb_embed_model},
    ).fetchone()
    return bool(unasked)


def open_moot_contradictions() -> int:
    """Open contradiction rows with a retired side: owner vetoes, questions kept open
    for the owner (:func:`_unasked_canonical`), plus pairs made moot AFTER tonight's
    sweep (auto-apply, evidence, archive_stale run later; the next sweep takes those).

    The nightly report's ``moot_contradictions_open`` gauge. Read-only.
    """
    with connect() as conn:
        (n,) = conn.execute(f"SELECT count(*) {_MOOT_FROM}").fetchone()  # noqa: S608
    return int(n)


def reopen_unmooted_in(conn: psycopg.Connection, item_ids: Iterable[int]) -> int:
    """Reopen the rows :func:`close_moot_contradictions` closed for ``item_ids`` once
    BOTH sides are active again. Runs in the caller's transaction: every owner path
    that brings a retired item back (``kb review restore``, the auto-apply undo, the
    resend-dups undo) calls it right after its item flip.

    Nothing else would ever re-queue such a pair: the scan memorises every judged
    pair and ``record_contradiction`` is idempotent on the ``contradicts`` edge. The
    stamp gains ``undone_at``; a later retirement re-stamps it. Returns rows reopened.
    """
    ids = sorted({int(i) for i in item_ids})
    if not ids:
        return 0
    rows = conn.execute(
        """
        UPDATE review_queue rq
        SET resolved = false,
            detail = rq.detail || jsonb_build_object('auto_resolution',
                rq.detail->'auto_resolution' || jsonb_build_object('undone_at', %(at)s::text))
        FROM items ia, items ib
        WHERE rq.resolved AND rq.reason = 'contradiction'
          AND rq.detail->'auto_resolution'->>'kind' = 'item_retired'
          AND rq.detail->'auto_resolution'->>'undone_at' IS NULL
          AND ia.id = (rq.detail->>'a_id')::bigint AND ib.id = (rq.detail->>'b_id')::bigint
          AND ia.status = 'active' AND ib.status = 'active'
          AND (ia.id = ANY(%(ids)s) OR ib.id = ANY(%(ids)s))
        RETURNING rq.id
        """,
        {"at": datetime.now(UTC).isoformat(), "ids": ids},
    ).fetchall()
    return len(rows)

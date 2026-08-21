"""kbcore.review_rejects — the reject-suggestion STOCK (review #5, val-3 / strat-8).

Auto-apply's cross-project jurisdiction wall (:mod:`kbcore.review_apply_dups`)
refuses to retire an item toward a canonical in another project — correctly and
PERMANENTLY: the wall is settled design (commit 0cfa6e0), so a wall-blocked
reject suggestion is a row the nightly loop re-examines every run and can never
apply. That stock is monotone (395 -> 1,264 across the first 8 runs, ~58% of
the examined universe) and it buries the digest's skip counters. This module
holds the two owner-speed dispositions for the stock, each an Invariant-5
status flip — no row is deleted, no item is touched, no trust changes:

* :func:`close_wall_blocked_rows` — resolve the wall-blocked rows with a
  distinct, reversible ``auto_resolution.kind='wall_blocked'`` stamp so the
  nightly stops rescanning them (:func:`undo_wall_blocked` reopens). The
  project wall itself is UNTOUCHED: this disposes of ROWS the wall already
  ruled on; it never widens or narrows what the wall blocks.
* :func:`list_suggested_rejects` — the selection behind ``kb review bulk
  --suggested-rejects``, the owner-speed path over the pending reject backlog
  (2,153 rows at review #5 with no listing or action surface).

Separate module beside :mod:`kbcore.review_auto` (which sits near the 300-line
cap) — the same seam :mod:`kbcore.review_expire` used when it split out.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from kbcore import review_apply_dups, review_apply_rails
from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation
from kbcore.review_expire import REJECT_TRIAGE_ACTIONS
from kbcore.triage_sql import sql_triage_field

#: Stamp kind for a wall-blocked close. Distinct from every other
#: ``auto_resolution`` kind so the close is auditable and selectable by
#: :func:`undo_wall_blocked`; the stored triage verdict is preserved untouched.
WALL_BLOCKED_KIND = "wall_blocked"

logger = logging.getLogger("kbcore.review_rejects")


def _wall_blocked_qids() -> list[int]:
    """Open reject_duplicate rows tonight's run would count ``skipped_cross_project``.

    EXACTLY the nightly plan's partition — ``candidates()`` then rails 1-2
    (cooling / access, mirrored from ``review_apply_rails.plan_run``) then the
    dup split — so this close and the run's ``skipped_cross_project`` counter
    can never disagree about which rows the wall blocks. A row still cooling
    stays open (the owner sees it in >= N digests first); an accessed row is
    the human's (rail 2); a linked pair counts under the more specific
    ``linked_alternative`` wall and is left open for the human, exactly as the
    plan partitions it. ``candidates()`` also excludes any row carrying an
    ``undone_at`` veto, so a reopened close is never re-selected.
    """
    cooling = max(0, int(settings.kb_auto_apply_cooling_days))
    cutoff = datetime.now(UTC) - timedelta(days=cooling)
    survivors = [
        r
        for r in review_apply_rails.candidates()
        if r.triage_at is not None and r.triage_at <= cutoff and not r.accessed
    ]
    split = review_apply_dups.split_dups(
        [(r.qid, r.item_id, r.dup_of) for r in survivors if r.action == "reject_duplicate"]
    )
    return sorted(split.cross_project)


def close_wall_blocked_rows() -> int:
    """Resolve the wall-blocked reject suggestions (reversible status flip).

    The wall guarantees automation will NEVER apply these rows, so leaving them
    open only makes every later run re-derive the same refusal and buries the
    digest's counters under a monotone stock. Each row is closed with
    ``resolved=true`` plus an ``auto_resolution={kind:'wall_blocked', at}``
    stamp merged into ``detail`` — the triage verdict stays in place, the ITEM
    stays active (the wall's ruling stands; a human can still act via
    ``kb review bulk --suggested-rejects`` before the close, or reopen after).
    Invariant 5 clean: a status flip, never a delete; reversed row-for-row by
    :func:`undo_wall_blocked`. Honors the auto-apply kill switch
    (``kb_auto_apply_enabled=false`` -> 0, no DB write) since the selection is
    auto-apply's own partition. Idempotent: closed rows leave ``candidates()``.
    Returns the number of rows closed.
    """
    if not settings.kb_auto_apply_enabled:
        return 0
    qids = _wall_blocked_qids()
    if not qids:
        return 0
    stamp = {"auto_resolution": {"kind": WALL_BLOCKED_KIND, "at": datetime.now(UTC).isoformat()}}
    with transaction() as conn:
        rows = conn.execute(
            """
            UPDATE review_queue
            SET resolved = true,
                detail = COALESCE(detail, '{}'::jsonb) || %s
            WHERE id = ANY(%s) AND resolved = false
            RETURNING id
            """,
            (Jsonb(stamp), qids),
        ).fetchall()
    if rows:
        logger.info("wall-blocked close: resolved %d review row(s)", len(rows))
    return len(rows)


def undo_wall_blocked(since: datetime, *, dry_run: bool = False) -> list[int]:
    """Reopen wall_blocked closes stamped at/after ``since`` (owner escape hatch).

    Mirrors ``review_apply.undo_auto_applied``'s row-only branch: the close
    touched no item, so its reversal touches no item — ``resolved`` flips back
    and the stamp gains ``undone_at``, which ``review_apply_rails.candidates()``
    treats as a PERMANENT veto, so a later nightly close cannot silently
    re-close what the owner just reopened. Returns the reopened queue ids
    (``dry_run`` returns what WOULD reopen and writes nothing).
    """
    if since.tzinfo is None:
        raise InvalidOperation("undo_wall_blocked requires a timezone-aware UTC datetime")
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT rq.id, rq.detail->'auto_resolution'
            FROM review_queue rq
            WHERE rq.resolved = true
              AND rq.detail->'auto_resolution'->>'kind' = %(kind)s
              AND rq.detail->'auto_resolution'->>'undone_at' IS NULL
              AND (rq.detail->'auto_resolution'->>'at')::timestamptz >= %(since)s
            ORDER BY rq.id
            """,
            {"kind": WALL_BLOCKED_KIND, "since": since},
        ).fetchall()
    if dry_run:
        return [int(r[0]) for r in rows]
    at = datetime.now(UTC).isoformat()
    undone: list[int] = []
    for qid, res in rows:
        prior = res if isinstance(res, dict) else {}
        with transaction() as conn:
            conn.execute(
                "UPDATE review_queue SET resolved = false, "
                "detail = COALESCE(detail, '{}'::jsonb) || %s WHERE id = %s",
                (Jsonb({"auto_resolution": {**prior, "undone_at": at}}), qid),
            )
        undone.append(int(qid))
    return undone


def list_suggested_rejects(
    project: str | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    """The pending reject backlog ``kb review bulk --suggested-rejects`` drains.

    Open ``unverified_extraction`` rows whose stored triage verdict asks for
    the item to be RETIRED and whose item is still active+unverified — the same
    liveness guard auto-apply's candidates carry, because a retired item's row
    is the moot sweep's job and ``review.reject`` on a non-active item would
    clobber a status some other disposition set. Oldest-first by queue id.
    READ-ONLY: the write path is ``review_auto.bulk_resolve('reject', ...)``,
    each row riding the owner chokepoint ``review.reject`` (Invariant-5 status
    flips, per-row isolation). Rows carrying an ``undone_at`` veto ARE listed —
    the veto denies AUTOMATION, never a human path, and the CLI previews and
    confirms before writing.
    """
    params: dict[str, Any] = {"reject": list(REJECT_TRIAGE_ACTIONS)}
    proj = ""
    if project is not None:
        proj = "AND i.project = %(project)s"
        params["project"] = project
    lim = ""
    if limit is not None:
        lim = "LIMIT %(limit)s"
        params["limit"] = int(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT rq.id, rq.item_id, i.project, {sql_triage_field("action")}, i.title
            FROM review_queue rq
            JOIN items i ON i.id = rq.item_id
            LEFT JOIN sources s ON s.id = i.source_id
            WHERE rq.resolved = false
              AND rq.reason = 'unverified_extraction'
              AND i.status = 'active'
              AND COALESCE(s.trust, 'verified') = 'unverified'
              AND {sql_triage_field("action")} = ANY(%(reject)s)
              {proj}
            ORDER BY rq.id
            {lim}
            """,  # noqa: S608 — constant SQL fragments; every value rides a parameter
            params,
        ).fetchall()
    return [
        {
            "queue_id": int(r[0]),
            "item_id": int(r[1]),
            "project": r[2],
            "action": r[3],
            "title": r[4],
        }
        for r in rows
    ]

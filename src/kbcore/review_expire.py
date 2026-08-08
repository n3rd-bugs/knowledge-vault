"""kbcore.review_expire — QUIET EXPIRY of never-recalled review rows, on rails.

Split out of :mod:`kbcore.review_auto` (300-line ratchet) when the sweep grew the
rails its sibling :mod:`kbcore.review_apply` already had. Quiet expiry closes an
open ``unverified_extraction`` row whose item is still active but was NEVER
recalled: only the ROW closes (Invariant 5 — a status flip), the item stays
active, unverified and searchable, and every closure is stamped
``detail.auto_resolution = {kind: 'quiet_expiry', age_days, at}``.

OWNER AMENDMENT 2026-08-08, amending decision KB 957 ("an untriaged row expires
as before"). The sweep was ONE uncapped UPDATE with no dry run, no undo and no
call-site stop gate, and its first real firing stood to close a ~6,600-row pool
in a single statement. Three rails now bound it, all additive (no migration):

1. THE UNTRIAGED GATE (:data:`EXPIRY_GATE`) — a row that carries NO triage
   verdict can never quiet-expire. Closing one turns "nobody has looked at this
   yet" into silence while the item stays live, unverified and searchable
   forever; held open, it is honest queue depth an owner can see and drain
   (``never_triaged_open`` in the nightly report). Expiry is now the exit for
   rows the machine DID examine and had nothing to retire over.
2. THE CAP (``kb_review_expire_cap``, 500; 0 = uncapped) with ``ORDER BY
   rq.created_at`` — oldest first, so a first firing is a ramp instead of a
   cliff and each night takes the rows closest to their deadline.
3. THE UNDO (:func:`undo_quiet_expiry`) — reopens a batch and stamps
   ``undone_at``, which :data:`EXPIRY_GATE` treats as a PERMANENT VETO so
   tonight's sweep cannot silently re-close what the owner just reopened.

Kill switch is unchanged: ``kb_review_expire_days = 0`` returns without touching
the DB. What does NOT move: the reject-suggestion exemption and the access_log
clause in :data:`QUIET_ELIGIBLE` are byte-for-byte what they were.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from kbcore import llm
from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation

logger = logging.getLogger("kbcore.review_expire")

STAMP_KIND = "quiet_expiry"
# Rows shown by a dry run / logged by a real one. Enough to recognise WHAT is
# about to close (which project, how old) without printing a 6,600-row table.
SAMPLE_SIZE = 5

# Triage suggestions that ask for the ITEM to be retired. Derived from
# llm.TRIAGE_ACTIONS rather than hand-listed so a future reject_* action is
# exempt from quiet expiry the day it ships, not the day someone remembers.
REJECT_TRIAGE_ACTIONS = tuple(a for a in llm.TRIAGE_ACTIONS if a.startswith("reject"))

# The quiet-expiry predicate, shared by the sweep and the "when does the first
# row expire" report count so the two can never drift. Constant SQL text with
# named placeholders — no value is ever formatted in (Invariant 2). ``i`` is the
# joined items row; ``%(reject)s`` is the REJECT_TRIAGE_ACTIONS array.
QUIET_ELIGIBLE = """
              rq.resolved = false
              AND rq.reason = 'unverified_extraction'
              AND i.status = 'active'
              -- HARD DEADLINE FIX: a row carrying a reject_* triage suggestion is
              -- a KNOWN-BAD item waiting on a human to apply it. Expiring it
              -- closes the evidence and leaves the item live, unverified and
              -- searchable forever — the exact opposite of what the suggestion
              -- asked for. Those rows therefore never expire; they stay open
              -- until an owner applies or dismisses them.
              -- A 'keep'-suggested row DOES still expire: applying a keep closes
              -- the row and leaves the item active + unverified, which is
              -- byte-for-byte the end state quiet expiry produces, so holding it
              -- open buys nothing.
              AND NOT (COALESCE(rq.detail->'triage'->>'action', '') = ANY(%(reject)s))
              AND NOT EXISTS (
                  SELECT 1 FROM access_log al WHERE al.item_id = rq.item_id
              )
"""

# The 2026-08-08 amendment, as ONE boolean expression (no leading AND) so it can
# be a WHERE conjunct in the sweep and a FILTER in the report's first_expiry_date
# without a second copy. Both halves are "this row may actually close": it was
# EXAMINED (a triage verdict is stored), and no owner has vetoed its closure by
# undoing an earlier expiry of it.
EXPIRY_GATE = """(
                  COALESCE(rq.detail, '{}'::jsonb) ? 'triage'
                  AND rq.detail->'auto_resolution'->>'undone_at' IS NULL
              )"""

# Oldest first, capped: the victims of one run, plus the FULL eligible count in
# every row (a window function is evaluated before LIMIT, so one query answers
# both "what closes now" and "what is left for the next run"). A NULL limit is
# Postgres for "no LIMIT", which is how cap = 0 stays uncapped.
_SCAN_SQL = f"""
    SELECT rq.id,
           rq.item_id,
           i.project,
           (extract(epoch FROM now() - rq.created_at) / 86400)::int AS age_days,
           count(*) OVER () AS eligible_total
    FROM review_queue rq
    JOIN items i ON i.id = rq.item_id
    WHERE {QUIET_ELIGIBLE}
      AND {EXPIRY_GATE}
      AND rq.created_at < now() - make_interval(days => %(days)s)
    ORDER BY rq.created_at
    LIMIT %(cap)s
"""  # noqa: S608 — both fragments are constant SQL text; every value is bound

_CLOSE_SQL = """
    UPDATE review_queue rq
    SET resolved = true,
        detail = COALESCE(rq.detail, '{}'::jsonb) || jsonb_build_object(
            'auto_resolution', jsonb_build_object(
                'kind', 'quiet_expiry',
                'age_days', %(days)s::int,
                'at', %(at)s::text))
    WHERE rq.id = ANY(%(ids)s)
      AND rq.resolved = false
    RETURNING rq.id
"""


def _scan(conn: Any, older_than_days: int, cap: int | None) -> tuple[list[dict[str, Any]], int]:
    """``(victims, eligible_total)`` — the capped, oldest-first closure set."""
    rows = conn.execute(
        _SCAN_SQL,
        {"days": older_than_days, "cap": cap, "reject": list(REJECT_TRIAGE_ACTIONS)},
    ).fetchall()
    victims = [
        {"id": int(r[0]), "item_id": int(r[1]), "project": r[2], "age_days": int(r[3])}
        for r in rows
    ]
    return victims, (int(rows[0][4]) if rows else 0)


def expire_quiet_rows(
    older_than_days: int,
    *,
    dry_run: bool = False,
    cap: int | None = None,
) -> dict[str, Any]:
    """Close old ``unverified_extraction`` rows for items that were NEVER recalled.

    Returns ``{expired, eligible, eligible_remaining, sample}``:

    * ``expired`` — rows closed (in a ``dry_run``, rows that WOULD close);
    * ``eligible`` — every row the predicate matched, cap ignored;
    * ``eligible_remaining`` — what the cap left for the next run;
    * ``sample`` — up to :data:`SAMPLE_SIZE` of the closure set, oldest first,
      as ``{id, item_id, project, age_days}``.

    ``cap`` bounds ONE run and is applied ``ORDER BY rq.created_at`` — oldest
    first, so the rows closest to their deadline go first and a first firing
    ramps instead of cliffing. ``cap=None`` reads ``kb_review_expire_cap``;
    ``cap <= 0`` is UNCAPPED — deliberately NOT the sibling's convention
    (``review_apply`` treats 0 as "apply nothing"), because this sweep already
    has a kill switch and a second one hiding in the cap would be a trap.

    ``older_than_days <= 0`` is that kill switch: all zeros, no DB access.
    ``dry_run=True`` reports the same shape and writes NOTHING.

    EXEMPT: rows carrying a reject_* suggestion (:data:`QUIET_ELIGIBLE`) and rows
    with no triage verdict at all or an owner's undo veto (:data:`EXPIRY_GATE`).
    """
    out: dict[str, Any] = {"expired": 0, "eligible": 0, "eligible_remaining": 0, "sample": []}
    if older_than_days <= 0:
        return out
    limit = settings.kb_review_expire_cap if cap is None else int(cap)
    limit = None if limit <= 0 else limit
    at = datetime.now(UTC).isoformat()
    # The dry run never opens a write transaction; the real sweep scans and
    # closes inside ONE, so nothing can become ineligible (a recall, a human
    # resolving the row) between choosing the victims and stamping them.
    with connect() if dry_run else transaction() as conn:
        victims, out["eligible"] = _scan(conn, older_than_days, limit)
        out["eligible_remaining"] = out["eligible"] - len(victims)
        out["sample"] = victims[:SAMPLE_SIZE]
        if dry_run:
            out["expired"] = len(victims)
        elif victims:
            closed = conn.execute(
                _CLOSE_SQL,
                {"ids": [v["id"] for v in victims], "days": older_than_days, "at": at},
            ).fetchall()
            out["expired"] = len(closed)
    if out["expired"] and not dry_run:
        logger.info(
            "quiet expiry: closed %d row(s), %d eligible row(s) left for the next run",
            out["expired"],
            out["eligible_remaining"],
        )
    return out


def undo_quiet_expiry(since: datetime, *, dry_run: bool = False) -> list[int]:
    """Reopen rows quiet-expired at/after ``since``. Owner CLI only, NEVER scheduled.

    The escape hatch for a batch the owner disagrees with, mirroring
    :func:`kbcore.review_apply.undo_auto_applied`. Per row, in its own
    transaction: ``resolved`` goes back to false and the existing
    ``auto_resolution`` stamp gains ``undone_at``. That annotation is also the
    VETO — :data:`EXPIRY_GATE` excludes an undone row permanently, so tonight's
    sweep cannot re-close it and the pair cannot loop.

    NO ITEM STATUS IS RESTORED, because quiet expiry never touched one: the item
    stayed active, unverified and searchable the whole time (that is the entire
    point of the sweep). Only ``resolved`` rows carrying THIS stamp are reversed.

    Returns the reopened queue ids (``dry_run`` returns what WOULD reopen and
    writes nothing).
    """
    if since.tzinfo is None:
        raise InvalidOperation("undo_quiet_expiry requires a timezone-aware UTC datetime")
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
            {"kind": STAMP_KIND, "since": since},
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
    if undone:
        logger.info("quiet expiry undo: reopened %d row(s) (permanently vetoed)", len(undone))
    return undone

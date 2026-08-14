"""kbcore.review_promote — the PROMOTE PROGRAM's candidate surface (review #3 P1).

Trust has been one-way in practice: promote throughput ~0 while the top-5
unverified share rose 53.5% -> 64% (KB 19663); ~15 promotes/day holds the line.
Verdict V4 IS LAW here: trust upgrades stay HUMAN. This module only makes the
human FAST — it enumerates the items most WORTH promoting (active, unverified,
RECALL-PROVEN) and batches explicit owner decisions through the ONE existing
trust-escalation chokepoint, :func:`kbcore.review.promote`. Nothing in this file
sets ``trust``; nothing here is ever called unattended (the nightly only COUNTS
candidates, for the digest's 'promote-worthy' line).

Consolidation output (``summary`` / ``report`` items) is excluded from the
candidate surface: it is derived from source knowledge, not source knowledge,
so "verifying" it says nothing a human meant to say.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from kbcore import review, scope
from kbcore.config import settings
from kbcore.db import connect

#: Item types the candidate surface never proposes (consolidation output).
CANDIDATE_EXCLUDED_TYPES = ("summary", "report")

# The candidate predicate, shared by the list and the count so the CLI table and
# the digest's 'promote-worthy' number can never drift apart. Constant SQL text;
# every value rides a named parameter (Invariant 2). ``al.recalls`` comes from
# one grouped scan of access_log; COALESCE keeps a never-recalled item countable
# when the floor is 0.
_CANDIDATE_FROM = """
    FROM items i
    JOIN sources s ON s.id = i.source_id
    LEFT JOIN (
        SELECT item_id, count(*) AS recalls FROM access_log GROUP BY item_id
    ) al ON al.item_id = i.id
    WHERE i.status = 'active'
      AND s.trust = 'unverified'
      AND i.type <> ALL(%(excluded)s)
      AND COALESCE(al.recalls, 0) >= %(min_recalls)s
      AND (%(project)s::text IS NULL OR i.project = %(project)s)
      AND (%(item_type)s::text IS NULL OR i.type = %(item_type)s)
"""

# Most-recalled first (proven value), then oldest first (longest-standing
# knowledge), then id for a stable order under pagination.
_LIST_SQL = f"""
    SELECT i.id,
           i.project,
           i.type,
           i.title,
           COALESCE(al.recalls, 0)::int AS recalls,
           (extract(epoch FROM now() - i.created_at) / 86400)::int AS age_days,
           (SELECT rq.id FROM review_queue rq
             WHERE rq.item_id = i.id AND rq.resolved = false
               AND rq.reason = 'unverified_extraction'
             ORDER BY rq.id LIMIT 1) AS queue_id
    {_CANDIDATE_FROM}
    ORDER BY COALESCE(al.recalls, 0) DESC, i.created_at, i.id
    LIMIT %(limit)s OFFSET %(offset)s
"""  # noqa: S608 — the interpolated fragment is constant SQL text; values are bound

_COUNT_SQL = f"SELECT count(*) {_CANDIDATE_FROM}"  # noqa: S608 — constant fragment, values bound


def _params(min_recalls: int | None, project: str | None, item_type: str | None) -> dict[str, Any]:
    floor = settings.kb_promote_candidate_min_recalls if min_recalls is None else int(min_recalls)
    return {
        "excluded": list(CANDIDATE_EXCLUDED_TYPES),
        "min_recalls": max(0, floor),
        "project": project,
        "item_type": item_type,
    }


def list_promote_candidates(
    min_recalls: int | None = None,
    *,
    project: str | None = None,
    item_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """The items most WORTH promoting, ranked. READ-ONLY — never writes anything.

    Active, ``trust='unverified'``, recall-proven (``access_log`` count >=
    ``min_recalls``, default :data:`settings.kb_promote_candidate_min_recalls`),
    excluding consolidation output (:data:`CANDIDATE_EXCLUDED_TYPES`). Ordered
    most-recalled first, then oldest. Bounded (``limit``, floor 1) and paginated
    (``offset``) so a big backlog can be walked page by page.

    Each row is ``{item_id, project, type, title, recalls, age_days, queue_id}``
    where ``queue_id`` is the item's OLDEST open ``unverified_extraction`` review
    row, or ``None`` — a candidate whose row already closed (quiet expiry, a
    dismiss) is still a candidate; :func:`promote_batch` handles both shapes.
    """
    params = _params(min_recalls, project, item_type)
    params.update({"limit": max(1, int(limit)), "offset": max(0, int(offset))})
    with connect() as conn:
        rows = conn.execute(_LIST_SQL, params).fetchall()
    return [
        {
            "item_id": int(r[0]),
            "project": r[1],
            "type": r[2],
            "title": r[3],
            "recalls": int(r[4]),
            "age_days": int(r[5]),
            "queue_id": int(r[6]) if r[6] is not None else None,
        }
        for r in rows
    ]


def count_promote_candidates(min_recalls: int | None = None) -> int:
    """How many candidates the surface holds (default threshold). READ-ONLY.

    The nightly report / digest number: same predicate as the list, so
    'promote-worthy: N' is exactly the table ``kb review promote-candidates``
    prints.
    """
    with connect() as conn:
        row = conn.execute(_COUNT_SQL, _params(min_recalls, None, None)).fetchone()
    return int(row[0])


def promote_batch(item_ids: list[int], *, dry_run: bool = False) -> list[tuple[int, str]]:
    """Walk item ids through :func:`kbcore.review.promote`, one at a time.

    OWNER-ONLY, like the singular promote it wraps (V4: a batch surface must not
    widen who can escalate trust). Every actual trust flip happens INSIDE
    ``review.promote`` — this wrapper only resolves each item to its open
    ``unverified_extraction`` row first, so the batch closes that row exactly as
    a manual ``kb review promote <qid>`` does today (trust flip + row resolution
    in one transaction, pending un-quarantine and proposed-supersede handling
    included). An item WITHOUT an open row (quiet-expired, dismissed) gets an
    audit row minted via :func:`kbcore.review.queue_review` and promoted through
    the same chokepoint, so every promotion leaves the same resolved-row trail.

    PER-ITEM ISOLATION: one bad id is recorded as ``'error: <msg>'`` and the
    batch continues (the :func:`kbcore.review_auto.bulk_resolve` convention).
    ``dry_run=True`` reports the same shape and writes NOTHING. Only items that
    are ``active`` + ``unverified`` are eligible; everything else is a per-item
    error, never a silent skip. Returns ``[(item_id, outcome)]`` sorted by id.
    """
    scope.require_owner("promote-batch")
    ids = sorted(dict.fromkeys(int(i) for i in item_ids))
    if not ids:
        return []
    outcomes: dict[int, str] = {}
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT i.id, i.status, s.trust,
                   (SELECT rq.id FROM review_queue rq
                     WHERE rq.item_id = i.id AND rq.resolved = false
                       AND rq.reason = 'unverified_extraction'
                     ORDER BY rq.id LIMIT 1)
            FROM items i
            LEFT JOIN sources s ON s.id = i.source_id
            WHERE i.id = ANY(%(ids)s)
            """,
            {"ids": ids},
        ).fetchall()
    found = {int(r[0]): (r[1], r[2], r[3]) for r in rows}
    for item_id in ids:
        if item_id not in found:
            outcomes[item_id] = f"error: item {item_id} does not exist"
            continue
        status, trust, qid = found[item_id]
        if status != "active":
            outcomes[item_id] = f"error: item {item_id} is not active (status={status})"
            continue
        if trust != "unverified":
            outcomes[item_id] = f"error: item {item_id} is not unverified (nothing to promote)"
            continue
        if dry_run:
            outcomes[item_id] = (
                f"would promote (queue {qid} resolves)"
                if qid is not None
                else "would promote (audit row minted + resolved)"
            )
            continue
        try:
            if qid is None:
                # No open row to close: mint the audit row FIRST, then promote it
                # through the chokepoint — the promotion leaves the same resolved
                # review_queue trail a manual promote leaves. A crash between the
                # two statements leaves an honest OPEN row for a still-unverified
                # item (visible queue depth), never a half-promotion.
                stamp = {"promote_program": {"minted": True, "at": datetime.now(UTC).isoformat()}}
                qid = review.queue_review(item_id, "unverified_extraction", stamp)
                review.promote(qid)
                outcomes[item_id] = f"promoted (audit row {qid} minted + resolved)"
            else:
                review.promote(qid)
                outcomes[item_id] = f"promoted (queue {qid} resolved)"
        except Exception as e:  # noqa: BLE001 — per-item isolation: one bad row must not abort the batch
            outcomes[item_id] = f"error: {e}"
    return sorted(outcomes.items())

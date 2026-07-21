"""kbcore.review — the review queue for unverified/automatic knowledge (§7.3/§7.4).

Automatic extractions land in ``review_queue`` so a human can promote or reject
them in batches (Verdict V4). Phase 3 only enqueues; the interactive ``kb
review`` workflow arrives in Phase 4. All SQL lives here (Invariant 2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from kbcore import scope
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation, NotFound
from kbcore.models import REVIEW_REASONS, ReviewEntry
from kbcore.supersede import supersede


def queue_review_in(
    conn: psycopg.Connection,
    item_id: int | None,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> int:
    """Enqueue a review-queue entry inside the caller's transaction.

    Composition point so an extraction can insert the item AND its review-queue
    row in one transaction — a crash between them must not leave an unverified
    item that is never surfaced for review (review finding).
    """
    if reason not in REVIEW_REASONS:
        raise InvalidOperation(
            f"unknown review reason {reason!r}; one of {', '.join(REVIEW_REASONS)}"
        )
    row = conn.execute(
        """
        INSERT INTO review_queue (item_id, reason, detail)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (item_id, reason, Jsonb(detail or {})),
    ).fetchone()
    return int(row[0])


def queue_review(
    item_id: int | None,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> int:
    """Enqueue a review-queue entry in its own transaction. Returns the row id."""
    with transaction() as conn:
        return queue_review_in(conn, item_id, reason, detail)


def open_review_by_reason(reason: str) -> tuple[int, dict[str, Any]] | None:
    """The oldest open review row ``(id, detail)`` for a reason, or None.

    Dedup + self-heal helper for the search-quality sentinel: it enqueues at most
    one open ``search_regression`` row and auto-resolves it once quality recovers.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT id, detail FROM review_queue "
            "WHERE resolved = false AND reason = %s ORDER BY id LIMIT 1",
            (reason,),
        ).fetchone()
    if row is None:
        return None
    return int(row[0]), (row[1] if isinstance(row[1], dict) else {})


def resolve_review(queue_id: int) -> None:
    """Mark a review row resolved (status flip, Invariant 5 — no item mutation).

    A generic close for item-less rows (the search-quality sentinel's regression
    flag) that promote/reject/supersede — all of which require an item — cannot.
    """
    with transaction() as conn:
        conn.execute("UPDATE review_queue SET resolved = true WHERE id = %s", (queue_id,))


def _visibility_gate() -> tuple[str, list]:
    """SQL fragment (+params) restricting review rows to items the request may read,
    or ('', []) (full access).

    Never list rows for items in projects the request cannot read (they leak
    withheld ids/titles/quotes). A NULL-item row → predicate not TRUE → dropped,
    matching stats.review_open. NOTE: the review TOOLS are owner-only in v1 because
    ``detail`` JSONB can carry cross-project titles/ids past this row-level gate
    (see kbmcp.tools_phase5 / kbcli.review_cmds); this gate is defense-in-depth for
    the row itself and for the read-only web /review view.
    """
    frag, params = scope.project_predicate("i.project")
    return (f"AND {frag}" if frag else "", params)


def list_open_review(limit: int | None = None) -> list[ReviewEntry]:
    """Unresolved review-queue rows joined to their item (kb review list, §7.4).

    ``limit`` caps the number of rows returned (oldest-first by queue id) so a
    connector result never blows the context budget after a big nightly extraction;
    None returns all. Pair with :func:`count_open_review` to report truncation.
    """
    gate, gate_params = _visibility_gate()
    lim = "LIMIT %s" if limit is not None else ""
    params: list = [*gate_params, *([int(limit)] if limit is not None else [])]
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT rq.id, rq.reason, rq.item_id, i.title, i.status, s.trust, rq.detail
            FROM review_queue rq
            LEFT JOIN items i ON i.id = rq.item_id
            LEFT JOIN sources s ON s.id = i.source_id
            WHERE rq.resolved = false {gate}
            ORDER BY rq.id
            {lim}
            """,  # noqa: S608 — gate/lim are constant fragments; values are parameterized
            params,
        ).fetchall()
    return [
        ReviewEntry(
            queue_id=r[0],
            reason=r[1],
            item_id=r[2],
            title=r[3],
            status=r[4],
            trust=r[5],
            detail=r[6] if isinstance(r[6], dict) else {},
        )
        for r in rows
    ]


def count_open_review() -> int:
    """Total open review rows visible to this instance (cheap COUNT for truncation).

    Uses the same public gate as :func:`list_open_review`; needs the items LEFT JOIN
    so the gate's ``i.project`` predicate resolves (NULL-item rows drop under it).
    """
    gate, gate_params = _visibility_gate()
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT count(*) FROM review_queue rq
            LEFT JOIN items i ON i.id = rq.item_id
            WHERE rq.resolved = false {gate}
            """,  # noqa: S608 — gate is a constant fragment; values ride params
            gate_params,
        ).fetchone()
    return int(row[0])


def _queued_item_id(conn: psycopg.Connection, queue_id: int) -> int:
    """Resolve a review-queue row to its item id, gating READABILITY first.

    Readability BEFORE writability (supersede's ordering): ``assert_writable``'s
    rejection embeds the PROJECT NAME, so probing queue ids would let a
    non-owner resolver enumerate withheld (NDA) project names. A row whose item
    the request cannot read must be byte-identical to a missing row — the SAME
    "does not exist" NotFound, never a project-naming error (no-oracle
    guarantee, WEBUI_TIERED_ACCESS_PLAN Phase 1).
    """
    row = conn.execute(
        """
        SELECT rq.item_id, i.project
        FROM review_queue rq LEFT JOIN items i ON i.id = rq.item_id
        WHERE rq.id = %s
        """,
        (queue_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"review queue row {queue_id} does not exist")
    if row[0] is None:
        raise InvalidOperation(f"review queue row {queue_id} has no associated item")
    scope.assert_readable(row[1], f"review queue row {queue_id} does not exist")
    # Readable now, so a write-rejection may safely name the project
    # (public-only instance: cannot resolve a review for a non-public item).
    scope.assert_writable(row[1])
    return row[0]


def promote(queue_id: int) -> int:
    """Promote the queued item to trust='verified' (on its source) + resolve the row.

    OWNER-ONLY (FIX 3a, A11-3 findings 1/2/9/12/15): the unverified->verified trust
    escalation is the highest-privilege review action, so it is gated at the kbcore
    CHOKEPOINT (Invariant 2) — ``require_owner`` runs BEFORE row resolution and is
    per-principal-constant (target-independent), so it leaks no existence oracle.
    CLI/worker run as implicit OWNER and MCP already require_owner; this makes a
    trust escalation from a non-owner impossible on EVERY surface, present or future.

    QUARANTINE LIFECYCLE (A11-3 finding 6): when the promoted item is
    status='pending' (a non-owner WEB save held out of retrieval), also flip it to
    'active' in the SAME transaction — un-quarantine on promotion, so
    pending+unverified becomes active+verified (visible) atomically. Non-pending
    rows (e.g. automatic 'unverified_extraction') are unaffected: the guarded UPDATE
    matches nothing and only the trust flip runs, exactly as before.

    PROPOSED SUPERSESSION (kbcore.web_write.supersede_for_review): a web supersede
    parks the REPLACEMENT as pending+queued and leaves the OLD item ACTIVE (so a
    contributor can never hide verified knowledge behind an invisible pending item).
    The actual supersession is deferred to owner promotion: when the row's ``detail``
    carries ``proposed_supersede``, retire the old item with the now-active
    replacement AFTER this transaction commits (a fresh supersede() transaction that
    can see the just-promoted active replacement; owner scope short-circuits its caps).
    """
    scope.require_owner("promote")
    with transaction() as conn:
        item_id = _queued_item_id(conn, queue_id)
        drow = conn.execute("SELECT detail FROM review_queue WHERE id = %s", (queue_id,)).fetchone()
        detail = drow[0] if drow and isinstance(drow[0], dict) else {}
        conn.execute(
            "UPDATE sources SET trust = 'verified' "
            "WHERE id = (SELECT source_id FROM items WHERE id = %s)",
            (item_id,),
        )
        # Un-quarantine: pending web save -> active. Guarded so a non-pending row
        # (already-active extraction) is left exactly as it was (Invariant 5 flip).
        conn.execute(
            "UPDATE items SET status = 'active', updated_at = now() "
            "WHERE id = %s AND status = 'pending'",
            (item_id,),
        )
        conn.execute("UPDATE review_queue SET resolved = true WHERE id = %s", (queue_id,))
    proposed = detail.get("proposed_supersede") if isinstance(detail, dict) else None
    if proposed is not None:
        # Replacement is now active+verified; apply the deferred supersession of the
        # old item under owner scope (its own transaction — the replacement is
        # committed active above, so supersede's 'must be active' check passes).
        supersede(
            int(proposed),
            new_item_id=item_id,
            reason=detail.get("supersede_reason") or "review: promoted proposed supersession",
        )
    return item_id


def reject(queue_id: int) -> int:
    """Archive the queued item (Invariant 5: status flip, never delete) + resolve the row.

    AUTHORIZATION (FIX 3b/3d, A11-3 findings 1/2/12/15) enforced at the kbcore
    CHOKEPOINT (Invariant 2): requires ``CAP_REVIEW_RESOLVE`` (per-principal-constant
    -> oracle-safe, precedes row resolution) AND — for a non-owner — the readable-
    then-writable project walls via ``_queued_item_id``. CONFINED TO UNVERIFIED: a
    non-owner may reject only an item whose source ``trust='unverified'``; archiving
    verified knowledge is a retrieval-suppression primitive that stays owner-only
    (a NULL/absent source trust counts as verified/protected). Owner short-circuits
    both the cap and the confinement.
    """
    scope.require_cap(scope.CAP_REVIEW_RESOLVE, "reject")
    with transaction() as conn:
        item_id = _queued_item_id(conn, queue_id)
        if not scope.current().is_owner:
            row = conn.execute(
                "SELECT s.trust FROM sources s JOIN items i ON i.source_id = s.id WHERE i.id = %s",
                (item_id,),
            ).fetchone()
            if row is None or row[0] != "unverified":
                raise InvalidOperation(
                    f"review queue row {queue_id} targets protected knowledge; "
                    "archiving it is available to the KB owner only"
                )
        conn.execute(
            "UPDATE items SET status = 'archived', updated_at = now() WHERE id = %s",
            (item_id,),
        )
        conn.execute("UPDATE review_queue SET resolved = true WHERE id = %s", (queue_id,))
    return item_id


def dismiss(queue_id: int) -> None:
    """Close ANY open row, item untouched (owner decision 2026-07-18, Invariant 5).

    The generic per-row drain promote/reject cannot be: it needs no item, so it
    also closes item-less rows (the search_regression sentinel row previously
    had no dismissing tool — known gap). Only ``resolved`` flips and ``detail``
    gains an ``auto_resolution`` stamp; item status and source trust are never
    touched.

    AUTHORIZATION (FIX 3b/3c, A11-3 findings 1/2/14) enforced at the kbcore
    CHOKEPOINT (Invariant 2): requires ``CAP_REVIEW_RESOLVE`` (per-principal-
    constant -> oracle-safe, precedes row resolution) AND — for a non-owner — the
    SAME readable-then-writable resolution promote/reject get. A missing row, an
    item-less sentinel row (for a non-owner), OR a not-readable project ALL raise
    the byte-identical ``NotFound("review queue row {id} does not exist")``, so a
    grantee cannot probe the existence/state of withheld-project or sentinel rows;
    the readability wall runs BEFORE the already-resolved check so a withheld row
    is indistinguishable whether open or resolved. Owner behavior is unchanged: it
    still drains item-less sentinel rows and sees today's messages.
    """
    scope.require_cap(scope.CAP_REVIEW_RESOLVE, "dismiss")
    is_owner = scope.current().is_owner
    with transaction() as conn:
        row = conn.execute(
            "SELECT rq.item_id, rq.resolved, i.project "
            "FROM review_queue rq LEFT JOIN items i ON i.id = rq.item_id "
            "WHERE rq.id = %s FOR UPDATE OF rq",
            (queue_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"review queue row {queue_id} does not exist")
        item_id, resolved, project = row
        if not is_owner:
            # An item-less sentinel row or a withheld project must be byte-identical
            # to a missing row (no existence/state oracle); only after readability
            # passes may assert_writable name the project.
            if item_id is None:
                raise NotFound(f"review queue row {queue_id} does not exist")
            scope.assert_readable(project, f"review queue row {queue_id} does not exist")
            scope.assert_writable(project)
        if resolved:
            raise InvalidOperation(f"review queue row {queue_id} is already resolved")
        stamp = {"auto_resolution": {"kind": "dismissed", "at": datetime.now(UTC).isoformat()}}
        conn.execute(
            "UPDATE review_queue SET resolved = true, "
            "detail = COALESCE(detail, '{}'::jsonb) || %s WHERE id = %s",
            (Jsonb(stamp), queue_id),
        )


def resolve_via_supersede(queue_id: int, new_item_id: int) -> int:
    """Supersede the flagged item with an existing item, then resolve the row.

    For contradictions: the reviewer keeps ``new_item_id`` and retires the older,
    conflicting one via kbcore.supersede (immutable-safe). Returns the flagged id.
    """
    with connect() as conn:
        item_id = _queued_item_id(conn, queue_id)
    supersede(item_id, new_item_id=new_item_id, reason="review: superseded via contradiction")
    with transaction() as conn:
        conn.execute("UPDATE review_queue SET resolved = true WHERE id = %s", (queue_id,))
    return item_id


def close_contradiction_rows_for(a_id: int, b_id: int) -> int:
    """Close open contradiction rows for a pair that auto-resolution settled.

    Matches the {a_id, b_id} detail in either order. Closing (resolved=true) is
    a status flip, not a delete (Invariant 5); the underlying items already
    carry the supersession trail.
    """
    with transaction() as conn:
        rows = conn.execute(
            """
            UPDATE review_queue SET resolved = true
            WHERE resolved = false AND reason = 'contradiction'
              AND ((detail->>'a_id')::bigint, (detail->>'b_id')::bigint) IN ((%s, %s), (%s, %s))
            RETURNING id
            """,
            (a_id, b_id, b_id, a_id),
        ).fetchall()
    return len(rows)


def auto_resolve_contradiction(queue_id: int, keep: int, retire: int) -> int:
    """Auto-resolve a contradiction whose evidence held for the stability streak.

    Owner V4 refinement (2026-07): supersede the evidence-losing side with the
    winner and close the row. Invoked ONLY by the nightly worker after the same
    suggestion persisted for AUTO_RESOLVE_STREAK consecutive refreshes; fully
    audited (supersede reason + relation + report count + log line) and
    reversible (supersession is a status flip, never a delete).
    """
    supersede(
        retire,
        new_item_id=keep,
        reason="auto: evidence margin stable across nightly refreshes",
    )
    with transaction() as conn:
        conn.execute("UPDATE review_queue SET resolved = true WHERE id = %s", (queue_id,))
    return retire


def apply_suggestion(queue_id: int) -> int:
    """Execute an open row's suggested resolution NOW (human-invoked early apply).

    Returns the retired item id. Raises when the row is missing, already
    resolved, or carries no suggestion.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT detail, resolved FROM review_queue WHERE id = %s", (queue_id,)
        ).fetchone()
    if row is None:
        raise NotFound(f"review row {queue_id} does not exist")
    detail, resolved = row
    if resolved:
        raise InvalidOperation(f"review row {queue_id} is already resolved")
    sugg = (detail or {}).get("suggested_resolution")
    if not sugg:
        raise InvalidOperation(f"review row {queue_id} has no suggested resolution")
    supersede(
        sugg["retire"], new_item_id=sugg["keep"], reason="review: applied evidence suggestion"
    )
    with transaction() as conn:
        conn.execute("UPDATE review_queue SET resolved = true WHERE id = %s", (queue_id,))
    return sugg["retire"]

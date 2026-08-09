"""kbcore.review_apply_rows — auto-apply's ROW-ONLY dispositions.

The three writes automation performs that touch NO item: closing a cluster
winner, re-queueing a provably-dead duplicate for a fresh verdict, and stamping a
row the machine can never resolve. Each is one guarded UPDATE of
``review_queue`` in its own transaction, and each returns whether it matched — a
row a human resolved in the meantime simply returns False and is skipped
silently, the same contract :func:`kbcore.review_apply._claim` has.

They live together because they share the one property that makes them safe to
run unattended: no ``items`` row is read or written by any of them, so none can
retire, resurrect, or re-trust knowledge (Invariants 4/5/10). Split out of
:mod:`kbcore.review_apply` for the 300-line ratchet.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from kbcore import review_triage
from kbcore.db import transaction

#: Stamped on a cluster-winner close so :func:`kbcore.review_apply.undo_auto_applied`
#: can find it. The close is a row flip and nothing else, so its undo is a row
#: flip and nothing else — but it MUST be undoable, because the CLI, DATA_FLOW
#: and the KB record all advertise every auto-apply effect as reversible.
WINNER_STAMP_KIND = "cluster_winner_closed"
#: Terminal state for a row whose stored verdict is unappliable AND was already
#: re-triaged to the same unappliable verdict once. See :func:`mark_triage_unresolvable`.
UNRESOLVABLE_KIND = "triage_unresolvable"


def close_cluster_winner(qid: int) -> bool:
    """Close a duplicate row whose OWN item is the cluster canonical.

    The winner is never retired, so this closes the row through the MANUAL
    path's primitive (``review_triage._close_row`` — a guarded status flip, no
    item touched, no trust change) and differs from a manual close in one way
    only: the stamp kind is :data:`WINNER_STAMP_KIND` rather than
    ``triage_keep``, which is what makes the close SELECTABLE by undo. The
    manual disposition rides along as ``note='cluster_winner'``.
    """
    return review_triage._close_row(qid, WINNER_STAMP_KIND, note="cluster_winner")


def invalidate_dead_dup(qid: int) -> bool:
    """Re-queue a reject_duplicate row whose dup_of is PROVABLY dead for fresh triage.

    Not a retirement or resolution: the row stays OPEN. Its stale triage moves to
    ``detail.triage_prior`` (audit preserved, Inv 5, reversible), an
    ``auto_resolution`` stamp records ``triage_invalidated``/``dup_target_dead``,
    and a ``detail.priority`` OBJECT (existence-ordered, never an int) floats it to
    the FRONT of ``pending_triage``. Dropping ``triage`` breaks the deadlock:
    ``candidates`` stops re-selecting it, ``pending_triage`` starts.

    CALLED ONLY FOR A GENUINELY DEAD TARGET (``review_apply_dups.DupSplit.dead``:
    the item is missing or non-active). A live-but-refusable target would come
    back from re-triage with the same verdict forever, and the
    ``reason='dup_target_dead'`` stamp would be a lie about a living item.
    """
    at = datetime.now(UTC).isoformat()
    stamp = {
        "auto_resolution": {"kind": "triage_invalidated", "reason": "dup_target_dead", "at": at},
        "priority": {"reason": "dup_target_dead", "source": "auto_apply", "at": at},
    }
    with transaction() as conn:
        row = conn.execute(
            """
            UPDATE review_queue
            SET detail = ((COALESCE(detail, '{}'::jsonb) - 'triage')
                          || jsonb_build_object('triage_prior', detail->'triage'))
                          || %(stamp)s
            WHERE id = %(qid)s AND resolved = false
              AND COALESCE(detail, '{}'::jsonb) ? 'triage'
            RETURNING id
            """,
            {"stamp": Jsonb(stamp), "qid": qid},
        ).fetchone()
    return row is not None


def mark_triage_unresolvable(qid: int, reason: str) -> bool:
    """Stamp a row TERMINAL for automation. The row stays OPEN, the verdict stands.

    THE MEMORY THAT MAKES A SECOND LAP IMPOSSIBLE. A dead-target row is re-queued
    once; if triage comes back naming the SAME dead target, re-queueing it again
    would start the invalidate -> retriage -> same verdict -> invalidate cycle
    over. So the second time around the row is stamped instead: it keeps its
    ``triage``, is never re-queued, and waits for the owner (it is counted every
    run and rides the nightly digest, so waiting is not the same as being lost).

    Idempotent by guard — an already-stamped row matches nothing and returns
    False — so a row sitting here for weeks is not re-written every night.
    """
    stamp: dict[str, Any] = {
        "auto_resolution": {
            "kind": UNRESOLVABLE_KIND,
            "reason": reason,
            "at": datetime.now(UTC).isoformat(),
        }
    }
    with transaction() as conn:
        row = conn.execute(
            """
            UPDATE review_queue
            SET detail = COALESCE(detail, '{}'::jsonb) || %(stamp)s
            WHERE id = %(qid)s AND resolved = false
              AND COALESCE(detail->'auto_resolution'->>'kind', '') <> %(kind)s
            RETURNING id
            """,
            {"stamp": Jsonb(stamp), "qid": qid, "kind": UNRESOLVABLE_KIND},
        ).fetchone()
    return row is not None

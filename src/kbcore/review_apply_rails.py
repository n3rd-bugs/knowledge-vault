"""kbcore.review_apply_rails — the SAFETY RAILS behind automated reject-apply.

Split out of :mod:`kbcore.review_apply` (300-line ratchet): this half decides
WHICH rows the machine may touch, that half performs the flips. Everything here
is read-only — selection plus three pure predicates (cooling-off, access
exclusion, dup-target liveness) — so a rail can be tested without any write
path, which is exactly how they are pinned in ``tests/test_auto_apply.py``.

See :func:`kbcore.review_apply.auto_apply_rejects` for the owner decision
(2026-08-02) these rails exist to bound.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from kbcore import cluster_util, consolidation
from kbcore.db import connect

# Only these two. 'keep' is excluded BY CONSTRUCTION, not by a filter someone
# could widen later: it is absent from this tuple, and the tuple is what the
# selection query binds. A keep leaves the item active + unverified — exactly
# what quiet expiry produces — so those rows need no automation at all.
AUTO_APPLY_ACTIONS = ("reject_noise", "reject_duplicate")
STAMP_KIND = "auto_applied_reject"
# Rail 2 window. An item recalled this recently is knowledge an agent actually
# used, so the machine stands down and leaves the row for the human.
ACCESS_WINDOW_DAYS = 30
# The item status each action leaves behind — the undo guard, so a reversal can
# never resurrect an item some LATER human action retired for its own reasons.
APPLIED_STATUS = {"reject_noise": "archived", "reject_duplicate": "duplicate"}
# ADD-only (telemetry contract, design review #2 KB 16601): skipped_target_gone
# used to conflate two disjoint rows — a cluster WINNER (its own item is the
# canonical) and a genuinely-dead dup target. They are now separate facts, and
# triage_invalidated counts the dead-target rows re-queued for a fresh verdict.
COUNT_KEYS = (
    "applied_noise",
    "applied_duplicate",
    "skipped_accessed",
    "skipped_cooling",
    "skipped_target_gone",
    "skipped_cluster_winner",
    "triage_invalidated",
    "eligible_remaining",
)


class Row(NamedTuple):
    """One candidate: the review row, its item, and the rail inputs."""

    qid: int
    item_id: int
    action: str
    dup_of: int | None
    triage_at: datetime | None
    accessed: bool


class RunPlan(NamedTuple):
    """What one auto-apply run may do, partitioned so each list is a single fact.

    ``eligible`` rows retire (rails 1-4 passed; surviving duplicates carry the
    CLUSTER canonical in ``dup_of``). ``cluster_winners`` are duplicate rows whose
    OWN item won its cluster — never retired, closed as ``triage_keep`` /
    ``cluster_winner`` (the manual path's disposition since 2026-07-18).
    ``dead_dup_targets`` are duplicate rows whose stored ``dup_of`` is PROVABLY
    dead — never appliable, so they are re-queued for a fresh triage verdict
    instead of being re-counted as skipped_target_gone every night (KB 16601).
    """

    eligible: list[Row]
    cluster_winners: list[Row]
    dead_dup_targets: list[Row]


def parse_at(text: str | None) -> datetime | None:
    """Parse a stored ISO stamp; None when absent or unparseable (fails CLOSED)."""
    if not text:
        return None
    try:
        at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def candidates() -> list[Row]:
    """Open reject-suggested rows whose item is still auto-appliable, oldest first.

    The base predicate IS the first half of the STATE GUARD (item still
    ``active``, source still ``trust='unverified'``, row still unresolved) — it
    is re-checked ATOMICALLY at apply time by ``review_apply._claim``, because a
    human may resolve a row while the batch runs.

    Rows carrying ``auto_resolution.undone_at`` are excluded ENTIRELY (not
    counted as skipped): an owner who ran ``kb review undo-auto`` vetoed that
    application, and a veto tonight's run silently re-applied would be no veto.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT rq.id, rq.item_id,
                   rq.detail->'triage'->>'action',
                   rq.detail->'triage'->>'dup_of',
                   rq.detail->'triage'->>'at',
                   EXISTS (SELECT 1 FROM access_log al
                           WHERE al.item_id = rq.item_id
                             AND al.ts > now() - make_interval(days => %(win)s))
            FROM review_queue rq
            JOIN items i ON i.id = rq.item_id
            LEFT JOIN sources s ON s.id = i.source_id
            WHERE rq.resolved = false
              AND rq.reason = 'unverified_extraction'
              AND i.status = 'active'
              AND COALESCE(s.trust, 'verified') = 'unverified'
              AND rq.detail->'triage'->>'action' = ANY(%(actions)s)
              AND rq.detail->'auto_resolution'->>'undone_at' IS NULL
            ORDER BY rq.id
            """,
            {"win": ACCESS_WINDOW_DAYS, "actions": list(AUTO_APPLY_ACTIONS)},
        ).fetchall()
    out = []
    for qid, item_id, action, dup_txt, at_txt, accessed in rows:
        dup_of = int(dup_txt) if dup_txt and dup_txt.lstrip("-").isdigit() else None
        out.append(Row(qid, item_id, action, dup_of, parse_at(at_txt), bool(accessed)))
    return out


def dup_canonicals(dups: list[Row]) -> tuple[dict[int, int], set[int]]:
    """TARGET LIVENESS → ``(queue_id -> canonical to retire toward, winner queue_ids)``.

    Two steps, both borrowed from the manual apply path so auto and manual agree:

    1. STRICT liveness: the stored ``dup_of`` must parse, must EXIST, must not be
       a ``summary`` (an unstable canonical — reused verbatim via
       ``cluster_util.validate_dup_edges``), must be ``active``, and must not be
       the item itself. Anything else drops the row — it is neither retargetable
       nor a winner, so the caller counts it skipped_target_gone.
    2. CLUSTER canonicalization: pairwise LLM edges are not a DAG (reciprocal
       pairs and chains — KB gotcha 958), so surviving edges are union-found and
       each component retires toward ONE canonical picked by the nightly dedupe's
       rule (``consolidation.item_ranks``: oldest verified, else oldest). A row
       whose own item IS the cluster canonical is NOT retired — this is what makes
       it impossible for a reciprocal A<->B pair to retire BOTH ends.

    The SECOND return is the set of queue ids whose own item won its cluster
    (excluded from the map at step 2). Splitting it out of the drop set lets the
    caller count skipped_cluster_winner separately and close those rows through
    the manual path's ``triage_keep`` disposition rather than re-counting them as
    dead targets forever (design review #2, KB 16601).
    """
    if not dups:
        return {}, set()
    ids = {r.item_id for r in dups} | {r.dup_of for r in dups if r.dup_of}
    with connect() as conn:
        found = conn.execute(
            "SELECT id, type, status FROM items WHERE id = ANY(%s)", (sorted(ids),)
        ).fetchall()
    info = {r[0]: (r[1], r[2]) for r in found}
    rejected: dict[int, str] = {}  # validate_dup_edges' error sink; rows just drop
    edges = cluster_util.validate_dup_edges(
        [(r.qid, r.item_id, {"dup_of": r.dup_of}) for r in dups], info, rejected
    )
    edges = [e for e in edges if e[2] != e[1] and info[e[2]][1] == "active"]
    canonical_of: dict[int, int] = {}
    for comp in cluster_util.clusters((i, d) for _q, i, d in edges):
        actives = [i for i in comp if info.get(i, ("", ""))[1] == "active"]
        if not actives:
            continue
        ranks = consolidation.item_ranks(actives)
        winner = min(actives, key=lambda i: ranks[i])
        for member in comp:
            if member != winner:
                canonical_of[member] = winner
    # Every surviving edge's item is a cluster member with an active canonical, so
    # it is either a non-winner (in ``canonical_of`` -> retargetable) or the winner
    # (absent -> its own row). This partitions the surviving edges cleanly in two.
    retarget = {qid: canonical_of[item] for qid, item, _d in edges if item in canonical_of}
    winners = {qid for qid, item, _d in edges if item not in canonical_of}
    return retarget, winners


def plan_run(cooling_days: int, counts: dict[str, int]) -> RunPlan:
    """Apply rails 1-3 to the candidates; PARTITION them into a :class:`RunPlan`.

    A row stopped by a rail is counted under the FIRST rail that stops it, in
    order (cooling -> accessed), so the counters PARTITION the candidates and can
    never double-count one row. At rail 3 (TARGET LIVENESS) each surviving
    ``reject_duplicate`` row splits three ways (:func:`dup_canonicals`):

    * retargeted onto its CLUSTER canonical → ``eligible`` (retire);
    * its OWN item is the cluster winner → ``skipped_cluster_winner`` /
      ``cluster_winners`` (close as a keep, never retire);
    * its stored ``dup_of`` is provably dead → ``skipped_target_gone`` /
      ``dead_dup_targets`` (re-queue for a fresh triage verdict).
    """
    cutoff = datetime.now(UTC) - timedelta(days=cooling_days)
    survivors: list[Row] = []
    for r in candidates():
        # Rail 1 COOLING-OFF: the owner must have seen this queued in >= N nightly
        # digests before the machine acts. A missing stamp fails closed.
        if r.triage_at is None or r.triage_at > cutoff:
            counts["skipped_cooling"] += 1
        elif r.accessed:  # Rail 2 ACCESS EXCLUSION
            counts["skipped_accessed"] += 1
        else:
            survivors.append(r)
    canon, winner_qids = dup_canonicals([r for r in survivors if r.action == "reject_duplicate"])
    eligible: list[Row] = []
    cluster_winners: list[Row] = []
    dead_dup_targets: list[Row] = []
    for r in survivors:
        if r.action != "reject_duplicate":
            eligible.append(r)
        elif r.qid in canon:  # Rail 3 TARGET LIVENESS — retire onto the cluster canonical
            eligible.append(r._replace(dup_of=canon[r.qid]))
        elif r.qid in winner_qids:
            counts["skipped_cluster_winner"] += 1
            cluster_winners.append(r)
        else:
            counts["skipped_target_gone"] += 1
            dead_dup_targets.append(r)
    return RunPlan(eligible, cluster_winners, dead_dup_targets)

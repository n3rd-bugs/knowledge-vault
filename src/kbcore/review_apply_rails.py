"""kbcore.review_apply_rails — the SAFETY RAILS behind automated reject-apply.

Split out of :mod:`kbcore.review_apply` (300-line ratchet): this half decides
WHICH rows the machine may touch, that half performs the flips. Everything here
is read-only — selection plus the rails (cooling-off, access exclusion, and the
dup partition delegated to :mod:`kbcore.review_apply_dups`) — so a rail can be
tested without any write path, which is exactly how they are pinned in
``tests/test_auto_apply.py``.

See :func:`kbcore.review_apply.auto_apply_rejects` for the owner decision
(2026-08-02) these rails exist to bound.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from kbcore import review_apply_dups
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
# ADD-ONLY (telemetry contract). Two waves of splits, and the rule both times was
# that a key already in the nightly report is never renamed or removed, only
# joined by a more specific sibling:
#   design review #2 (KB 16601) — skipped_target_gone stopped meaning "cluster
#     WINNER" as well as "dead target"; triage_invalidated counts the re-queues;
#   review wfulfrf33 — skipped_target_gone keeps meaning PROVABLY dead (its two
#     sub-counts, triage_invalidated + triage_unresolvable, say what was DONE
#     about those rows), while the alive-but-unappliable rows it also used to
#     absorb get their own three facts: skipped_unresolvable_target (a live
#     summary / self-reference / no target), skipped_cross_project and
#     skipped_linked_alternative (the two jurisdiction walls). Winner closes are
#     now capped, so cluster_winners_closed + cluster_winner_remaining report the
#     split of skipped_cluster_winner.
COUNT_KEYS = (
    "applied_noise",
    "applied_duplicate",
    "skipped_accessed",
    "skipped_cooling",
    "skipped_target_gone",
    "skipped_cluster_winner",
    "triage_invalidated",
    "eligible_remaining",
    "skipped_unresolvable_target",
    "triage_unresolvable",
    "skipped_cross_project",
    "skipped_linked_alternative",
    "cluster_winners_closed",
    "cluster_winner_remaining",
)


class Row(NamedTuple):
    """One candidate: the review row, its item, and the rail inputs.

    ``prior_dup_of`` is the target named by the verdict a PREVIOUS run
    invalidated (``detail.triage_prior``). It is the loop memory: a dead-target
    row whose fresh verdict names the same target again has already been round
    once, and must be stamped terminal rather than re-queued a second time.
    """

    qid: int
    item_id: int
    action: str
    dup_of: int | None
    triage_at: datetime | None
    accessed: bool
    prior_dup_of: int | None = None


class RunPlan(NamedTuple):
    """What one auto-apply run may do, partitioned so each list is a single fact.

    ``eligible`` rows retire (rails 1-4 passed; surviving duplicates carry the
    CLUSTER canonical in ``dup_of``). ``cluster_winners`` are duplicate rows whose
    OWN item won its cluster — never retired, closed as ``cluster_winner_closed``
    (the manual path's disposition since 2026-07-18, made undoable).
    ``dead_dup_targets`` are duplicate rows whose stored ``dup_of`` is PROVABLY
    dead — never appliable, so they are re-queued for a fresh triage verdict
    instead of being re-counted as skipped_target_gone every night (KB 16601).
    ``repeat_dead_targets`` came back from that re-queue naming the SAME dead
    target: re-queueing them again is the livelock, so they are stamped terminal.

    Rows stopped by the two jurisdiction walls appear in NO list — they are
    counted and left strictly alone (no write at all is the disposition).
    """

    eligible: list[Row]
    cluster_winners: list[Row]
    dead_dup_targets: list[Row]
    repeat_dead_targets: list[Row]


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
                             AND al.ts > now() - make_interval(days => %(win)s)),
                   rq.detail->'triage_prior'->>'dup_of'
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
    for qid, item_id, action, dup_txt, at_txt, accessed, prior_txt in rows:
        out.append(
            Row(
                qid,
                item_id,
                action,
                _as_id(dup_txt),
                parse_at(at_txt),
                bool(accessed),
                _as_id(prior_txt),
            )
        )
    return out


def _as_id(text: str | None) -> int | None:
    """A stored jsonb id as an int; None when absent or not an integer (fails CLOSED)."""
    return int(text) if text and text.lstrip("-").isdigit() else None


def plan_run(cooling_days: int, counts: dict[str, int]) -> RunPlan:
    """Apply rails 1-3 to the candidates; PARTITION them into a :class:`RunPlan`.

    A row stopped by a rail is counted under the FIRST rail that stops it, in
    order (cooling -> accessed), so the counters PARTITION the candidates and can
    never double-count one row. At rail 3 each surviving ``reject_duplicate`` row
    splits six ways (:func:`kbcore.review_apply_dups.split_dups`, whose docstring
    holds the reasoning):

    * retargeted onto its CLUSTER canonical → ``eligible`` (retire);
    * its OWN item is the cluster winner → ``skipped_cluster_winner`` /
      ``cluster_winners`` (close as a keep, never retire);
    * the twin linker already ruled on the item → ``skipped_linked_alternative``;
    * the retirement would cross a project wall → ``skipped_cross_project``;
    * ``dup_of`` is alive but permanently refusable → ``skipped_unresolvable_target``;
    * ``dup_of`` is PROVABLY dead → ``skipped_target_gone``, and then either
      ``dead_dup_targets`` (re-queue for a fresh verdict) or, if a re-queue has
      already been tried against this same target, ``repeat_dead_targets``
      (stamp terminal — the second lap is the livelock).

    The three middle cases (linked, cross-project, unresolvable) write NOTHING at
    all and simply leave the row open, so their counter is the only trace a run
    leaves of them — which is why each is its own key rather than a shared one.
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
    split = review_apply_dups.split_dups(
        [(r.qid, r.item_id, r.dup_of) for r in survivors if r.action == "reject_duplicate"]
    )
    eligible: list[Row] = []
    cluster_winners: list[Row] = []
    dead_dup_targets: list[Row] = []
    repeat_dead_targets: list[Row] = []
    for r in survivors:
        if r.action != "reject_duplicate":
            eligible.append(r)
        elif r.qid in split.retarget:  # Rail 3 — retire onto the cluster canonical
            eligible.append(r._replace(dup_of=split.retarget[r.qid]))
        elif r.qid in split.winners:
            counts["skipped_cluster_winner"] += 1
            cluster_winners.append(r)
        elif r.qid in split.linked_alternative:
            counts["skipped_linked_alternative"] += 1
        elif r.qid in split.cross_project:
            counts["skipped_cross_project"] += 1
        elif r.qid in split.unresolvable:
            counts["skipped_unresolvable_target"] += 1
        else:  # PROVABLY dead target — re-queueable exactly once
            counts["skipped_target_gone"] += 1
            if r.prior_dup_of is not None and r.prior_dup_of == r.dup_of:
                counts["triage_unresolvable"] += 1
                repeat_dead_targets.append(r)
            else:
                dead_dup_targets.append(r)
    return RunPlan(eligible, cluster_winners, dead_dup_targets, repeat_dead_targets)

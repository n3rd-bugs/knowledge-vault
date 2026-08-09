"""kbcore.review_apply_dups — dup-target LIVENESS and JURISDICTION for auto-apply.

Split out of :mod:`kbcore.review_apply_rails` when the pre-first-run review
(wfulfrf33) turned one liveness question into six: for every ``reject_duplicate``
candidate this module answers WHICH single thing the row is. Read-only — two
SELECTs and pure union-find — so every partition is pinned without a write path.

THE LIVELOCK THIS PARTITION EXISTS TO BREAK. Auto-apply used to lump every
non-appliable duplicate row into ONE "dead target" bucket and re-queue the whole
bucket for a fresh triage verdict, priority-stamped to the FRONT of the triage
queue. But a row whose ``dup_of`` is ALIVE and permanently refusable — a
``summary`` (an unstable canonical: superseded on every consolidation refresh),
or the item itself — comes back from re-triage with the SAME verdict, is refused
again, and is re-queued again, with a cycle period of the 3-day cooling rail and
no memory anywhere to stop it. Only a PROVABLY DEAD target (missing, or a
non-active item) can be re-queued usefully. Everything else stays OPEN and
untouched, exactly as the manual apply path leaves an unappliable row.

THE TWO JURISDICTION WALLS. The nightly dedupe is same-project by settled design,
and the owner-invoked cross-project twin pass (:mod:`kbcore.twin_crossproject`)
disposes of a cross-project twin by LINKING it (``alternative_to``, no status
flip — nothing leaves any project's surface). Triage suggestions, though, are
cross-project BY CONSTRUCTION: ``graph.similar`` ranks by cosine and knows
nothing about projects. An unwalled auto-apply therefore retires one side of a
pair the owner-invoked linker just certified as "both stay active". Two refusals,
checked in this order so the more specific one wins the counter:

* ALREADY LINKED — the item about to be retired carries an ``alternative_to``
  edge (see :func:`_linked_items` for why that is read broadly). The linker has
  ruled that this item stays active; the machine defers to it.
* CROSS-PROJECT — the retirement itself would cross a project wall. Refused
  whether or not a link exists yet, because making that join is the linker's job.

Neither is a verdict that the suggestion is WRONG, so neither writes anything:
the row stays open and ``kb review apply-triage`` can still apply it under a
human, who is the authority these walls defer to.
"""

from __future__ import annotations

from typing import NamedTuple

from kbcore import cluster_util, consolidation
from kbcore.db import connect

#: Refused as a dup target because it is an UNSTABLE canonical — consolidation
#: supersedes summaries on every refresh. Same refusal as the manual path's
#: ``cluster_util.validate_dup_edges``; the two paths must agree on what a
#: duplicate may retire toward.
UNSTABLE_TARGET_TYPES = ("summary",)
#: The cross-project twin linker's edge (``kbcore.twin_crossproject``).
ALTERNATIVE_TO = "alternative_to"


class DupSplit(NamedTuple):
    """Every ``reject_duplicate`` candidate in EXACTLY ONE bucket.

    ``retarget`` is the only bucket that retires anything; the other five all
    leave their row open, and each is a distinct FACT about why (which is the
    whole point — ``skipped_target_gone`` used to mean all five at once).
    """

    retarget: dict[int, int]  # qid -> cluster canonical to retire toward
    winners: set[int]  # qid whose OWN item won its cluster
    dead: set[int]  # qid whose dup_of is PROVABLY dead (missing / non-active)
    unresolvable: set[int]  # qid whose dup_of is ALIVE but permanently refusable
    cross_project: set[int]  # qid whose retirement would cross a project wall
    linked_alternative: set[int]  # qid whose pair the twin linker already ruled on


def _item_info(ids: list[int]) -> dict[int, tuple[str, str, str]]:
    """``id -> (type, status, project)`` for every id that EXISTS. Absent = missing."""
    if not ids:
        return {}
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, type, status, project FROM items WHERE id = ANY(%s)", (ids,)
        ).fetchall()
    return {int(r[0]): (r[1], r[2], r[3]) for r in rows}


def _linked_items(ids: list[int]) -> set[int]:
    """Which of ``ids`` carry an ``alternative_to`` edge — in EITHER direction, to
    ANY item, whether or not that item is part of this run.

    Two deliberate widenings of "the pair is linked", both because the finding is
    that automation must not retire ONE SIDE of a pair the owner certified as
    "both stay active", and which item it retires ONTO does not change that:

    * DIRECTION-INSENSITIVE — ``graph.link`` stores one row per link and the
      relation is symmetric in meaning, so an a->b edge protects b as it does a;
    * ANY PARTNER, not just this row's retirement target — otherwise a cluster
      whose canonical happens to be a third item would walk straight around the
      linker's ruling.

    An ``alternative_to`` edge is a deliberate curation act (the owner-invoked
    cross-project pass, or an explicit ``kb_link``), so treating it as "hands off,
    this one is the owner's" costs almost nothing: a human can still apply the
    verdict with ``kb review apply-triage``. It is a denial of AUTOMATION, never
    of a human path, which is why it is safe that ``graph.link`` is reachable from
    the capability-gated web write surface.
    """
    if not ids:
        return set()
    with connect() as conn:
        rows = conn.execute(
            "SELECT src_id, dst_id FROM relations "
            "WHERE rel = %s AND src_kind = 'item' AND dst_kind = 'item' "
            "  AND (src_id = ANY(%s) OR dst_id = ANY(%s))",
            (ALTERNATIVE_TO, ids, ids),
        ).fetchall()
    wanted = set(ids)
    return {int(x) for a, b in rows for x in (a, b) if int(x) in wanted}


def _classify_targets(
    dups: list[tuple[int, int, int | None]], info: dict[int, tuple[str, str, str]]
) -> tuple[list[tuple[int, int, int]], set[int], set[int]]:
    """``(edges, dead, unresolvable)`` — pure; the LIVENESS half of :func:`split_dups`.

    The order of the tests is the fix: STATUS is asked before TYPE, so a retired
    summary is DEAD (re-triage will produce a different verdict) while a live one
    is UNRESOLVABLE (re-triage will produce the same one, forever).
    """
    edges: list[tuple[int, int, int]] = []
    dead: set[int] = set()
    unresolvable: set[int] = set()
    for qid, item_id, dup_of in dups:
        target = info.get(dup_of) if dup_of is not None else None
        if dup_of is None or dup_of == item_id:
            unresolvable.add(qid)  # no target at all / itself — alive, never appliable
        elif target is None:
            dead.add(qid)  # the target item does not exist
        elif target[1] != "active":
            dead.add(qid)  # the target was retired
        elif target[0] in UNSTABLE_TARGET_TYPES:
            unresolvable.add(qid)  # ALIVE but refusable — the livelock's fuel
        else:
            edges.append((qid, item_id, dup_of))
    return edges, dead, unresolvable


def split_dups(dups: list[tuple[int, int, int | None]]) -> DupSplit:
    """Partition ``(queue_id, item_id, stored dup_of)`` rows — see the module docstring.

    Liveness first (:func:`_classify_targets`), then CLUSTER canonicalization:
    pairwise LLM edges are not a DAG (reciprocal pairs and chains — KB gotcha
    958), so surviving edges are union-found and each component retires toward
    ONE canonical picked by the nightly dedupe's rule
    (:func:`kbcore.consolidation.item_ranks`: oldest verified, else oldest). A row
    whose own item IS that canonical is a WINNER, never retired — which is what
    makes it impossible for a reciprocal A<->B pair to retire both ends. Only
    then are the two jurisdiction walls applied to the surviving retirements.
    """
    if not dups:
        return DupSplit({}, set(), set(), set(), set(), set())
    ids = {i for _q, i, _d in dups} | {d for _q, _i, d in dups if d is not None}
    info = _item_info(sorted(ids))
    edges, dead, unresolvable = _classify_targets(dups, info)

    canonical_of: dict[int, int] = {}
    for comp in cluster_util.clusters((i, d) for _q, i, d in edges):
        actives = [i for i in comp if info.get(i, ("", "", ""))[1] == "active"]
        if not actives:
            continue
        ranks = consolidation.item_ranks(actives)
        winner = min(actives, key=lambda i: ranks[i])
        for member in comp:
            if member != winner:
                canonical_of[member] = winner

    linked = _linked_items(sorted({i for _q, i, _d in edges if i in canonical_of}))
    retarget: dict[int, int] = {}
    winners: set[int] = set()
    cross_project: set[int] = set()
    linked_alternative: set[int] = set()
    for qid, item_id, _d in edges:
        canon = canonical_of.get(item_id)
        if canon is None:
            winners.add(qid)  # this row's own item is the cluster canonical
        elif item_id in linked:
            linked_alternative.add(qid)  # the twin linker already ruled on this item
        elif info[item_id][2] != info[canon][2]:
            cross_project.add(qid)  # cross-project sameness is the linker's call
        else:
            retarget[qid] = canon
    return DupSplit(retarget, winners, dead, unresolvable, cross_project, linked_alternative)

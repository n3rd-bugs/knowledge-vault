"""kbcore.cluster_util — pure union-find clustering (no DB, no I/O).

Pairwise duplicate suggestions do not form a consistent DAG in practice: the
first live triage run produced 7 reciprocal pairs (A dup-of B AND B dup-of A)
and 5 chains (A→B where B itself retires) out of 39 (KB gotcha 958). Appliers
therefore collapse edges into connected components and pick ONE canonical per
component instead of trusting each edge's stored target. This mirrors the
nightly dedupe's clustering (kbworker.consolidate._canonical_clusters); it
lives here as a pure kbcore helper so kbcore.review_triage can reuse the shape
without importing worker code (kbcore must not depend on kbworker).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def clusters(edges: Iterable[tuple[int, int]]) -> list[set[int]]:
    """Union-find ``edges`` into connected components.

    Every node mentioned in any edge lands in exactly one returned set; a
    self-edge yields a singleton (callers decide whether size-1 components are
    meaningful). Deterministic output order: components sorted by smallest
    member. No edges → no components.
    """
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path halving
            x = parent[x]
        return x

    for a, b in edges:
        parent[find(a)] = find(b)
    comps: dict[int, set[int]] = {}
    for node in parent:
        comps.setdefault(find(node), set()).add(node)
    return sorted(comps.values(), key=min)


def validate_dup_edges(
    dup_rows: list[tuple[int, int, dict[str, Any]]],
    info: dict[int, tuple[str, str]],
    outcomes: dict[int, str],
) -> list[tuple[int, int, int]]:
    """Validate reject_duplicate rows' stored dup_of → ``(qid, item, dup_of)`` edges.

    ``dup_rows`` is ``(queue_id, item_id, triage_dict)``; ``info`` maps item id →
    ``(type, status)`` for every id that exists. Invalid edges — no int dup_of,
    target missing from ``info``, or target type='summary' (an unstable
    canonical: summaries are superseded on every consolidation refresh) — stamp
    an ``'error: …'`` outcome for their row (which stays open, the existing
    invalid-dup_of contract) and never enter the union-find. Pure: no I/O;
    ``outcomes`` is mutated in place.
    """
    edges: list[tuple[int, int, int]] = []
    for qid, item_id, triage in dup_rows:
        dup_of = triage.get("dup_of")
        if isinstance(dup_of, bool) or not isinstance(dup_of, int):
            outcomes[qid] = "error: reject_duplicate suggestion carries no valid dup_of"
        elif dup_of not in info:
            outcomes[qid] = f"error: dup_of item {dup_of} does not exist"
        elif info[dup_of][0] == "summary":
            outcomes[qid] = f"error: dup_of item {dup_of} is a summary (unstable canonical)"
        else:
            edges.append((qid, item_id, dup_of))
    return edges

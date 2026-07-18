"""kbcore.cluster_util — pure union-find + dup-edge validation (no DB, no fixtures).

The DB-facing behavior (which rows close/retire) is covered in
test_review_triage.py; these tests pin the pure helper's contracts.
"""

from __future__ import annotations

from kbcore import cluster_util


def test_clusters_empty_and_single_edge():
    assert cluster_util.clusters([]) == []
    assert cluster_util.clusters([(1, 2)]) == [{1, 2}]


def test_clusters_transitive_reciprocal_and_disjoint():
    # (1,2)+(2,3) chain and (3,2) reciprocal merge into one component; (7,8) stays
    # apart. Output deterministically ordered by smallest member.
    assert cluster_util.clusters([(7, 8), (1, 2), (2, 3), (3, 2)]) == [{1, 2, 3}, {7, 8}]


def test_clusters_self_edge_singleton():
    assert cluster_util.clusters([(5, 5)]) == [{5}]  # callers decide what size 1 means


def test_validate_dup_edges_filters_and_stamps_errors():
    info = {1: ("note", "active"), 2: ("note", "active"), 9: ("summary", "active")}
    outcomes: dict[int, str] = {}
    rows = [
        (10, 1, {"dup_of": 2}),  # valid
        (11, 1, {"dup_of": None}),  # no dup_of at all
        (12, 1, {"dup_of": True}),  # bool is not an item id
        (13, 1, {"dup_of": 404}),  # target does not exist
        (14, 1, {"dup_of": 9}),  # summary target — unstable canonical
    ]
    edges = cluster_util.validate_dup_edges(rows, info, outcomes)
    assert edges == [(10, 1, 2)]  # only the valid edge enters the union-find
    assert outcomes[11] == "error: reject_duplicate suggestion carries no valid dup_of"
    assert outcomes[12] == "error: reject_duplicate suggestion carries no valid dup_of"
    assert outcomes[13] == "error: dup_of item 404 does not exist"
    assert outcomes[14] == "error: dup_of item 9 is a summary (unstable canonical)"
    assert 10 not in outcomes  # valid rows are stamped later, by the applier

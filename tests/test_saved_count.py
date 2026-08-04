"""Per-row ``saved_count`` attribution for coalesced groups, and its interaction
with the zero-yield ``requeue_extracted`` recovery tool.

THE BUG: ``mark_group_extracted(ids, saved, rejected)`` wrote the group TOTAL to
the ``saved_count`` column of EVERY row in the group, so ``sum(saved_count)``
multiplied by the coalesce factor (a 648-item session reported 26,972 against
~42 rows). The honest fix attributes the total to ONE row (the first id) and 0
to the rest, so the column sums to the real total.

THE CONSTRAINT: ``requeue_extracted`` re-opens ZERO-YIELD extracted rows, keyed
off ``saved_count = 0``. Attributing the total to one row leaves the other rows
of a PRODUCTIVE group reading 0 — so the zero-yield predicate is now GROUP-AWARE
(a row is only re-openable when no row in its (project, source_uri) group
produced anything). These tests pin both halves: the column sums honestly, and a
mined session is never made to look re-drainable.
"""

from __future__ import annotations

from kbcore import ingest_queue
from kbcore.db import connect


def _coalesced_group(project: str, uri: str, n: int) -> list[int]:
    """Enqueue ``n`` rows sharing one non-degenerate ``source_uri`` — the exact
    shape the drain coalesces into a single extraction. Content differs per row so
    the dedupe UNIQUE (kind, project, content_hash) does not collapse them."""
    ids: list[int] = []
    for i in range(n):
        res = ingest_queue.enqueue(
            project, f"chunk {i}: distinct body for {uri}", kind="session", source_uri=uri
        )
        assert res.id is not None
        ids.append(res.id)
    return ids


def _saved_counts(ids: list[int]) -> dict[int, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, saved_count FROM ingest_queue WHERE id = ANY(%s)", (ids,)
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def _statuses(ids: list[int]) -> dict[int, str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, status FROM ingest_queue WHERE id = ANY(%s)", (ids,)
        ).fetchall()
    return {r[0]: r[1] for r in rows}


# ------------------------------------------------- the column sums honestly


def test_group_total_is_attributed_to_one_row_not_every_row(kb):
    """The whole point of the fix: ``sum(saved_count)`` over a coalesced group is
    the real total, not total*rows."""
    ids = _coalesced_group("sc-sum", "session:s1", 5)
    ingest_queue.mark_group_extracted(ids, saved=648, rejected=12)

    counts = _saved_counts(ids)
    assert sum(counts.values()) == 648, "sum(saved_count) was multiplied by the coalesce factor"
    assert counts[ids[0]] == 648, "the total belongs on the first id"
    assert all(counts[i] == 0 for i in ids[1:]), "sibling rows must not each carry the total"

    with connect() as conn:
        rejected = dict(
            conn.execute(
                "SELECT id, rejected_count FROM ingest_queue WHERE id = ANY(%s)", (ids,)
            ).fetchall()
        )
    assert sum(rejected.values()) == 12, "rejected_count must be attributed the same way"
    assert rejected[ids[0]] == 12 and all(rejected[i] == 0 for i in ids[1:])


def test_the_whole_group_still_flips_atomically(kb):
    """Attribution changes the counts, never the all-or-nothing status flip."""
    ids = _coalesced_group("sc-atomic", "session:s2", 4)
    ingest_queue.mark_group_extracted(ids, saved=3, rejected=0)
    assert set(_statuses(ids).values()) == {"extracted"}


def test_a_single_row_group_gets_the_whole_total(kb):
    """A group of one (a memory dump, or a session windowed but one queue row) is
    unchanged: its single row still carries the full total."""
    res = ingest_queue.enqueue("sc-one", "a standalone submission", kind="memory")
    assert res.id is not None
    ingest_queue.mark_group_extracted([res.id], saved=7, rejected=2)
    assert _saved_counts([res.id]) == {res.id: 7}


# --------------------------------- interaction with requeue_extracted (Fix 1 constraint)


def test_a_productive_group_has_NO_row_eligible_for_requeue(kb):
    """The whole reason the zero-yield predicate had to become group-aware: the
    sibling rows of a mined session read saved_count=0 after the attribution fix,
    and re-opening any of them would re-assemble and DUPLICATE the session."""
    ids = _coalesced_group("sc-prod", "session:mined", 6)
    ingest_queue.mark_group_extracted(ids, saved=5, rejected=1)

    reopened = ingest_queue.requeue_extracted(project="sc-prod", reason="test")
    assert reopened == [], "a productive group's rows must never be re-opened"
    assert set(_statuses(ids).values()) == {"extracted"}, "no row of a mined session moved"


def test_a_true_zero_yield_group_still_has_every_row_eligible(kb):
    """The recovery tool must still reach content that genuinely produced nothing
    (the coalescing bug left 137 such rows across six projects)."""
    ids = _coalesced_group("sc-zero", "session:empty", 4)
    ingest_queue.mark_group_extracted(ids, saved=0, rejected=0)

    reopened = ingest_queue.requeue_extracted(project="sc-zero", reason="test")
    assert set(reopened) == set(ids), "a genuinely zero-yield group must be fully re-openable"
    assert set(_statuses(ids).values()) == {"pending"}


def test_requeue_reopens_only_the_zero_yield_group_when_both_are_present(kb):
    """Productive and zero-yield groups in the SAME project: only the empty one is
    re-opened, and the mined one is untouched."""
    productive = _coalesced_group("sc-mixed", "session:kept", 3)
    empty = _coalesced_group("sc-mixed", "session:gone", 3)
    ingest_queue.mark_group_extracted(productive, saved=9, rejected=0)
    ingest_queue.mark_group_extracted(empty, saved=0, rejected=0)

    reopened = ingest_queue.requeue_extracted(project="sc-mixed", reason="test")
    assert set(reopened) == set(empty), "only the zero-yield group should re-open"

    statuses = _statuses(productive + empty)
    assert all(statuses[i] == "extracted" for i in productive), "the mined group must not move"
    assert all(statuses[i] == "pending" for i in empty)


def test_a_degenerate_uri_zero_row_is_still_reachable(kb):
    """Rows with an absent/degenerate source_uri were extracted as groups of one,
    so they have no siblings and the group-aware check reduces to the plain per-row
    predicate — a zero-yield one is still re-openable even alongside an unrelated
    productive row in the same project."""
    zero = ingest_queue.enqueue("sc-degen", "produced nothing", kind="session").id
    good = ingest_queue.enqueue("sc-degen", "produced knowledge", kind="session").id
    assert zero is not None and good is not None
    ingest_queue.mark_extracted(zero, saved=0, rejected=0)
    ingest_queue.mark_extracted(good, saved=3, rejected=0)

    reopened = ingest_queue.requeue_extracted(project="sc-degen", reason="test")
    assert zero in reopened and good not in reopened

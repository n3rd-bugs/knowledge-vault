"""Wiring of the recency guard into the nightly counts, the digest and the review
list (kbworker.consolidate / kbworker.digest / kbcore.render). Real DB (kb_test);
the evidence judge is a fixed suggestion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kbcore import db, evidence, llm
from kbcore.consolidation import record_contradiction
from kbcore.ingest import ingest
from kbcore.models import ReviewEntry
from kbcore.render import render_review
from kbworker import consolidate, digest

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def test_the_nightly_counts_and_digest_carry_held_pairs(kb, monkeypatch):
    old = ingest("wire", "qa", "old eval", "no regression", uri="wo", trust="unverified")
    new = ingest("wire", "qa", "new eval", "below the floor", uri="wn", trust="unverified")
    with db.transaction() as conn:
        conn.execute("UPDATE items SET created_at = %s WHERE id = %s", (T0, old))
        conn.execute(
            "UPDATE items SET created_at = %s WHERE id = %s", (T0 + timedelta(days=27), new)
        )
    assert record_contradiction(old, new) is True
    sugg = {"action": "supersede", "keep": old, "retire": new, "rule": "evidence: test"}
    detail = {"suggested_resolution": sugg}
    monkeypatch.setattr(evidence, "gather_evidence", lambda a, b: {"a_id": a, "b_id": b, **detail})
    monkeypatch.setattr(llm, "items_contradict", lambda *a: False)
    monkeypatch.setattr(llm, "summarize_items", lambda *a: "")
    consolidate.main()
    counts = consolidate.main()
    assert (counts["auto_resolve_held"], counts["auto_resolve_held_open"]) == (1, 1)
    assert counts["evidence_auto_resolved"] == 0
    assert "auto-resolve held: 1 (+1 tonight; would retire the newer item)" in digest.digest_lines(
        counts
    )
    counts = consolidate.main()
    assert (counts["auto_resolve_held"], counts["auto_resolve_held_open"]) == (0, 1)
    assert "auto-resolve held: 1 (would retire the newer item)" in digest.digest_lines(counts)
    assert "auto-resolve held" not in "\n".join(digest.digest_lines({"queued_drained": 0}))


def test_the_review_list_shows_why_a_pair_is_held():
    e = ReviewEntry(
        queue_id=7,
        reason="contradiction",
        item_id=1,
        detail={
            "suggested_resolution": {"keep": 1, "retire": 2},
            "suggestion_streak": 2,
            "auto_resolve_hold": {
                "kind": "newer_retire",
                "reason": "held: would retire the newer item (+26.8 d) - owner decision",
            },
        },
    )
    out = render_review([e])
    assert "suggested: supersede item 2" in out
    assert "    held: would retire the newer item (+26.8 d) - owner decision" in out

"""KB 57973: contradiction rows with a retired side close in the nightly moot sweep.

The WP-1 sweep (``review_auto.close_moot_rows``) covered ``unverified_extraction``
rows only, and nothing else closed a contradiction row whose side was retired outside
the evidence step's own pass: ``evidence.open_contradiction_rows`` skips any pair that
is not active on both sides. On 2026-11-02 that was 269 of the 507 open contradiction
rows. The rule is ``review_recency.pair_moot``'s (either side not active = moot); an
owner veto stays open. Everything asserted here is a status flip (Invariant 5).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from kbcore import consolidation, db, evidence, llm, review, review_moot
from kbcore.config import settings
from kbcore.ingest import ingest
from kbcore.resend_repair_items import archive_items
from kbcore.resend_repair_undo import undo_items
from kbcore.review import queue_review
from kbcore.review_apply import undo_auto_applied
from kbcore.review_apply_rails import STAMP_KIND
from kbcore.review_recency import HOLD_NEWER, HOLD_RESTORED
from kbcore.review_restore import restore_auto_resolved
from kbcore.supersede import supersede
from kbworker import consolidate


def _row(qid: int) -> tuple[bool, dict]:
    with db.connect() as conn:
        r = conn.execute(
            "SELECT resolved, detail FROM review_queue WHERE id = %s", (qid,)
        ).fetchone()
    return bool(r[0]), (r[1] if isinstance(r[1], dict) else {})


def _item(tag: str) -> int:
    return ingest("moot", "note", f"note {tag}", f"the body of {tag}", uri=f"moot:{tag}")


def _pair(a: int, b: int, **extra) -> int:
    """A contradiction row in the shape consolidation.record_contradiction queues."""
    return queue_review(a, "contradiction", {"a_id": a, "b_id": b, **extra})


def _retire(item_id: int) -> None:
    supersede(item_id, new_item_id=_item(f"winner-{item_id}"), reason="test: retired")


def test_each_retirement_path_makes_a_contradiction_moot(kb):
    keeper = _item("keeper")
    sup, dup, stale, other = (_item(t) for t in ("sup", "dup", "stale", "other"))
    q_sup = _pair(keeper, sup, suggested_resolution={"keep": keeper, "retire": sup})
    q_dup = _pair(dup, keeper)  # the retired side can be either one
    q_stale = _pair(keeper, stale)
    q_live = _pair(keeper, other)

    _retire(sup)
    assert consolidation.mark_duplicate(dup, keeper) is True  # folded into the other side
    with db.transaction() as conn:  # backdate so ONLY this item goes stale (kb_test-owned)
        conn.execute(
            "UPDATE items SET created_at = now() - interval '400 days', "
            "updated_at = now() - interval '400 days' WHERE id = %s",
            (stale,),
        )
    assert consolidation.archive_stale(datetime.now(UTC) - timedelta(days=180)) == 1

    # The gap: the evidence step never sees a pair with a retired side.
    assert {r[0] for r in evidence.open_contradiction_rows()} == {q_live}

    assert review_moot.close_moot_contradictions() == 3
    for qid, retired_side, status in (
        (q_sup, "b_status", "superseded"),
        (q_dup, "a_status", "duplicate"),
        (q_stale, "b_status", "archived"),
    ):
        resolved, detail = _row(qid)
        assert resolved is True
        stamp = detail["auto_resolution"]
        assert stamp["kind"] == "item_retired"
        assert stamp[retired_side] == status
        assert stamp["a_status" if retired_side == "b_status" else "b_status"] == "active"
        assert "a_id" in detail and "b_id" in detail, "jsonb merge keeps the pair"
    assert _row(q_sup)[1]["suggested_resolution"] == {"keep": keeper, "retire": sup}
    assert _row(q_live)[0] is False, "both sides active: still a live question"
    assert review_moot.close_moot_contradictions() == 0, "idempotent"


def test_a_duplicate_whose_canonical_pair_was_never_compared_stays_open(kb):
    """Closing would end the question for good: the scan never offers a pair outside its
    lead-chunk band, and nothing re-queues it. It stays open, counted, until settled."""
    x, dup, canonical = _item("x"), _item("dup"), _item("canonical")
    q = _pair(x, dup)
    assert consolidation.mark_duplicate(dup, canonical) is True

    assert review_moot.close_moot_contradictions() == 0
    assert _row(q)[0] is False
    assert review_moot.open_moot_contradictions() == 1, "the gauge shows it to the owner"

    consolidation.record_pair_judgement(x, canonical, False)  # now the scan has asked
    assert review_moot.close_moot_contradictions() == 1
    assert _row(q)[1]["auto_resolution"]["b_status"] == "duplicate"


def test_a_duplicate_whose_canonical_pair_has_its_own_row_closes(kb):
    x, dup, canonical = _item("x"), _item("dup"), _item("canonical")
    q = _pair(x, dup)
    _pair(canonical, x)  # the canonical pair is tracked, in either orientation
    assert consolidation.mark_duplicate(dup, canonical) is True

    assert review_moot.close_moot_contradictions() == 1
    assert _row(q)[0] is True


def test_an_owner_veto_stays_open_and_a_machine_hold_does_not(kb):
    a, b, c = _item("a"), _item("b"), _item("c")
    vetoed = _pair(a, b, auto_resolve_hold={"kind": HOLD_RESTORED})
    held = _pair(a, c, auto_resolve_hold={"kind": HOLD_NEWER})
    _retire(b)
    _retire(c)

    assert review_moot.close_moot_contradictions() == 1
    assert _row(vetoed)[0] is False, "the owner restored a side: never close it behind them"
    assert _row(held)[0] is True, "a recency hold is the machine's, and the pair is moot"


def test_the_nightly_runs_it_as_its_own_step_and_reports_the_gauge(kb, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "kb_raw", tmp_path / "kbraw")
    monkeypatch.setattr(settings, "kb_eval_in_consolidation", False)
    monkeypatch.setattr(llm, "items_contradict", lambda *a, **k: False)
    a, b, c = _item("a"), _item("b"), _item("c")
    extraction = queue_review(c, "unverified_extraction", {"rejected_count": 0})
    _pair(a, b)
    _pair(a, c, auto_resolve_hold={"kind": HOLD_RESTORED})
    _retire(b)
    _retire(c)
    assert review_moot.open_moot_contradictions() == 2

    counts = consolidate.main()

    assert counts["review_moot_closed"] == 1, "the extraction sweep's key keeps its meaning"
    assert counts["moot_contradictions_closed"] == 1
    assert _row(extraction)[0] is True
    assert counts["moot_contradictions_open"] == 1, "only the owner veto is left"


# --- an owner path that brings a retired side back reopens what the sweep closed ---


def _qid(a: int, b: int) -> int:
    with db.connect() as conn:
        return conn.execute(
            "SELECT id FROM review_queue WHERE reason = 'contradiction' "
            "AND (detail->>'a_id')::bigint = %s AND (detail->>'b_id')::bigint = %s",
            (a, b),
        ).fetchone()[0]


def _status(item_id: int) -> str:
    with db.connect() as conn:
        return conn.execute("SELECT status FROM items WHERE id = %s", (item_id,)).fetchone()[0]


def _evidence_suggests(monkeypatch, on: tuple[int, int], keep: int, retire: int) -> None:
    """Evidence suggests retiring ``retire`` on the row ``on`` = (a_id, b_id) only."""

    def gather(x: int, y: int) -> dict:
        sugg = {"action": "supersede", "keep": keep, "retire": retire, "rule": "evidence: test"}
        return {"a_id": x, "b_id": y, "suggested_resolution": sugg if (x, y) == on else None}

    monkeypatch.setattr(evidence, "gather_evidence", gather)
    monkeypatch.setattr(consolidate, "EVIDENCE_PAIR_CAP", 10)
    monkeypatch.setattr(settings, "kb_auto_resolve_newer_gap_days", 1000.0)
    monkeypatch.setattr(settings, "kb_auto_resolve_evidence", True)


def test_a_restore_reopens_swept_rows_and_vetoes_a_swept_twin(kb, monkeypatch):
    k, r, x = _item("k"), _item("r"), _item("x")
    for a, b in ((k, r), (r, k), (r, x)):  # (r, k) is the twin: its own relation + row
        assert consolidation.record_contradiction(a, b) is True
        consolidation.record_pair_judgement(a, b, True)
    q_pair, q_twin, q_rx = _qid(k, r), _qid(r, k), _qid(r, x)
    _evidence_suggests(monkeypatch, on=(k, r), keep=k, retire=r)
    consolidate._evidence()
    consolidate._evidence()  # the streak reaches 2 on (k, r): r is superseded
    assert _status(r) == "superseded"
    assert review_moot.close_moot_contradictions() == 2, "the hidden twin and (r, x)"

    restore_auto_resolved(r, apply=True)

    resolved, detail = _row(q_rx)
    assert resolved is False and "undone_at" in detail["auto_resolution"]
    assert "auto_resolve_hold" not in detail, "a third-party row needs no veto"
    assert q_rx in {row[0] for row in evidence.open_contradiction_rows()}
    for qid in (q_pair, q_twin):  # reopened BEFORE the twin veto, so it is vetoed too
        resolved, detail = _row(qid)
        assert resolved is False and detail["auto_resolve_hold"]["kind"] == HOLD_RESTORED
    _evidence_suggests(monkeypatch, on=(r, k), keep=k, retire=r)  # now via the twin
    for _ in range(3):
        consolidate._evidence()
    assert _status(r) == "active", "an unvetoed twin would have re-retired r"


def test_the_undo_paths_reopen_swept_rows_but_not_a_still_moot_one(kb):
    x, y, z = _item("x"), _item("y"), _item("z")
    q_xy, q_yz = _pair(x, y), _pair(y, z)
    # auto-apply's reject (archives x), then the owner's undo
    qx = queue_review(x, "unverified_extraction", {"rejected_count": 0})
    stamp = {"kind": STAMP_KIND, "action": "reject_noise", "at": datetime.now(UTC).isoformat()}
    with db.transaction() as conn:
        conn.execute(
            "UPDATE review_queue SET detail = detail || %s WHERE id = %s",
            (Jsonb({"auto_resolution": stamp}), qx),
        )
    review.reject(qx)
    archive_items([z], reason="test: resend-dups", archived={})
    assert review_moot.close_moot_contradictions() == 2
    assert undo_auto_applied(datetime.now(UTC) - timedelta(hours=1)) == [qx]
    assert _row(q_xy)[0] is False, "x is back: x-y is a live question again"
    assert _row(q_yz)[0] is True, "z is still archived: y-z stays moot"
    assert undo_items([z]) == (1, 0)
    assert _row(q_yz)[0] is False, "the resend-dups undo brings y-z back too"


def test_the_reopen_touches_only_the_sweeps_own_closes(kb):
    x, y = _item("x"), _item("y")
    q_xy = _pair(x, y)
    with db.transaction() as conn:  # closed by anything else (a human dismissal, ...)
        conn.execute(
            "UPDATE review_queue SET resolved = true, detail = detail || %s WHERE id = %s",
            (Jsonb({"auto_resolution": {"kind": "dismissed"}}), q_xy),
        )
        assert review_moot.reopen_unmooted_in(conn, [x, y]) == 0
    assert _row(q_xy)[0] is True

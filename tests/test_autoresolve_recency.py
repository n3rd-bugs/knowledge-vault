"""The recency guard on nightly evidence auto-resolution (kbcore.review_recency).

Owner-verified defect (2026-10-24): 46 of 92 auto-resolutions in 60 days retired
the NEWER item of a contradiction pair in favour of an older one, because
corroboration accrues to whatever has been in the KB longer. Real DB (kb_test);
the evidence judge is replaced by a fixed suggestion, as the existing evidence
tests do, so each "night" is one consolidate._evidence() pass.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kbcore import db, evidence, review
from kbcore.config import Settings, settings
from kbcore.consolidation import record_contradiction
from kbcore.ingest import ingest
from kbworker import consolidate, evidence_step

# The supersede_reason the live auto-resolutions carry (kbcore.review_recency).
AUTO_RESOLVE_REASON = "auto: evidence margin stable across nightly refreshes"
T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
GAP_EVAL = timedelta(days=26, hours=20)  # KB 39395 (09-20) vs KB 50850 (10-16)


def _set_window(monkeypatch, days: float) -> None:
    """Set the knob — asserting it EXISTS first: monkeypatch's missing-attribute
    error prints the whole Settings repr, secrets included (seen on HEAD)."""
    if "kb_auto_resolve_newer_gap_days" not in type(settings).model_fields:
        pytest.fail("Settings has no kb_auto_resolve_newer_gap_days knob")
    monkeypatch.setattr(settings, "kb_auto_resolve_newer_gap_days", days)


def _pair(gap: timedelta, tag: str = "x") -> tuple[int, int, int]:
    """``(old, new, queue_id)``: two unverified items, ``new`` created ``gap``
    after ``old``, with their contradiction row queued."""
    old = ingest(
        "recency",
        "qa",
        f"eval {tag} 09-20",
        f"hit@5 0.9836, MRR 0.8607, no regression {tag}",
        uri=f"old-{tag}",
        trust="unverified",
    )
    new = ingest(
        "recency",
        "qa",
        f"eval {tag} 10-16",
        f"hit@5 0.9508 below the 0.97 floor {tag}",
        uri=f"new-{tag}",
        trust="unverified",
    )
    with db.transaction() as conn:
        conn.execute("UPDATE items SET created_at = %s WHERE id = %s", (T0, old))
        conn.execute("UPDATE items SET created_at = %s WHERE id = %s", (T0 + gap, new))
    assert record_contradiction(old, new) is True
    with db.connect() as conn:
        qid = conn.execute(
            "SELECT id FROM review_queue WHERE reason = 'contradiction' "
            "AND (detail->>'a_id')::bigint = %s",
            (old,),
        ).fetchone()[0]
    return old, new, qid


@pytest.fixture()
def nights(kb, monkeypatch):
    """``night(keep, retire)`` runs one evidence pass whose judge always suggests
    retiring ``retire``; ``calls`` records every pair actually judged."""
    calls: list[tuple[int, int]] = []
    sugg: dict = {}

    def gather(a: int, b: int) -> dict:
        calls.append((a, b))
        return {
            "a_id": a,
            "b_id": b,
            "evidence_updated": datetime.now(UTC).isoformat(),
            "suggested_resolution": dict(sugg),
        }

    monkeypatch.setattr(evidence, "gather_evidence", gather)
    monkeypatch.setattr(consolidate, "EVIDENCE_PAIR_CAP", 10)

    def night(keep: int, retire: int) -> tuple[int, int, int]:
        sugg.clear()
        sugg.update(action="supersede", keep=keep, retire=retire, rule="evidence: test")
        return consolidate._evidence()

    night.calls = calls  # type: ignore[attr-defined]
    return night


def _item(item_id: int) -> tuple:
    with db.connect() as conn:
        return conn.execute(
            "SELECT status, superseded_by, supersede_reason, valid_to, body, meta "
            "FROM items WHERE id = %s",
            (item_id,),
        ).fetchone()


def _row(qid: int) -> tuple[bool, dict]:
    with db.connect() as conn:
        resolved, detail = conn.execute(
            "SELECT resolved, detail FROM review_queue WHERE id = %s", (qid,)
        ).fetchone()
    return resolved, detail


def _rows() -> int:
    with db.connect() as conn:
        return conn.execute("SELECT count(*) FROM review_queue").fetchone()[0]


def test_the_default_window_is_half_a_day():
    assert Settings.model_fields["kb_auto_resolve_newer_gap_days"].default == 0.5


def test_retiring_the_older_item_still_auto_resolves(nights):
    old, new, qid = _pair(GAP_EVAL)
    assert nights(new, old) == (1, 0, 0)  # night 1: streak 1, no action
    assert nights(new, old) == (1, 1, 0)  # night 2: today's behaviour
    status, by, reason, *_ = _item(old)
    assert (status, by, reason) == ("superseded", new, AUTO_RESOLVE_REASON)
    resolved, detail = _row(qid)
    assert resolved is True
    assert detail["auto_resolution"]["kind"] == "evidence_supersede"
    assert (detail["auto_resolution"]["keep"], detail["auto_resolution"]["retire"]) == (new, old)
    assert evidence_step._EVIDENCE_STATS == {"auto_resolve_held": 0, "auto_resolve_held_open": 0}


def test_retiring_the_newer_item_beyond_the_window_is_held(nights):
    old, new, qid = _pair(GAP_EVAL)
    nights(old, new)
    assert nights(old, new) == (1, 0, 0), "a stable suggestion that must NOT execute"
    assert _item(new)[0] == "active", "the newer observation was retired automatically"
    resolved, detail = _row(qid)
    assert resolved is False, "the pair stays pending for the owner"
    hold = detail["auto_resolve_hold"]
    assert hold["kind"] == "newer_retire" and (hold["keep"], hold["retire"]) == (old, new)
    assert hold["gap_days"] == pytest.approx(26.833, abs=1e-3)
    assert hold["reason"] == "held: would retire the newer item (+26.8 d) - owner decision"
    assert hold["reason"].isascii()
    assert detail["suggested_resolution"]["retire"] == new, "the suggestion stays visible"
    assert detail["suggestion_streak"] == 2
    assert evidence_step._EVIDENCE_STATS == {"auto_resolve_held": 1, "auto_resolve_held_open": 1}

    # Night 3 (and every night after): idempotent — skipped before batching, so
    # no judge call, no streak churn, no rewritten detail, no new row.
    judged, rows = len(nights.calls), _rows()
    assert nights(old, new) == (0, 0, 0)
    assert len(nights.calls) == judged, "a held pair must not be re-judged nightly"
    assert _rows() == rows
    assert _row(qid) == (False, detail), "byte-identical: no streak churn, first 'at' stands"
    assert _item(new)[0] == "active"
    assert evidence_step._EVIDENCE_STATS == {"auto_resolve_held": 0, "auto_resolve_held_open": 1}


def test_a_contemporaneous_newer_retire_still_auto_resolves(nights):
    """Two extractions of one working session (the live twins sat 2 s - 7.4 h
    apart): evidence may still settle them."""
    old, new, qid = _pair(timedelta(hours=7, minutes=24))
    nights(old, new)
    assert nights(old, new) == (1, 1, 0)
    assert _item(new)[:2] == ("superseded", old)
    assert _row(qid)[0] is True


def test_the_window_at_zero_holds_every_newer_retire(nights, monkeypatch):
    _set_window(monkeypatch, 0.0)
    old, new, qid = _pair(timedelta(seconds=3), tag="twin")
    nights(old, new)
    assert nights(old, new) == (1, 0, 0)
    assert _item(new)[0] == "active" and _row(qid)[0] is False
    assert _row(qid)[1]["auto_resolve_hold"]["reason"].startswith(
        "held: would retire the newer item (+0.0 d)"
    )
    # ...while retiring the OLDER side is untouched by the knob.
    o2, n2, q2 = _pair(timedelta(seconds=3), tag="twin2")
    nights(n2, o2)
    nights(n2, o2)
    assert _item(o2)[:2] == ("superseded", n2) and _row(q2)[0] is True


def test_raising_the_window_releases_a_held_pair(nights, monkeypatch):
    old, new, qid = _pair(GAP_EVAL)
    nights(old, new)
    nights(old, new)
    assert _row(qid)[0] is False
    _set_window(monkeypatch, 30.0)  # owner's policy
    assert nights(old, new) == (1, 1, 0)
    assert _item(new)[:2] == ("superseded", old)


def test_the_kbcore_guard_is_idempotent_on_its_own(kb):
    old, new, qid = _pair(GAP_EVAL)
    assert review.auto_resolve_contradiction(qid, old, new) is None
    first = _row(qid)
    assert review.auto_resolve_contradiction(qid, old, new) is None
    assert _row(qid) == first, "the hold is stamped once"
    assert _item(new)[0] == "active" and first[0] is False
    assert review.auto_resolve_contradiction(qid, new, old) == old, "older-retire acts"


def test_a_gap_just_past_the_window_stays_held(nights):
    """Within a second of the boundary the stored gap must still test as held,
    or the row would be released and re-held every night."""
    old, new, qid = _pair(timedelta(hours=12, seconds=1), tag="edge")
    nights(old, new)
    nights(old, new)
    assert _row(qid)[1]["auto_resolve_hold"]["gap_days"] > 0.5
    judged = len(nights.calls)
    assert nights(old, new) == (0, 0, 0) and len(nights.calls) == judged
    assert evidence_step._EVIDENCE_STATS["auto_resolve_held"] == 0

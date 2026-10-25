"""The PRECISION GATE on nightly evidence auto-resolution (kbcore.review_judge_gate)
and the contradiction seat it rides (KB_CONTRADICTION_JUDGE_MODEL).

Measured defect (2026-10-25): of the 92 pairs the nightly auto-resolved, a blind
two-labeler ground truth found only 42 real conflicts — the rest were complementary
facts or the same fact twice, and automation retired one side of each. Real DB
(kb_test); the evidence judge is a fixed suggestion (as the recency tests do) and the
contradiction judge is a recorded stub, so each "night" is one consolidate._evidence().
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from kbcore import (
    consolidation,
    db,
    evidence,
    llm,
    review,
    review_judge_gate,
    review_recency,
    twin_judge,
)
from kbcore.config import Settings, settings
from kbcore.consolidation import record_contradiction
from kbcore.errors import AlreadySuperseded, ExtractorUnavailable
from kbcore.ingest import ingest
from kbworker import consolidate, digest, evidence_step, tail_ops
from kbworker.digest_recency_lines import judge_held_line

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
SEAT = "bonsai-2:27b"


def _pair(tag: str) -> tuple[int, int, int]:
    """``(old, new, queue_id)``: two unverified items a day apart, row queued."""
    old = ingest("gate", "gotcha", f"bug {tag}", f"the rail stays off after UVLO {tag}",
                 uri=f"old-{tag}", trust="unverified")  # fmt: skip
    new = ingest("gate", "decision", f"fix {tag}", f"rewrite register 0x2C bit 5 {tag}",
                 uri=f"new-{tag}", trust="unverified")  # fmt: skip
    with db.transaction() as conn:
        conn.execute("UPDATE items SET created_at = %s WHERE id = %s", (T0, old))
        conn.execute(
            "UPDATE items SET created_at = %s WHERE id = %s", (T0 + timedelta(days=1), new)
        )
    assert record_contradiction(old, new) is True
    with db.connect() as conn:
        qid = conn.execute(
            "SELECT id FROM review_queue WHERE reason = 'contradiction' "
            "AND (detail->>'a_id')::bigint = %s",
            (old,),
        ).fetchone()[0]
    return old, new, qid


@pytest.fixture()
def events() -> list:
    """Every gather and judge call of a test, in order (the two-phase check)."""
    return []


@pytest.fixture()
def gate(kb, monkeypatch, events):
    """Gate on, contradiction seat = SEAT; ``gate.verdict`` is what the stub judge
    answers (True / False / None = no verdict, or an exception to raise);
    ``gate.judged`` records every judged ``(keep, retire)``."""
    monkeypatch.setattr(settings, "kb_auto_resolve_judge_gate", True)
    monkeypatch.setattr(settings, "kb_contradiction_judge_model", SEAT)
    state: dict = {"verdict": True, "judged": []}

    def judge(keep: int, retire: int) -> bool | None:
        state["judged"].append((keep, retire))
        events.append(("judge", keep))
        v = state["verdict"]
        if callable(v):
            v = v()
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(review_judge_gate, "verdict", judge)
    return state


@pytest.fixture()
def nights(kb, monkeypatch, events):
    """``night(mode)`` = one evidence pass whose refresh suggests, for every open row,
    retiring its older item (``"retire_old"``, kept = the newer) or its newer one;
    ``night.mode_for[a_id]`` overrides the mode for one pair."""
    calls: list[tuple[int, int]] = []
    state: dict = {"mode": "retire_old", "mode_for": {}}

    def gather(a: int, b: int) -> dict:  # a = the older item (record_contradiction order)
        calls.append((a, b))
        events.append(("gather", a))
        mode = state["mode_for"].get(a, state["mode"])
        keep, retire = (b, a) if mode == "retire_old" else (a, b)
        sugg = {"action": "supersede", "keep": keep, "retire": retire, "rule": "evidence: test"}
        return {"a_id": a, "b_id": b, "evidence_updated": datetime.now(UTC).isoformat(),
                "suggested_resolution": sugg}  # fmt: skip

    monkeypatch.setattr(evidence, "gather_evidence", gather)
    monkeypatch.setattr(consolidate, "EVIDENCE_PAIR_CAP", 10)

    def night(mode: str = "retire_old") -> tuple[int, int, int]:
        state["mode"] = mode
        return consolidate._evidence()

    night.calls = calls  # type: ignore[attr-defined]
    night.mode_for = state["mode_for"]  # type: ignore[attr-defined]
    return night


def _status(item_id: int) -> str:
    with db.connect() as conn:
        return conn.execute("SELECT status FROM items WHERE id = %s", (item_id,)).fetchone()[0]


def _row(qid: int) -> tuple[bool, dict]:
    with db.connect() as conn:
        return conn.execute(
            "SELECT resolved, detail FROM review_queue WHERE id = %s", (qid,)
        ).fetchone()


def _wrapped(cause: Exception) -> ExtractorUnavailable:
    """An ExtractorUnavailable the way kbcore.llm._chat raises it (``from`` httpx)."""
    exc = ExtractorUnavailable(f"extraction call failed: {cause!r}")
    exc.__cause__ = cause
    return exc


GATE_ZERO = {
    "auto_resolve_judge_held": 0,
    "auto_resolve_judge_failed": 0,
    "auto_resolve_judge_retry": 0,
    "auto_resolve_judge_held_open": 0,
    "auto_resolve_judge_failed_open": 0,
    "auto_resolve_gate_unavailable": 0,
}


def test_defaults_are_inert():
    assert Settings.model_fields["kb_auto_resolve_judge_gate"].default is False
    assert Settings.model_fields["kb_contradiction_judge_model"].default == ""
    assert Settings.model_fields["kb_contradiction_judge_think"].default is None


def test_a_confirmed_conflict_still_auto_resolves(gate, nights):
    old, new, qid = _pair("ok")
    nights()
    assert nights() == (1, 1, 0)
    assert _status(old) == "superseded" and _row(qid)[0] is True
    assert gate["judged"] == [(new, old)]
    assert evidence_step._GATE_STATS == GATE_ZERO


def test_no_conflict_holds_the_row_and_retires_nothing(gate, nights):
    gate["verdict"] = False
    old, new, qid = _pair("bugfix")
    nights()
    assert nights() == (1, 0, 0)
    assert _status(old) == "active", "a complementary pair was retired"
    resolved, detail = _row(qid)
    assert resolved is False
    hold = detail["auto_resolve_hold"]
    assert hold["kind"] == review_recency.HOLD_JUDGE == "judge_no_conflict"
    assert (hold["keep"], hold["retire"], hold["model"]) == (new, old, SEAT)
    want = f"held: the contradiction judge ({SEAT}) finds no conflict - owner decision"
    assert hold["reason"] == want and hold["reason"].isascii()
    assert detail["suggested_resolution"]["retire"] == old, "the suggestion stays visible"
    assert evidence_step._GATE_STATS == {**GATE_ZERO, "auto_resolve_judge_held": 1,
                                         "auto_resolve_judge_held_open": 1}  # fmt: skip
    assert evidence_step._EVIDENCE_STATS == {"auto_resolve_held": 0, "auto_resolve_held_open": 0}
    # Night 3: skipped before batching: no refresh, no judge, no churn.
    refreshed, judged = len(nights.calls), len(gate["judged"])
    assert nights() == (0, 0, 0)
    assert (len(nights.calls), len(gate["judged"])) == (refreshed, judged)
    assert _row(qid) == (False, detail)
    assert evidence_step._GATE_STATS == {**GATE_ZERO, "auto_resolve_judge_held_open": 1}


def test_a_gate_hold_is_sticky_across_a_seat_change_and_a_gate_off(gate, nights, monkeypatch):
    """A held row's streak is already past the threshold: releasing it on a rollback
    would retire it automatically the very next pass. Only the owner releases it."""
    gate["verdict"] = False
    old, new, qid = _pair("sticky")
    nights()
    nights()
    detail = _row(qid)[1]
    monkeypatch.setattr(settings, "kb_contradiction_judge_model", "")  # the seat rolled back
    assert nights() == (0, 0, 0)
    monkeypatch.setattr(settings, "kb_auto_resolve_judge_gate", False)  # and the gate off
    assert nights() == (0, 0, 0)
    assert _status(old) == "active" and _row(qid) == (False, detail)
    review.dismiss(qid)  # the owner decides (a bug and its fix are not a conflict)
    assert _row(qid)[0] is True


def test_a_row_queued_by_this_seat_is_still_re_asked_strictly(gate, nights):
    """Detection reads replies leniently (bool(val): a malformed reply can queue a
    pair); only the gate's strict three-state re-ask may authorise a retirement."""
    old, new, qid = _pair("same")
    review_judge_gate.stamp_judged_by(old, new)
    assert _row(qid)[1]["judged_by"] == {"model": SEAT, "think": None}
    gate["verdict"] = None  # the strict parser finds no verdict in the same reply
    nights()
    assert nights() == (1, 0, 0)
    assert gate["judged"] == [(new, old)] and _status(old) == "active"
    assert _row(qid)[1]["auto_resolve_hold"]["kind"] == "judge_failed"


def test_a_moot_row_closes_without_a_judge_call(gate, nights):
    """Two rows share the loser X: the first resolution retires X, the second row is
    moot — closed by the moot path, never held (it would leak open forever)."""
    x = ingest("gate", "qa", "loser", "the old claim", uri="x", trust="unverified")
    k1 = ingest("gate", "qa", "winner one", "the new claim", uri="k1", trust="unverified")
    k2 = ingest("gate", "qa", "winner two", "another new claim", uri="k2", trust="unverified")
    assert record_contradiction(x, k1) and record_contradiction(x, k2)
    nights()
    assert nights() == (2, 1, 1)
    assert _status(x) == "superseded"
    assert len(gate["judged"]) == 1, "the moot row spent no judge call"
    with db.connect() as conn:
        open_rows = conn.execute(
            "SELECT count(*) FROM review_queue WHERE reason = 'contradiction' AND resolved = false"
        ).fetchone()[0]
    assert open_rows == 0


def test_a_no_verdict_is_retried_and_only_the_third_is_final(gate, nights):
    """A timeout on one pair: not retired, counted, re-judged next pass — the slot may
    just have been busy; the third consecutive failure parks it for the owner."""
    old, new, qid = _pair("slow")
    nights()
    gate["verdict"] = _wrapped(httpx.ReadTimeout("timed out"))
    for attempt in (1, 2):
        assert nights() == (1, 0, 0)
        hold = _row(qid)[1]["auto_resolve_hold"]
        assert (hold["kind"], hold["attempts"], hold["final"]) == ("judge_failed", attempt, False)
        assert hold["reason"] == (
            f"retrying: the contradiction judge ({SEAT}) gave no verdict (ReadTimeout; "
            f"attempt {attempt} of 3) - re-judged next pass"
        )
        assert evidence_step._GATE_STATS["auto_resolve_judge_retry"] == 1
        assert evidence_step._GATE_STATS["auto_resolve_judge_failed"] == 0
        assert evidence_step._GATE_STATS["auto_resolve_judge_failed_open"] == 0
    assert nights() == (1, 0, 0)
    hold = _row(qid)[1]["auto_resolve_hold"]
    assert (hold["attempts"], hold["final"]) == (3, True)
    assert hold["reason"].startswith("held: ") and hold["reason"].endswith("- owner decision")
    assert evidence_step._GATE_STATS["auto_resolve_judge_failed"] == 1
    assert evidence_step._GATE_STATS["auto_resolve_judge_failed_open"] == 1
    assert evidence_step._GATE_STATS["auto_resolve_judge_held_open"] == 0
    judged = len(gate["judged"])
    assert nights() == (0, 0, 0) and len(gate["judged"]) == judged, "final: never re-stalled"
    assert _status(old) == "active"


def test_a_verdict_after_a_failure_resolves_normally(gate, nights):
    old, new, qid = _pair("flaky")
    nights()
    gate["verdict"] = None  # an unparseable reply: no verdict
    assert nights() == (1, 0, 0)
    assert (
        "gave no verdict (unparseable; attempt 1 of 3)"
        in _row(qid)[1]["auto_resolve_hold"]["reason"]
    )
    gate["verdict"] = True
    assert nights() == (1, 1, 0) and _status(old) == "superseded"


def test_two_no_verdicts_in_a_row_trip_the_pass_breaker(gate, nights):
    pairs = [_pair(f"brk{i}") for i in range(3)]
    nights()
    gate["verdict"] = _wrapped(ValueError("Expecting value"))  # an unusable reply body
    assert nights() == (3, 0, 0)
    assert len(gate["judged"]) == 2, "the third pair is deferred, not judged"
    assert evidence_step._GATE_STATS["auto_resolve_judge_retry"] == 2
    assert evidence_step._GATE_STATS["auto_resolve_gate_unavailable"] == 1
    assert all(_status(o) == "active" for o, _, _ in pairs)


def test_an_unreachable_judge_retires_nothing_and_stops_asking(gate, nights):
    a_old, a_new, a_qid = _pair("down1")
    b_old, b_new, b_qid = _pair("down2")
    nights()  # streak 1 on both rows: no candidate yet, no judge call
    assert gate["judged"] == []
    gate["verdict"] = _wrapped(httpx.ConnectError("connection refused"))
    assert nights() == (2, 0, 0)  # both rows are candidates tonight
    assert _status(a_old) == "active" and _status(b_old) == "active"
    for qid in (a_qid, b_qid):
        resolved, detail = _row(qid)
        assert resolved is False and "auto_resolve_hold" not in detail, "an outage stamps nothing"
    assert len(gate["judged"]) == 1, "one unreachable judge per pass, not a timeout per pair"
    assert evidence_step._GATE_STATS["auto_resolve_gate_unavailable"] == 2
    gate["verdict"] = True  # the judge is back: the next run resolves both
    assert nights() == (2, 2, 0)


def test_after_an_outage_the_recency_guard_still_holds(gate, nights):
    a_old, a_new, a_qid = _pair("needsjudge")
    b_old, b_new, b_qid = _pair("recent")
    nights.mode_for[b_old] = "retire_new"  # pair b would retire its NEWER item
    nights()
    gate["verdict"] = _wrapped(httpx.ConnectError("connection refused"))
    nights()
    assert _row(b_qid)[1]["auto_resolve_hold"]["kind"] == "newer_retire"
    assert evidence_step._EVIDENCE_STATS["auto_resolve_held"] == 1
    assert evidence_step._GATE_STATS["auto_resolve_gate_unavailable"] == 1


def test_resolutions_run_after_every_refresh(gate, nights, events):
    """Two phases: all evidence calls (default model) first, then the judge (another
    model on the one-slot card) — one model swap per pass, not two per row."""
    _pair("p1")
    _pair("p2")
    nights()
    events.clear()
    nights()
    kinds = [k for k, _ in events]
    assert kinds == ["gather", "gather", "judge", "judge"]


def test_the_recency_hold_comes_first_and_spends_no_judge_call(gate, nights):
    old, new, qid = _pair("recent")
    nights("retire_new")  # retire the NEWER item, a day later: the recency guard holds
    nights("retire_new")
    assert _status(new) == "active"
    assert _row(qid)[1]["auto_resolve_hold"]["kind"] == "newer_retire"
    assert gate["judged"] == []


def test_the_gate_never_overwrites_an_owner_restore_veto(gate):
    old, new, qid = _pair("veto")
    veto = {"auto_resolve_hold": {"kind": review_recency.HOLD_RESTORED, "keep": old, "retire": new}}
    evidence.update_review_detail(qid, veto)
    before = _row(qid)
    assert review_judge_gate.hold_row(qid, new, old) is False
    assert _row(qid) == before


def test_the_kbcore_chokepoint_gates_on_its_own(gate):
    gate["verdict"] = False
    old, new, qid = _pair("core")
    out = review_recency.resolve_contradiction(qid, new, old)
    assert out == review_recency.Outcome(None, "judge_no_conflict")
    first = _row(qid)
    assert review.auto_resolve_contradiction(qid, new, old) is None
    assert _row(qid) == first, "the hold is stamped once"
    assert review_recency.resolve_contradiction(qid, new, old, judge=False).hold == "gate_deferred"
    gate["verdict"] = True
    assert review.auto_resolve_contradiction(qid, new, old) == old


def test_the_contradiction_step_stamps_the_seat_on_rows_it_queues(gate, monkeypatch):
    a = ingest("gate", "qa", "claim a", "x is 5", uri="ca", trust="unverified")
    b = ingest("gate", "qa", "claim b", "x is 7", uri="cb", trust="unverified")
    monkeypatch.setattr(consolidation, "contradiction_pairs", lambda *a_, **k: [(a, b, 0.9)])
    monkeypatch.setattr(llm, "items_contradict", lambda *a_: True)
    assert tail_ops._contradictions("gate") == 1
    with db.connect() as conn:
        judged_by = conn.execute(
            "SELECT detail->'judged_by' FROM review_queue WHERE reason = 'contradiction' "
            "AND (detail->>'a_id')::bigint = %s",
            (a,),
        ).fetchone()[0]
    assert judged_by == {"model": SEAT, "think": None}


def test_a_moot_row_is_closed_even_where_the_recency_guard_would_hold(gate, nights):
    """Round 2: the moot check runs BEFORE the recency guard, gate on or off."""
    k2 = ingest("gate", "qa", "oldest winner", "claim k2", uri="mk2", trust="unverified")
    x = ingest("gate", "qa", "the loser", "claim x", uri="mx", trust="unverified")
    k1 = ingest("gate", "qa", "newer winner", "claim k1", uri="mk1", trust="unverified")
    with db.transaction() as conn:
        for item, days in ((k2, 0), (x, 2), (k1, 3)):
            conn.execute(
                "UPDATE items SET created_at = %s WHERE id = %s", (T0 + timedelta(days=days), item)
            )
    assert record_contradiction(x, k1) and record_contradiction(k2, x)
    nights.mode_for[x] = "retire_old"  # row (x, k1): keep k1, retire x
    nights.mode_for[k2] = "retire_new"  # row (k2, x): keep k2, retire x (+2 d: recency)
    nights()
    nights()
    assert _status(x) == "superseded"
    with db.connect() as conn:
        open_rows = conn.execute(
            "SELECT count(*) FROM review_queue WHERE reason = 'contradiction' AND resolved = false"
        ).fetchone()[0]
    assert open_rows == 0, "the second row leaked open (held, but invisible to the nightly)"


def test_a_row_held_before_its_loser_is_retired_is_closed_as_moot(gate, nights):
    """The reverse order: the recency hold lands first, then a later row retires the
    same loser — the post-pass sweep closes the held row instead of leaking it."""
    k2 = ingest("gate", "qa", "oldest winner", "claim k2", uri="rk2", trust="unverified")
    x = ingest("gate", "qa", "the loser", "claim x", uri="rx", trust="unverified")
    k1 = ingest("gate", "qa", "newer winner", "claim k1", uri="rk1", trust="unverified")
    with db.transaction() as conn:
        for item, days in ((k2, 0), (x, 2), (k1, 3)):
            conn.execute(
                "UPDATE items SET created_at = %s WHERE id = %s", (T0 + timedelta(days=days), item)
            )
    assert record_contradiction(k2, x) and record_contradiction(x, k1)  # held row FIRST
    nights.mode_for[k2] = "retire_new"
    nights.mode_for[x] = "retire_old"
    nights()
    _, _, moot = nights()
    assert _status(x) == "superseded" and moot == 1
    with db.connect() as conn:
        open_rows = conn.execute(
            "SELECT count(*) FROM review_queue WHERE reason = 'contradiction' AND resolved = false"
        ).fetchone()[0]
    assert open_rows == 0


def test_an_inactive_keeper_makes_the_pair_moot_without_a_judge_call(gate):
    old, new, qid = _pair("keeper")
    with db.transaction() as conn:
        conn.execute("UPDATE items SET status = 'archived' WHERE id = %s", (new,))
    with pytest.raises(AlreadySuperseded, match="is already archived"):
        review_recency.resolve_contradiction(qid, new, old)
    assert gate["judged"] == [] and _status(old) == "active"


def test_a_chain_that_retires_a_keeper_closes_the_later_row(gate, nights):
    """keep C / retire A, then keep A / retire B in the same pass: the second row's
    keeper is gone — closed as moot, not left open where the nightly can't see it."""
    a = ingest("gate", "qa", "a", "claim a", uri="cha", trust="unverified")
    b = ingest("gate", "qa", "b", "claim b", uri="chb", trust="unverified")
    c = ingest("gate", "qa", "c", "claim c", uri="chc", trust="unverified")
    assert record_contradiction(a, c)  # row 1 (a_id a): keep c, retire a
    assert record_contradiction(b, a)  # row 2 (a_id b): keep a, retire b
    # the fixture retires each row's a_id side and keeps its b_id side by default
    nights()
    nights()
    assert _status(a) == "superseded"
    with db.connect() as conn:
        open_rows = conn.execute(
            "SELECT count(*) FROM review_queue WHERE reason = 'contradiction' AND resolved = false"
        ).fetchone()[0]
    assert open_rows == 0


def test_an_owner_dismissal_since_the_snapshot_is_never_overridden(gate):
    old, new, qid = _pair("dismissed")
    review.dismiss(qid)
    before = _row(qid)
    assert review_recency.resolve_contradiction(qid, new, old) == review_recency.Outcome(None, None)
    assert _status(old) == "active" and _row(qid) == before


def test_the_digest_names_gate_holds_by_kind():
    assert judge_held_line({}) is None
    counts = {"auto_resolve_judge_held": 2, "auto_resolve_judge_held_open": 5,
              "auto_resolve_judge_failed_open": 1}  # fmt: skip
    want = "auto-resolve held by the judge: 6 (5 no conflict, 1 no verdict; +2 tonight)"
    assert judge_held_line(counts) == want
    assert want in digest.digest_lines(counts)
    assert judge_held_line({"auto_resolve_judge_retry": 1}) == (
        "precision gate: 1 retrying (no verdict yet)"
    )
    down = judge_held_line({"auto_resolve_gate_unavailable": 3})
    assert down == "precision gate: judge unreachable, 3 deferred"


def test_a_deadline_in_phase_one_still_resolves_the_collected_rows(gate, nights, monkeypatch):
    """Only a battery stand-down stops phase 2: the hot reserve keeps its promise."""
    old, new, qid = _pair("deadline")
    nights()
    monkeypatch.setattr(consolidate, "_on_battery", lambda: False)
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return None if calls["n"] == 1 else "deadline"  # the deadline lands after phase 1

    refreshed, resolved, _ = consolidate._evidence(stop=stop)
    assert (refreshed, resolved) == (1, 1) and _status(old) == "superseded"


def test_a_battery_stand_down_stops_the_judge_phase_even_past_the_deadline(
    gate, nights, monkeypatch
):
    """Past the deadline should_stop() answers 'deadline' and never reads the battery,
    so phase 2 asks the power guard itself."""
    old, new, qid = _pair("battery")
    nights()
    monkeypatch.setattr(consolidate, "_on_battery", lambda: True)
    refreshed, resolved, _ = consolidate._evidence(stop=lambda: None)
    assert (refreshed, resolved) == (1, 0) and gate["judged"] == []
    assert _status(old) == "active"


def test_a_recency_hold_between_no_verdicts_does_not_reset_the_breaker(gate, nights):
    p1 = _pair("b1")
    p2 = _pair("b2")
    p3 = _pair("b3")
    nights.mode_for[p2[0]] = "retire_new"  # the middle row is recency-held, no judge
    nights()
    gate["verdict"] = _wrapped(httpx.ReadTimeout("timed out"))
    nights()
    assert len(gate["judged"]) == 2, "fail, recency hold, fail: the breaker trips at 2"
    assert evidence_step._GATE_STATS["auto_resolve_judge_retry"] == 2
    assert _row(p2[2])[1]["auto_resolve_hold"]["kind"] == "newer_retire"
    assert _status(p1[0]) == "active" and _status(p3[0]) == "active"


def test_a_held_row_whose_keeper_a_later_row_retires_is_closed(gate, nights):
    """keep y / retire z is held first; a later row retires y: the sweep closes the
    held row (its keeper is gone) and takes the hold back out of tonight's counts."""
    z = ingest("gate", "qa", "z", "value 1", uri="kz", trust="unverified")
    y = ingest("gate", "qa", "y", "value 2", uri="ky", trust="unverified")
    x = ingest("gate", "qa", "x", "value 3", uri="kx", trust="unverified")
    assert record_contradiction(z, y)  # row 1: keep y, retire z (judged first)
    assert record_contradiction(y, x)  # row 2: keep x, retire y
    nights()
    answers = iter([False, True])  # row 1: no conflict (held); row 2: confirmed
    gate["verdict"] = lambda: next(answers)
    _, resolved, moot = nights()
    assert (resolved, moot) == (1, 1) and _status(y) == "superseded"
    with db.connect() as conn:
        open_rows = conn.execute(
            "SELECT count(*) FROM review_queue WHERE reason = 'contradiction' AND resolved = false"
        ).fetchone()[0]
    assert open_rows == 0
    assert evidence_step._GATE_STATS["auto_resolve_judge_held"] == 0
    assert evidence_step._GATE_STATS["auto_resolve_judge_held_open"] == 0


def test_attempts_restart_when_the_seat_changes_think(gate, nights, monkeypatch):
    old, new, qid = _pair("think")
    nights()
    gate["verdict"] = _wrapped(httpx.ReadTimeout("timed out"))
    nights()
    nights()
    assert _row(qid)[1]["auto_resolve_hold"]["attempts"] == 2
    monkeypatch.setattr(settings, "kb_contradiction_judge_think", False)  # a new seat
    nights()
    hold = _row(qid)[1]["auto_resolve_hold"]
    assert (hold["attempts"], hold["final"]) == (1, False)


def test_apply_all_skips_rows_a_hold_keeps_for_the_owner(gate, nights):
    from typer.testing import CliRunner

    from kbcli.main import app

    gate["verdict"] = False
    old, new, qid = _pair("cli")
    nights()
    nights()
    out = CliRunner().invoke(app, ["review", "apply", "--all"])
    assert out.exit_code == 0, out.output
    assert f"[{qid}] skipped: held: the contradiction judge" in out.output
    assert "1 held row(s) skipped (--include-held)" in out.output
    assert _status(old) == "active"
    out = CliRunner().invoke(app, ["review", "apply", "--all", "--include-held"])
    assert out.exit_code == 0 and _status(old) == "superseded"


def test_a_failed_judge_stamp_never_aborts_the_contradiction_step(gate, monkeypatch):
    a = ingest("gate", "qa", "sa", "x is 5", uri="sa", trust="unverified")
    b = ingest("gate", "qa", "sb", "x is 7", uri="sb", trust="unverified")
    monkeypatch.setattr(consolidation, "contradiction_pairs", lambda *a_, **k: [(a, b, 0.9)])
    monkeypatch.setattr(llm, "items_contradict", lambda *a_: True)

    def boom(*_a):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(review_judge_gate, "stamp_judged_by", boom)
    assert tail_ops._contradictions("gate") == 1


def test_an_http_status_from_the_judge_is_an_outage(gate, nights):
    """Ollama's 4xx are request-shape errors common to every pair (e.g. think on a
    non-thinking model): never a per-pair hold."""
    old, new, qid = _pair("http400")
    nights()
    req = httpx.Request("POST", "http://127.0.0.1:11434/api/chat")
    resp = httpx.Response(400, text='{"error":"model does not support thinking"}', request=req)
    gate["verdict"] = _wrapped(httpx.HTTPStatusError("400", request=req, response=resp))
    nights()
    assert "auto_resolve_hold" not in _row(qid)[1]
    assert evidence_step._GATE_STATS["auto_resolve_gate_unavailable"] == 1


# --- the contradiction seat -------------------------------------------------------


@pytest.fixture()
def capture(monkeypatch) -> dict:
    store: dict = {}
    answer = json.dumps({"contradict": False, "same_fact": False, "rationale": "r"})

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": answer}}

    def post(url, *, json=None, timeout=None):
        store["payload"] = json
        return _Resp()

    monkeypatch.setattr(llm.httpx, "post", post)
    monkeypatch.setattr(twin_judge, "item_pair_texts", lambda a, b: (("A", "a"), ("B", "b")))
    return store


def test_items_contradict_rides_its_own_seat_and_the_same_fact_judge_does_not(capture, monkeypatch):
    monkeypatch.setattr(settings, "kb_pair_judge_model", "")
    monkeypatch.setattr(settings, "kb_contradiction_judge_model", SEAT)
    monkeypatch.setattr(settings, "kb_contradiction_judge_think", False)
    llm.items_contradict("a", "x", "b", "y")
    assert (capture["payload"]["model"], capture["payload"]["think"]) == (SEAT, False)
    twin_judge.judge_same_fact(1, "p1", 2, "p2")
    assert capture["payload"]["model"] == settings.kb_extract_model
    assert "think" not in capture["payload"]


def test_an_empty_contradiction_seat_falls_back_to_the_pair_seat(capture, monkeypatch):
    monkeypatch.setattr(settings, "kb_pair_judge_model", "granite4.2:8b")
    monkeypatch.setattr(settings, "kb_pair_judge_think", False)
    monkeypatch.setattr(settings, "kb_contradiction_judge_model", "")
    monkeypatch.setattr(
        settings, "kb_contradiction_judge_think", True
    )  # stale: read only with a model
    llm.items_contradict("a", "x", "b", "y")
    assert (capture["payload"]["model"], capture["payload"]["think"]) == ("granite4.2:8b", False)
    monkeypatch.setattr(settings, "kb_pair_judge_model", "")
    llm.items_contradict("a", "x", "b", "y")
    assert capture["payload"]["model"] == settings.kb_extract_model
    assert "think" not in capture["payload"], "both seats empty: the incumbent request"


@pytest.mark.parametrize(
    "content,expected",
    [
        ('{"contradict": true}', True),
        ('{"contradict": false}', False),
        ('{"contradict": "false"}', False),
        ('{"contradict": "yes"}', True),
        ('{"contradict": 1}', True),
        ('{"contradict": 0}', False),
        ('{"contradict": 2}', None),
        ('{"contradict": "maybe"}', None),
        ('{"verdict": true}', None),
        ("", None),  # a token-repeat abort reads as an empty reply (kbcore.llm)
        ("not json", None),
    ],
)
def test_the_gate_verdict_is_three_state(kb, monkeypatch, content, expected):
    monkeypatch.setattr(settings, "kb_contradiction_judge_model", SEAT)
    a = ingest(
        "gate", "qa", "va", "x is 5", uri=f"va-{expected}-{len(content)}", trust="unverified"
    )
    b = ingest(
        "gate", "qa", "vb", "x is 7", uri=f"vb-{expected}-{len(content)}", trust="unverified"
    )

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": content}}

    monkeypatch.setattr(llm.httpx, "post", lambda url, *, json=None, timeout=None: _Resp())
    assert review_judge_gate.verdict(b, a) is expected


def test_the_gate_asks_byte_for_byte_what_the_nightly_asks(kb, capture, monkeypatch):
    monkeypatch.setattr(settings, "kb_contradiction_judge_model", SEAT)
    a = ingest("gate", "qa", "ta", "x is 5", uri="ta", trust="unverified")
    b = ingest("gate", "qa", "tb", "x is 7", uri="tb", trust="unverified")
    (at, ab), (bt, bb) = consolidation.item_pair_texts(a, b)
    llm.items_contradict(at, ab, bt, bb)
    nightly = json.dumps(capture["payload"])
    review_judge_gate.verdict(b, a)  # keep/retire in either order: lower id is Item A
    assert json.dumps(capture["payload"]) == nightly

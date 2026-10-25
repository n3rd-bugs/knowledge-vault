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

import pytest

from kbcore import db, evidence, llm, review, review_judge_gate, review_recency, twin_judge
from kbcore.config import Settings, settings
from kbcore.consolidation import record_contradiction
from kbcore.errors import ExtractorUnavailable
from kbcore.ingest import ingest
from kbworker import consolidate, digest, evidence_step
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
def gate(kb, monkeypatch):
    """Gate on, contradiction seat = SEAT; ``gate.verdict`` is what the stub judge
    answers (a value or an exception), ``gate.judged`` records every judged pair."""
    monkeypatch.setattr(settings, "kb_auto_resolve_judge_gate", True)
    monkeypatch.setattr(settings, "kb_contradiction_judge_model", SEAT)
    state: dict = {"verdict": True, "judged": []}

    def judge(a_title: str, a_body: str, b_title: str, b_body: str) -> bool:
        state["judged"].append((a_title, b_title))
        if isinstance(state["verdict"], Exception):
            raise state["verdict"]
        return state["verdict"]

    monkeypatch.setattr(llm, "items_contradict", judge)
    return state


@pytest.fixture()
def nights(kb, monkeypatch):
    """``night(mode)`` = one evidence pass whose refresh suggests, for EVERY open row,
    retiring its older item (``"retire_old"``, kept = the newer) or its newer one."""
    calls: list[tuple[int, int]] = []
    state = {"mode": "retire_old"}

    def gather(a: int, b: int) -> dict:  # a = the older item (record_contradiction order)
        calls.append((a, b))
        keep, retire = (b, a) if state["mode"] == "retire_old" else (a, b)
        sugg = {"action": "supersede", "keep": keep, "retire": retire, "rule": "evidence: test"}
        return {"a_id": a, "b_id": b, "evidence_updated": datetime.now(UTC).isoformat(),
                "suggested_resolution": sugg}  # fmt: skip

    monkeypatch.setattr(evidence, "gather_evidence", gather)
    monkeypatch.setattr(consolidate, "EVIDENCE_PAIR_CAP", 10)

    def night(mode: str = "retire_old") -> tuple[int, int, int]:
        state["mode"] = mode
        return consolidate._evidence()

    night.calls = calls  # type: ignore[attr-defined]
    return night


def _status(item_id: int) -> str:
    with db.connect() as conn:
        return conn.execute("SELECT status FROM items WHERE id = %s", (item_id,)).fetchone()[0]


def _row(qid: int) -> tuple[bool, dict]:
    with db.connect() as conn:
        return conn.execute(
            "SELECT resolved, detail FROM review_queue WHERE id = %s", (qid,)
        ).fetchone()


def test_defaults_are_inert():
    assert Settings.model_fields["kb_auto_resolve_judge_gate"].default is False
    assert Settings.model_fields["kb_contradiction_judge_model"].default == ""
    assert Settings.model_fields["kb_contradiction_judge_think"].default is None


def test_a_confirmed_conflict_still_auto_resolves(gate, nights):
    old, new, qid = _pair("ok")
    nights()
    assert nights() == (1, 1, 0)
    assert _status(old) == "superseded" and _row(qid)[0] is True
    # asked the way the nightly step asks: lower id as Item A
    assert gate["judged"] == [("bug ok", "fix ok")]
    assert evidence_step._GATE_STATS == {
        "auto_resolve_judge_held": 0,
        "auto_resolve_judge_held_open": 0,
        "auto_resolve_gate_unavailable": 0,
    }


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
    assert (
        hold["reason"]
        == f"held: the contradiction judge ({SEAT}) finds no conflict - owner decision"
    )
    assert hold["reason"].isascii()
    assert detail["suggested_resolution"]["retire"] == old, "the suggestion stays visible"
    assert evidence_step._GATE_STATS["auto_resolve_judge_held"] == 1
    assert evidence_step._GATE_STATS["auto_resolve_judge_held_open"] == 1
    assert evidence_step._EVIDENCE_STATS == {"auto_resolve_held": 0, "auto_resolve_held_open": 0}
    # Night 3: idempotent — skipped before batching: no refresh, no judge, no churn.
    refreshed, judged = len(nights.calls), len(gate["judged"])
    assert nights() == (0, 0, 0)
    assert (len(nights.calls), len(gate["judged"])) == (refreshed, judged)
    assert _row(qid) == (False, detail)
    assert evidence_step._GATE_STATS["auto_resolve_judge_held"] == 0
    assert evidence_step._GATE_STATS["auto_resolve_judge_held_open"] == 1


def test_an_unreachable_judge_retires_nothing_and_stops_asking(gate, nights):
    a_old, a_new, a_qid = _pair("down1")
    b_old, b_new, b_qid = _pair("down2")
    nights()  # streak 1 on both rows: no candidate yet, no judge call
    assert gate["judged"] == []
    gate["verdict"] = ExtractorUnavailable("model 'bonsai-2:27b' not found")
    assert nights() == (2, 0, 0)  # both rows are candidates tonight
    assert _status(a_old) == "active" and _status(b_old) == "active"
    for qid in (a_qid, b_qid):
        resolved, detail = _row(qid)
        assert resolved is False and "auto_resolve_hold" not in detail, "an outage stamps nothing"
    assert len(gate["judged"]) == 1, "one unreachable judge per pass, not a timeout per pair"
    assert evidence_step._GATE_STATS["auto_resolve_gate_unavailable"] == 2
    gate["verdict"] = True  # the judge is back: the next run resolves both
    assert nights() == (2, 2, 0)


def test_the_recency_hold_comes_first_and_spends_no_judge_call(gate, nights):
    gate["verdict"] = True
    old, new, qid = _pair("recent")
    nights("retire_new")  # retire the NEWER item, a day later: the recency guard holds
    nights("retire_new")
    assert _status(new) == "active"
    assert _row(qid)[1]["auto_resolve_hold"]["kind"] == "newer_retire"
    assert gate["judged"] == []


def test_a_new_judge_model_releases_the_hold_for_re_judging(gate, nights, monkeypatch):
    gate["verdict"] = False
    old, new, qid = _pair("swap")
    nights()
    nights()
    assert _row(qid)[1]["auto_resolve_hold"]["model"] == SEAT
    monkeypatch.setattr(settings, "kb_contradiction_judge_model", "gemma4:12b")
    gate["verdict"] = True
    judged = len(gate["judged"])
    assert nights() == (1, 1, 0), "released, re-judged by the new seat, confirmed"
    assert len(gate["judged"]) == judged + 1 and _status(old) == "superseded"


def test_switching_the_gate_off_restores_the_ungated_behaviour(gate, nights, monkeypatch):
    gate["verdict"] = False
    old, new, qid = _pair("off")
    nights()
    nights()
    assert _row(qid)[0] is False
    monkeypatch.setattr(settings, "kb_auto_resolve_judge_gate", False)
    judged = len(gate["judged"])
    assert nights() == (1, 1, 0)
    assert _status(old) == "superseded" and len(gate["judged"]) == judged


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
    assert review.auto_resolve_contradiction(qid, new, old) is None
    assert review_recency.hold_kind(qid) == "judge_no_conflict"
    first = _row(qid)
    assert review.auto_resolve_contradiction(qid, new, old) is None
    assert _row(qid) == first, "the hold is stamped once"
    gate["verdict"] = True
    assert review.auto_resolve_contradiction(qid, new, old) == old


def test_the_digest_names_gate_holds():
    assert judge_held_line({}) is None
    counts = {"auto_resolve_judge_held": 2, "auto_resolve_judge_held_open": 5}
    want = "auto-resolve held: 5 (+2 tonight; contradiction judge: no conflict)"
    assert judge_held_line(counts) == want
    assert want in digest.digest_lines(counts)
    down = judge_held_line({"auto_resolve_gate_unavailable": 3})
    assert (
        down
        == "auto-resolve held: 0 (contradiction judge: no conflict); gate judge unreachable for 3"
    )


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

"""The owner's undo for an evidence auto-resolution (kbcore.review_restore,
``kb review restore``) and the recency guard's digest line.

The live damage this reverses: 46 newer items retired by the nightly in favour
of older ones before the guard existed (their rows carry no auto_resolution
stamp). Real DB (kb_test); the evidence judge is a fixed suggestion.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from kbcore import db, evidence, scope
from kbcore.config import settings
from kbcore.consolidation import record_contradiction
from kbcore.errors import InvalidOperation, NotFound
from kbcore.ingest import ingest
from kbcore.review import apply_suggestion, queue_review
from kbcore.review_recency import AUTO_RESOLVE_REASON
from kbcore.review_restore import SWAP_REASON, restore_auto_resolved
from kbcore.supersede import supersede
from kbworker import consolidate
from kbworker.digest_recency_lines import auto_resolve_held_line

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _set_window(monkeypatch, days: float) -> None:
    """Set the knob — asserting it EXISTS first: monkeypatch's missing-attribute
    error prints the whole Settings repr, secrets included (seen on HEAD)."""
    if "kb_auto_resolve_newer_gap_days" not in type(settings).model_fields:
        pytest.fail("Settings has no kb_auto_resolve_newer_gap_days knob")
    monkeypatch.setattr(settings, "kb_auto_resolve_newer_gap_days", days)


def _pair(tag: str = "x") -> tuple[int, int, int]:
    """``(old, new, qid)``: ``new`` 27 days younger, contradiction row queued."""
    old = ingest("restore", "qa", f"old {tag}", f"no regression {tag}", uri=f"o-{tag}")
    new = ingest("restore", "qa", f"new {tag}", f"below the floor {tag}", uri=f"n-{tag}")
    with db.transaction() as conn:
        conn.execute("UPDATE items SET created_at = %s WHERE id = %s", (T0, old))
        conn.execute(
            "UPDATE items SET created_at = %s WHERE id = %s", (T0 + timedelta(days=27), new)
        )
    assert record_contradiction(old, new) is True
    with db.connect() as conn:
        qid = conn.execute(
            "SELECT id FROM review_queue WHERE (detail->>'a_id')::bigint = %s", (old,)
        ).fetchone()[0]
    return old, new, qid


@pytest.fixture()
def damaged(kb, monkeypatch):
    """A pair the PRE-GUARD nightly settled wrongly: the newer item retired by the
    evidence auto-resolve (window wide open, as before 2026-10-24)."""
    old, new, qid = _pair()
    sugg = {"action": "supersede", "keep": old, "retire": new, "rule": "evidence: test"}
    calls: list[tuple[int, int]] = []

    def gather(a: int, b: int) -> dict:
        calls.append((a, b))
        return {"a_id": a, "b_id": b, "suggested_resolution": sugg}

    monkeypatch.setattr(evidence, "gather_evidence", gather)
    monkeypatch.setattr(consolidate, "EVIDENCE_PAIR_CAP", 10)
    _set_window(monkeypatch, 1000.0)
    consolidate._evidence()
    assert consolidate._evidence() == (1, 1, 0)
    assert _item(new)[:3] == ("superseded", old, AUTO_RESOLVE_REASON)
    return {"old": old, "new": new, "qid": qid, "calls": calls}


def _item(item_id: int) -> tuple:
    with db.connect() as conn:
        return conn.execute(
            "SELECT status, superseded_by, supersede_reason, valid_to, body, meta "
            "FROM items WHERE id = %s",
            (item_id,),
        ).fetchone()


def _row(qid: int) -> tuple[bool, dict]:
    with db.connect() as conn:
        return conn.execute(
            "SELECT resolved, detail FROM review_queue WHERE id = %s", (qid,)
        ).fetchone()


def _relations() -> int:
    with db.connect() as conn:
        return conn.execute("SELECT count(*) FROM relations").fetchone()[0]


def test_restore_is_a_dry_run_by_default(damaged):
    new, before = damaged["new"], _item(damaged["new"])
    plan = restore_auto_resolved(new)
    assert (plan.retired_id, plan.kept_id, plan.queue_id) == (new, damaged["old"], damaged["qid"])
    assert plan.reopen_window is True, "the supersession's inferred close is undone"
    assert plan.applied is False and plan.swap is False
    assert _item(new) == before, "a dry run writes nothing"
    assert _row(damaged["qid"])[0] is True


def test_restore_reactivates_and_reopens_the_pair_with_a_veto(damaged):
    old, new, qid = damaged["old"], damaged["new"], damaged["qid"]
    body, rels = _item(new)[4], _relations()
    plan = restore_auto_resolved(new, apply=True, note="10-16 eval is the current one")
    assert plan.applied is True and plan.queue_id == qid
    status, by, reason, valid_to, after_body, meta = _item(new)
    assert (status, by, reason, valid_to) == ("active", None, None, None)
    assert after_body == body, "items.body is immutable"
    assert "valid_time" not in meta, "the inferred-close stamp goes with the close"
    rec = meta["restored"][-1]
    assert rec["superseded_by"] == old and rec["supersede_reason"] == AUTO_RESOLVE_REASON
    assert rec["note"] == "10-16 eval is the current one" and rec["swap"] is False
    assert _relations() == rels, "nothing deleted: the supersedes edge stays as history"
    resolved, detail = _row(qid)
    assert resolved is False, "re-opened for the owner"
    assert detail["auto_resolve_hold"]["kind"] == "owner_restore"
    assert detail["suggested_resolution"] is None and detail["suggestion_streak"] == 0
    assert detail["restored_suggestion"]["retire"] == new
    assert "undone_at" in detail["auto_resolution"]
    # The veto: tonight's pass never re-judges (or re-retires) the restored pair,
    # however wide the window, and `kb review apply` has nothing to re-apply.
    judged = len(damaged["calls"])
    assert consolidate._evidence() == (0, 0, 0)
    assert len(damaged["calls"]) == judged and _item(new)[0] == "active"
    with pytest.raises(InvalidOperation, match="no suggested resolution"):
        apply_suggestion(qid)


def test_restore_swap_supersedes_the_previously_kept_item(damaged):
    old, new, qid = damaged["old"], damaged["new"], damaged["qid"]
    plan = restore_auto_resolved(new, swap=True, apply=True)
    assert plan.applied and plan.swap
    assert _item(new)[:2] == ("active", None)
    assert _item(old)[:3] == ("superseded", new, SWAP_REASON)
    resolved, detail = _row(qid)
    assert resolved is True
    assert detail["auto_resolution"]["kind"] == "owner_restore_swap"
    assert (detail["auto_resolution"]["keep"], detail["auto_resolution"]["retire"]) == (new, old)


def test_restore_finds_the_row_of_a_pre_stamp_resolution(kb):
    """The 92 live resolutions predate the auto_resolution stamp: the row is found
    by its suggestion, and an item with no surviving row gets a fresh one."""
    old, new, qid = _pair("live")
    evidence.update_review_detail(
        qid, {"suggested_resolution": {"keep": old, "retire": new}, "suggestion_streak": 2}
    )
    supersede(new, new_item_id=old, reason=AUTO_RESOLVE_REASON)  # HEAD's exact effect
    with db.transaction() as conn:
        conn.execute("UPDATE review_queue SET resolved = true WHERE id = %s", (qid,))
    assert restore_auto_resolved(new, apply=True).queue_id == qid
    assert _row(qid)[0] is False

    lone_old = ingest("restore", "qa", "lone old", "lone old body", uri="lo")
    lone_new = ingest("restore", "qa", "lone new", "lone new body", uri="ln")
    supersede(lone_new, new_item_id=lone_old, reason=AUTO_RESOLVE_REASON)
    plan = restore_auto_resolved(lone_new, apply=True)
    resolved, detail = _row(plan.queue_id)
    assert resolved is False and (detail["a_id"], detail["b_id"]) == (lone_old, lone_new)
    assert detail["auto_resolve_hold"]["kind"] == "owner_restore"


def test_restore_vetoes_the_hidden_twin_row_too(damaged):
    """record_contradiction keys by direction, so (b, a) can be open beside the
    settled (a, b) — hidden while one side was superseded. Restoring makes both
    items active again; the twin must not resurface with its own streak."""
    old, new = damaged["old"], damaged["new"]
    twin = queue_review(new, "contradiction", {"a_id": new, "b_id": old})
    sugg = {"keep": old, "retire": new, "rule": "evidence: twin"}
    evidence.update_review_detail(twin, {"suggested_resolution": sugg, "suggestion_streak": 1})
    assert restore_auto_resolved(new).twin_rows == 1
    plan = restore_auto_resolved(new, apply=True)
    assert plan.queue_id == damaged["qid"] and plan.twin_rows == 1
    resolved, detail = _row(twin)
    assert resolved is False and detail["auto_resolve_hold"]["kind"] == "owner_restore"
    assert detail["suggested_resolution"] is None
    assert detail["restored_suggestion"]["rule"] == "evidence: twin", "its OWN suggestion"
    judged = len(damaged["calls"])
    assert consolidate._evidence() == (0, 0, 0)
    assert len(damaged["calls"]) == judged and _item(new)[0] == "active"


def test_swap_closes_every_open_row_of_the_pair(damaged):
    old, new = damaged["old"], damaged["new"]
    twin = queue_review(new, "contradiction", {"a_id": new, "b_id": old})
    restore_auto_resolved(new, swap=True, apply=True)
    for qid in (damaged["qid"], twin):
        resolved, detail = _row(qid)
        assert resolved is True and detail["auto_resolution"]["kind"] == "owner_restore_swap"


def test_a_restore_racing_the_nightly_close_is_not_undone(kb, monkeypatch):
    """supersede() and the row close commit separately: an owner restore landing
    between them must leave the row open and vetoed."""
    from kbcore import review_recency

    old, new, qid = _pair("race")
    real = review_recency.supersede

    def supersede_then_owner_restores(*a, **k):
        out = real(*a, **k)
        restore_auto_resolved(new, apply=True)
        return out

    monkeypatch.setattr(review_recency, "supersede", supersede_then_owner_restores)
    _set_window(monkeypatch, 1000.0)
    assert review_recency.auto_resolve_contradiction(qid, old, new) == new
    resolved, detail = _row(qid)
    assert resolved is False and detail["auto_resolve_hold"]["kind"] == "owner_restore"
    assert _item(new)[0] == "active"


def test_an_asserted_valid_to_survives_a_restore(kb):
    """Only the STAMPED inferred close is reopened: a valid_to equal to the keeper's
    own start is indistinguishable from an asserted end, so it stands."""
    start = datetime(2026, 10, 1, tzinfo=UTC)
    old = ingest("restore", "qa", "vt old", "vt old body", uri="vo", valid_from=start)
    new = ingest("restore", "qa", "vt new", "vt new body", uri="vn", valid_to=start)  # asserted
    supersede(new, new_item_id=old, reason=AUTO_RESOLVE_REASON)
    assert _item(new)[3] == start, "supersede leaves an asserted end alone"
    plan = restore_auto_resolved(new, apply=True)
    assert plan.reopen_window is False and _item(new)[3] == start


def test_restore_refuses_what_it_does_not_own(damaged):
    with pytest.raises(NotFound):
        restore_auto_resolved(999_999)
    with pytest.raises(InvalidOperation, match="not superseded"):
        restore_auto_resolved(damaged["old"])  # active
    a = ingest("restore", "note", "human a", "human a body", uri="ha")
    b = ingest("restore", "note", "human b", "human b body", uri="hb")
    supersede(a, new_item_id=b, reason="manual: corrected by hand")
    with pytest.raises(InvalidOperation, match="not retired by an evidence resolution"):
        restore_auto_resolved(a, apply=True)
    assert _item(a)[0] == "superseded"
    # --swap over a kept item that is no longer active writes nothing
    with db.transaction() as conn:
        conn.execute("UPDATE items SET status = 'archived' WHERE id = %s", (damaged["old"],))
    with pytest.raises(InvalidOperation, match="needs it active"):
        restore_auto_resolved(damaged["new"], swap=True, apply=True)
    assert _item(damaged["new"])[0] == "superseded"
    grantee = scope.Principal(
        name="g",
        read_projects=frozenset({"restore"}),
        write_projects=frozenset({"restore"}),
        caps=scope.ALL_CAPS,
    )
    with scope.use_principal(grantee), pytest.raises(InvalidOperation, match="owner only"):
        restore_auto_resolved(damaged["new"])


def _fake_lock(acquired: bool):
    @contextmanager
    def _lock(_key: int):
        yield acquired

    return _lock


def test_cli_restore_dry_run_then_apply(damaged, monkeypatch):
    from kbcli.main import app

    # --apply takes the consolidation lock; keys are schema-scoped, so a real
    # pg_try_advisory_lock here would race other test sessions -- fake it.
    monkeypatch.setattr(db, "advisory_lock", _fake_lock(True))

    new = str(damaged["new"])
    dry = CliRunner().invoke(app, ["review", "restore", new])
    assert dry.exit_code == 0, dry.output
    assert f"would restore item {new}" in dry.output and "dry run: nothing written" in dry.output
    assert _item(damaged["new"])[0] == "superseded"
    done = CliRunner().invoke(app, ["review", "restore", new, "--apply", "--note", "newer wins"])
    assert done.exit_code == 0, done.output
    assert f"restored item {new}" in done.output and "re-opened for owner review" in done.output
    assert _item(damaged["new"])[0] == "active"
    again = CliRunner().invoke(app, ["review", "restore", new])
    assert again.exit_code == 1 and "not superseded" in again.output


def test_digest_line_for_held_pairs():
    assert auto_resolve_held_line({}) is None
    assert auto_resolve_held_line({"auto_resolve_held": 0, "auto_resolve_held_open": 0}) is None
    assert auto_resolve_held_line({"auto_resolve_held_open": True}) is None
    assert (
        auto_resolve_held_line({"auto_resolve_held": 0, "auto_resolve_held_open": 31})
        == "auto-resolve held: 31 (would retire the newer item)"
    )
    line = auto_resolve_held_line({"auto_resolve_held": 2, "auto_resolve_held_open": 31})
    assert line == "auto-resolve held: 31 (+2 tonight; would retire the newer item)"
    assert line.isascii()


def test_cli_restore_apply_refuses_while_a_consolidation_holds_the_lock(damaged, monkeypatch):
    """Codex review (C.3): the nightly evidence pass batches from a snapshot of
    hold state, so a restore interleaved with it could be re-retired from a
    cached streak. --apply therefore runs under the consolidation lock."""
    from kbcli.main import app

    monkeypatch.setattr(db, "advisory_lock", _fake_lock(False))
    new = str(damaged["new"])
    res = CliRunner().invoke(app, ["review", "restore", new, "--apply"])
    assert res.exit_code == 1 and "consolidation lock" in res.output, res.output
    assert _item(damaged["new"])[0] == "superseded", "nothing written"
    dry = CliRunner().invoke(app, ["review", "restore", new])
    assert dry.exit_code == 0 and f"would restore item {new}" in dry.output, "dry run needs no lock"


def test_a_recency_hold_never_overwrites_an_owner_restore_veto(damaged):
    from kbcore import review_recency

    old, new, qid = damaged["old"], damaged["new"], damaged["qid"]
    restore_auto_resolved(new, apply=True)
    assert _row(qid)[1]["auto_resolve_hold"]["kind"] == "owner_restore"
    assert review_recency.hold_row(qid, old, new, 30.0) is False
    assert _row(qid)[1]["auto_resolve_hold"]["kind"] == "owner_restore", "the veto stands"

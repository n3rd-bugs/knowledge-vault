"""Phase 4 — the `kb review` workflow (PLAN.md §7.4): promote / reject / supersede
round-trip resolve queue rows; plus llm extractor helpers (mock-only Ollama).
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from kbcore import db, llm
from kbcore import review as review_mod
from kbcore.ingest import ingest
from kbcore.items import get_item
from kbcore.review import queue_review

runner = CliRunner()


def _resolved(queue_id: int) -> bool:
    with db.connect() as conn:
        return conn.execute(
            "SELECT resolved FROM review_queue WHERE id = %s", (queue_id,)
        ).fetchone()[0]


def test_promote_sets_verified_and_resolves(kb):
    item_id = ingest("p", "note", "auto note", "an unverified extraction", trust="unverified")
    q = queue_review(item_id, "unverified_extraction", {"rejected_count": 0})
    assert get_item(item_id).trust == "unverified"

    assert review_mod.promote(q) == item_id
    assert get_item(item_id).trust == "verified"
    assert _resolved(q) is True


def test_reject_archives_and_resolves(kb):
    item_id = ingest("p", "note", "bad note", "an unverified extraction", trust="unverified")
    q = queue_review(item_id, "unverified_extraction")

    assert review_mod.reject(q) == item_id
    assert get_item(item_id).status == "archived"
    assert _resolved(q) is True


def test_supersede_resolves_contradiction(kb):
    old = ingest("p", "decision", "old", "the bus runs in mode 0", uri="old")
    new = ingest("p", "decision", "new", "the bus runs in mode 1", uri="new")
    q = queue_review(old, "contradiction", {"a_id": old, "b_id": new})

    assert review_mod.resolve_via_supersede(q, new) == old
    assert get_item(old).status == "superseded"
    assert get_item(old).superseded_by == new
    assert _resolved(q) is True


def test_list_open_review_hides_resolved(kb):
    keep = ingest("p", "note", "keep", "still open", trust="unverified")
    gone = ingest("p", "note", "gone", "will resolve", trust="unverified")
    q_keep = queue_review(keep, "unverified_extraction")
    q_gone = queue_review(gone, "unverified_extraction")
    review_mod.promote(q_gone)

    open_ids = {e.queue_id for e in review_mod.list_open_review()}
    assert q_keep in open_ids and q_gone not in open_ids


def test_review_cli_list_and_promote_roundtrip(kb):
    from kbcli.main import app

    item_id = ingest("p", "note", "cli note", "unverified via cli", trust="unverified")
    q = queue_review(item_id, "unverified_extraction")

    listed = runner.invoke(app, ["review", "list"])
    assert listed.exit_code == 0
    assert f"[{q}]" in listed.stdout and "cli note" in listed.stdout

    promoted = runner.invoke(app, ["review", "promote", str(q)])
    assert promoted.exit_code == 0
    assert get_item(item_id).trust == "verified"
    assert _resolved(q) is True

    empty = runner.invoke(app, ["review", "list"])
    assert "review queue is empty" in empty.stdout


def test_promote_missing_queue_row_errors(kb):
    with pytest.raises(Exception):  # noqa: B017 — NotFound domain error
        review_mod.promote(999999)


def _row(queue_id: int) -> tuple:
    with db.connect() as conn:
        return conn.execute(
            "SELECT item_id, reason, resolved, detail, created_at FROM review_queue WHERE id = %s",
            (queue_id,),
        ).fetchone()


def test_resolve_review_note_stamps_the_detail_and_stays_a_flip(kb):
    """E2 (review #10): the sentinel closes an other-epoch regression row with
    note='epoch changed' — a status flip plus a detail stamp, nothing else."""
    from datetime import datetime

    detail = {"baseline_hit_at_5": 0.9836, "baseline_mrr": 0.8607}
    noted = queue_review(None, "search_regression", detail)
    plain = queue_review(None, "search_regression", dict(detail))
    bystander = queue_review(None, "search_regression", {"baseline_mrr": 0.5})
    before = _row(noted)

    review_mod.resolve_review(noted, note="epoch changed")
    item_id, reason, resolved, stamped, created_at = _row(noted)
    assert resolved is True and (item_id, reason, created_at) == (None, before[1], before[4])
    resolved_at = stamped.pop("resolved_at")
    assert stamped == {**detail, "resolution": "epoch changed"}, "merged, nothing else touched"
    assert datetime.fromisoformat(resolved_at).utcoffset().total_seconds() == 0, "a UTC stamp"

    review_mod.resolve_review(plain)  # no note: the pre-E2 flip, detail byte-identical
    assert _row(plain)[2] is True and _row(plain)[3] == detail
    assert _row(bystander)[2] is False and _row(bystander)[3] == {"baseline_mrr": 0.5}


# --- llm extractor helpers (mock Ollama HTTP) --------------------------------


class _FakeResp:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"message": {"content": self._content}}


def test_items_contradict_parses_and_never_defaults_false(monkeypatch):
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: _FakeResp('{"contradict": true}'))
    assert llm.items_contradict("A", "a", "B", "b") is True

    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: _FakeResp('{"contradict": false}'))
    assert llm.items_contradict("A", "a", "B", "b") is False

    # Ambiguous / unparseable answer must NOT raise — and is NO verdict (None), never a
    # silent False the nightly would memorize as "no conflict" (KB 55589).
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: _FakeResp("not json at all"))
    assert llm.items_contradict("A", "a", "B", "b") is None


def test_summarize_items_returns_content(monkeypatch):
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: _FakeResp("A grounded summary."))
    out = llm.summarize_items("hw:tps55288", [{"title": "t", "body": "b"}])
    assert out == "A grounded summary."
    assert llm.summarize_items("x", []) == ""  # no items → no call, empty summary

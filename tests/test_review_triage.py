"""WP-2 LLM triage of the unverified-extraction backlog (owner decision
2026-07-18): the nightly 14B SUGGESTS keep / reject_noise / reject_duplicate
into review_queue.detail.triage; a HUMAN applies suggestions in bulk. The LLM
is fully mocked (conftest fakes embeddings; _chat / triage_extraction are
monkeypatched here — never the real Ollama). Every applied outcome is a status
flip (Invariant 5); trust upgrades stay human-only.
"""

from __future__ import annotations

import asyncio

import pytest
from typer.testing import CliRunner

from kbcore import db, llm, review_triage
from kbcore import review as review_mod
from kbcore.config import settings
from kbcore.errors import ExtractionFailed, ExtractorUnavailable, InvalidOperation
from kbcore.ingest import ingest
from kbcore.items import get_item, log_access
from kbcore.review import queue_review

runner = CliRunner()


def _row(queue_id: int) -> tuple[bool, dict]:
    with db.connect() as conn:
        r = conn.execute(
            "SELECT resolved, detail FROM review_queue WHERE id = %s", (queue_id,)
        ).fetchone()
    return bool(r[0]), (r[1] if isinstance(r[1], dict) else {})


def _unverified(project: str, tag: str) -> tuple[int, int]:
    """One unverified item + its unverified_extraction review row (WP-1 shape)."""
    item_id = ingest(project, "note", f"note {tag}", f"body {tag}", trust="unverified", uri=tag)
    return item_id, queue_review(item_id, "unverified_extraction", {"rejected_count": 0})


def _store(queue_id: int, action: str, dup_of: int | None = None) -> None:
    assert review_triage.store_triage(
        queue_id, {"action": action, "dup_of": dup_of, "rationale": "test"}
    )


# --- llm._validate_triage / triage_extraction --------------------------------


def test_validate_triage_accepts_and_hardens():
    ok = llm._validate_triage(
        {"action": "keep", "dup_of": None, "rationale": "solid gotcha"}, set()
    )
    assert ok == {"action": "keep", "dup_of": None, "rationale": "solid gotcha"}
    # dup_of is meaningful only for reject_duplicate — nulled otherwise
    assert llm._validate_triage({"action": "reject_noise", "dup_of": 7}, {7})["dup_of"] is None
    dup = llm._validate_triage({"action": "reject_duplicate", "dup_of": 7, "rationale": "x"}, {7})
    assert dup["action"] == "reject_duplicate" and dup["dup_of"] == 7
    # a stringified int is tolerated but must still be a PRESENTED id
    assert llm._validate_triage({"action": "reject_duplicate", "dup_of": "7"}, {7})["dup_of"] == 7


def test_validate_triage_rejects_bad_verdicts():
    assert llm._validate_triage(["keep"], set()) is None  # not a dict
    assert llm._validate_triage({"action": "obliterate"}, set()) is None  # bad enum
    # a hallucinated / absent dup id must never mark anything duplicate
    assert llm._validate_triage({"action": "reject_duplicate", "dup_of": 99}, {7}) is None
    assert llm._validate_triage({"action": "reject_duplicate", "dup_of": None}, {7}) is None
    assert llm._validate_triage({"action": "reject_duplicate", "dup_of": True}, {1}) is None


def test_triage_extraction_parses_validates_and_raises(monkeypatch):
    item = {"id": 1, "project": "p", "type": "note", "title": "t", "body": "b", "tags": []}
    similar = [{"id": 7, "title": "existing", "similarity": 0.99, "status": "active"}]

    monkeypatch.setattr(
        llm,
        "_chat",
        lambda *a, **k: '{"action": "reject_duplicate", "dup_of": 7, "rationale": "same fact"}',
    )
    out = llm.triage_extraction(item, similar, usage_count=0)
    assert out == {"action": "reject_duplicate", "dup_of": 7, "rationale": "same fact"}

    # hallucinated dup id / unknown action → NO suggestion, not an error
    monkeypatch.setattr(llm, "_chat", lambda *a, **k: '{"action": "reject_duplicate", "dup_of": 9}')
    assert llm.triage_extraction(item, similar, usage_count=0) is None
    monkeypatch.setattr(llm, "_chat", lambda *a, **k: '{"action": "purge-everything"}')
    assert llm.triage_extraction(item, similar, usage_count=0) is None

    # unparseable output is content-deterministic → ExtractionFailed (module taxonomy)
    monkeypatch.setattr(llm, "_chat", lambda *a, **k: "no json here at all")
    with pytest.raises(ExtractionFailed):
        llm.triage_extraction(item, similar, usage_count=3)


# --- pending_triage ----------------------------------------------------------


def test_pending_triage_selects_open_untriaged_oldest_first(kb):
    a, qa = _unverified("p", "a")
    b, qb = _unverified("p", "b")
    _c, qc = _unverified("p", "c")
    _d, qd = _unverified("p", "d")
    log_access(b, "kb_get")
    log_access(b, "kb_search")
    review_mod.dismiss(qc)  # resolved → excluded
    _store(qd, "keep")  # already triaged → excluded
    _q_contra = queue_review(a, "contradiction", {"a_id": a, "b_id": b})  # wrong reason

    rows = review_triage.pending_triage(10)
    assert [r["queue_id"] for r in rows] == [qa, qb]  # oldest first
    assert rows[0]["item"]["id"] == a and rows[0]["item"]["title"] == "note a"
    assert rows[0]["item"]["project"] == "p" and rows[0]["item"]["type"] == "note"
    assert rows[0]["usage_count"] == 0
    assert rows[1]["usage_count"] == 2  # access_log counted per item

    assert [r["queue_id"] for r in review_triage.pending_triage(1)] == [qa]  # limit
    assert review_triage.pending_triage(0) == []  # kill switch


# --- store_triage ------------------------------------------------------------


def test_store_triage_merges_and_refuses_resolved(kb):
    _item, q = _unverified("p", "s1")
    assert review_triage.store_triage(q, {"action": "keep", "dup_of": None, "rationale": "why"})
    resolved, detail = _row(q)
    assert resolved is False  # storing a suggestion never resolves the row
    assert detail["rejected_count"] == 0  # sibling detail keys preserved (jsonb merge)
    tri = detail["triage"]
    assert tri["action"] == "keep" and tri["rationale"] == "why"
    assert tri["model"] == settings.kb_extract_model and tri["at"]  # audit stamp

    review_mod.dismiss(q)
    assert review_triage.store_triage(q, {"action": "keep"}) is False  # resolved row refused


# --- apply_triage ------------------------------------------------------------


def test_apply_triage_all_three_actions(kb):
    keep, q_keep = _unverified("p", "keep")
    noise, q_noise = _unverified("p", "noise")
    dup, q_dup = _unverified("p", "dup")
    canonical = ingest("p", "note", "canonical", "the canonical body", uri="canon")
    _store(q_keep, "keep")
    _store(q_noise, "reject_noise")
    _store(q_dup, "reject_duplicate", dup_of=canonical)

    out = dict(review_triage.apply_triage())
    assert out[q_keep].startswith("kept")
    assert out[q_noise] == f"rejected item {noise} (archived)"
    assert out[q_dup] == f"item {dup} marked duplicate of {canonical}"

    # keep: row closed, item untouched — active + unverified (no auto trust bump)
    resolved, detail = _row(q_keep)
    assert resolved is True and detail["auto_resolution"]["kind"] == "triage_keep"
    got = get_item(keep)
    assert got.status == "active" and got.trust == "unverified"

    # reject_noise: item archived (status flip, never a delete) + row closed
    assert _row(q_noise)[0] is True
    assert get_item(noise).status == "archived"

    # reject_duplicate: item duplicate + derived_from relation to the canonical
    resolved, detail = _row(q_dup)
    assert resolved is True
    assert detail["auto_resolution"]["kind"] == "triage_duplicate"
    assert detail["auto_resolution"]["dup_of"] == canonical
    assert get_item(dup).status == "duplicate"
    with db.connect() as conn:
        rel = conn.execute(
            "SELECT 1 FROM relations WHERE src_id = %s AND rel = 'derived_from' AND dst_id = %s",
            (dup, canonical),
        ).fetchone()
    assert rel is not None

    assert review_triage.apply_triage() == []  # idempotent: nothing left to apply


def test_apply_triage_duplicate_edge_paths(kb):
    retired, q_retired = _unverified("p", "retired")
    _bad, q_bad = _unverified("p", "bad")
    canonical = ingest("p", "note", "canon2", "canon2 body", uri="canon2")
    _store(q_retired, "reject_duplicate", dup_of=canonical)
    _store(q_bad, "reject_duplicate", dup_of=999999)  # stale/dangling suggestion
    with db.transaction() as conn:  # retire the item out-of-band (kb_test-owned)
        conn.execute("UPDATE items SET status = 'archived' WHERE id = %s", (retired,))

    out = dict(review_triage.apply_triage())
    # already-retired item: mark_duplicate no-ops → row closes as item_retired
    resolved, detail = _row(q_retired)
    assert out[q_retired] == f"item {retired} already retired; row closed"
    assert resolved is True and detail["auto_resolution"]["kind"] == "item_retired"
    assert get_item(retired).status == "archived"  # item untouched

    # invalid dup_of: error outcome and the row STAYS OPEN for a human
    assert out[q_bad].startswith("error:")
    assert _row(q_bad)[0] is False


def test_apply_triage_filters(kb):
    _a, qa = _unverified("p", "fa")
    _b, qb = _unverified("p", "fb")
    _c, qc = _unverified("other", "fc")
    _store(qa, "keep")
    _store(qb, "reject_noise")
    _store(qc, "keep")

    # actions filter: only the named suggestion kind applies
    out = dict(review_triage.apply_triage(actions={"reject_noise"}))
    assert list(out) == [qb]
    assert _row(qa)[0] is False and _row(qc)[0] is False

    # project filter
    out = dict(review_triage.apply_triage(project="other"))
    assert list(out) == [qc] and _row(qc)[0] is True

    # queue_ids filter
    out = dict(review_triage.apply_triage(queue_ids=[qa]))
    assert list(out) == [qa] and _row(qa)[0] is True

    with pytest.raises(InvalidOperation):
        review_triage.apply_triage(actions={"obliterate"})


# --- run_triage (the shared worker/CLI loop) ---------------------------------


def test_run_triage_shared_loop_taxonomy(kb, monkeypatch):
    rows = [_unverified("p", f"loop{i}") for i in range(4)]
    calls: list[int] = []

    def fake_triage(item, similar, usage_count):
        calls.append(item["id"])
        if len(calls) == 1:
            return {"action": "keep", "dup_of": None, "rationale": "good"}
        if len(calls) == 2:
            raise ExtractionFailed("garbled output")  # skip THIS row, keep going
        raise ExtractorUnavailable("ollama down")  # STOP the batch early

    monkeypatch.setattr(llm, "triage_extraction", fake_triage)
    assert review_triage.run_triage(50) == 1  # count = suggestions stored
    assert calls == [rows[0][0], rows[1][0], rows[2][0]]  # 4th row never attempted

    assert "triage" in _row(rows[0][1])[1]
    for _item, q in rows[1:]:  # failed/stopped rows: open + untriaged, no penalty
        resolved, detail = _row(q)
        assert resolved is False and "triage" not in detail
    # all three stay pending for the next night
    assert [r["queue_id"] for r in review_triage.pending_triage(10)] == [q for _, q in rows[1:]]

    assert review_triage.run_triage(0) == 0  # kill switch (batch knob = 0)


def test_worker_wires_the_shared_loop(kb):
    from kbworker import consolidate as worker

    # main() wires review_triage.run_triage between the moot sweep and summaries
    # (do not run main() — it takes the advisory lock and writes a report).
    assert worker.review_triage is review_triage


# --- CLI ---------------------------------------------------------------------


def test_cli_triage_and_apply(kb, monkeypatch):
    from kbcli.main import app

    _item, q = _unverified("p", "cli-tri")
    monkeypatch.setattr(
        llm,
        "triage_extraction",
        lambda item, similar, usage_count: {"action": "keep", "dup_of": None, "rationale": "r"},
    )
    res = runner.invoke(app, ["review", "triage", "--batch", "10"])
    assert res.exit_code == 0 and "stored 1 triage suggestion" in res.stdout

    res = runner.invoke(app, ["review", "apply-triage"])
    assert res.exit_code == 0
    assert f"[{q}] kept" in res.stdout
    assert "1 applied, 0 error(s), 1 row(s)" in res.stdout
    assert _row(q)[0] is True


# --- MCP layer ---------------------------------------------------------------


def _call(mcp, name, args):
    from fastmcp import Client

    async def run():
        async with Client(mcp) as c:
            res = await c.call_tool(name, args)
            return res.content[0].text

    return asyncio.run(run())


def test_mcp_dismiss_itemless_row_and_bulk_apply_triage(kb):
    from kbmcp.server import mcp

    # dismiss closes an item-less sentinel row (promote/reject never could)
    q_sentinel = queue_review(None, "search_regression", {"note": "hit@5 dropped"})
    text = _call(mcp, "kb_review_resolve", {"queue_id": q_sentinel, "action": "dismiss"})
    assert "dismissed" in text
    assert _row(q_sentinel)[0] is True

    # bulk apply_triage happy path over a stored suggestion
    _item, q = _unverified("p", "mcp-tri")
    _store(q, "keep")
    listing = _call(mcp, "kb_review_list", {})
    assert "triage: keep" in listing  # the suggestion line renders on open rows
    out = _call(mcp, "kb_review_bulk", {"action": "apply_triage"})
    assert f"[{q}] kept" in out
    assert "1 ok, 0 error(s), 1 row(s)" in out
    assert _row(q)[0] is True


def test_toolset_version_bumped_for_new_tool():
    from kbmcp import KB_TOOLSET_VERSION

    assert KB_TOOLSET_VERSION == "2026-07-18"  # kb_review_bulk + dismiss action

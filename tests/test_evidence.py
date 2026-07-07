"""Contradiction evidence/weightage (kbcore.evidence + nightly step).

Real DB (kb_test); the LLM judge is always mocked. Embeddings are forced via
lead-chunk updates (test_phase4_consolidate.py pattern) so graph.similar()
returns controlled candidates.
"""

from __future__ import annotations

import pytest

from kbcore import db, evidence, llm
from kbcore.config import settings
from kbcore.ingest import ingest
from kbcore.review import queue_review


def _vec(*head: float) -> str:
    v = [float(x) for x in head] + [0.0] * (settings.kb_embed_dim - len(head))
    return "[" + ",".join(repr(x) for x in v) + "]"


def _force_lead(item_id: int, vec: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE chunks SET embedding = %s::vector WHERE item_id = %s AND ord = 0",
            (vec, item_id),
        )


# --- llm.evidence_side coercion ------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"side": "a"}', "a"),
        ('{"side": "B"}', "b"),
        ('{"side": " b "}', "b"),
        ('{"side": "both"}', "neither"),
        ('{"side": "neither"}', "neither"),
        ("not json at all", "neither"),
        ("[1, 2]", "neither"),
        ('<think>hmm</think>{"side": "a"}', "a"),  # think-wrapper path
    ],
)
def test_evidence_side_coerces_answer(monkeypatch, content, expected):
    monkeypatch.setattr(llm, "_chat", lambda *a, **k: content)
    assert llm.evidence_side("at", "ab", "bt", "bb", "ct", "cb") == expected


# --- gather_evidence ------------------------------------------------------------


@pytest.fixture()
def contradiction_pair(kb):
    """Items a & b (the pair) + candidates near them, with forced embeddings."""
    a = ingest("evproj", "note", "use mode 0", "the sensor needs SPI mode 0", uri="a")
    b = ingest("evproj", "note", "use mode 1", "the sensor needs SPI mode 1", uri="b")
    # verified candidate (default trust) + unverified candidate, in ANOTHER
    # project — similar() is cross-project, the contradiction scan is not.
    c_ver = ingest("evother", "note", "datasheet says mode 0", "vendor datasheet: mode 0", uri="c")
    c_unv = ingest(
        "evother", "note", "session said mode 1", "we saw mode 1 work", uri="d", trust="unverified"
    )
    # Geometry matters: the pair must land in the CONTRADICTION band [0.80,
    # 0.95) — not the dedupe range (>=0.97, which runs first and would flip one
    # item to 'duplicate'). Same-project candidate pairs must stay < 0.97 too.
    _force_lead(a, _vec(1.0))
    _force_lead(b, _vec(0.88, 0.4749736))  # cos(a,b)=0.88 — in the band
    _force_lead(c_ver, _vec(0.95, 0.312))  # cos(c_ver,c_unv)=0.82 — no dedupe
    _force_lead(c_unv, _vec(0.6, 0.8))
    return {"a": a, "b": b, "c_ver": c_ver, "c_unv": c_unv}


def test_gather_evidence_counts_and_weights(contradiction_pair, monkeypatch):
    ids = contradiction_pair

    def judge(a_title, a_body, b_title, b_body, c_title, c_body):
        return {"datasheet says mode 0": "a", "session said mode 1": "b"}.get(c_title, "neither")

    monkeypatch.setattr(llm, "evidence_side", judge)
    d = evidence.gather_evidence(ids["a"], ids["b"])
    assert d["a_id"] == ids["a"] and d["b_id"] == ids["b"]
    assert d["in_favor_a"] == 1 and d["in_favor_b"] == 1
    assert d["weighted_a"] == 2, "verified evidence weighs 2"
    assert d["weighted_b"] == 1, "unverified evidence weighs 1"
    by_id = {e["item_id"]: e for e in d["evidence"]}
    assert by_id[ids["c_ver"]]["side"] == "a" and by_id[ids["c_ver"]]["trust"] == "verified"
    assert by_id[ids["c_unv"]]["side"] == "b" and by_id[ids["c_unv"]]["trust"] == "unverified"
    assert d["evidence_updated"]  # iso timestamp present


def test_gather_excludes_summaries_and_caps_judge_calls(contradiction_pair, monkeypatch):
    ids = contradiction_pair
    # a near summary must never reach the judge
    s = ingest("evother", "summary", "mode summary", "summary of the mode debate", uri="s")
    _force_lead(s, _vec(0.9995, 0.0316))
    # flood with candidates beyond the cap
    extra = []
    for i in range(12):
        e = ingest("evother", "note", f"filler {i}", f"filler body {i}", uri=f"f{i}")
        _force_lead(e, _vec(0.99, 0.05 + i * 0.001))
        extra.append(e)

    seen_titles: list[str] = []

    def judge(a_title, a_body, b_title, b_body, c_title, c_body):
        seen_titles.append(c_title)
        return "neither"

    monkeypatch.setattr(llm, "evidence_side", judge)
    d = evidence.gather_evidence(ids["a"], ids["b"])
    assert len(seen_titles) <= evidence.MAX_CANDIDATES
    assert "mode summary" not in seen_titles, "summary items are excluded from evidence"
    assert d["in_favor_a"] == 0 and d["weighted_a"] == 0


# --- nightly step integration -----------------------------------------------------


def _pinned_contradict(a_title: str) -> object:
    """items_contradict mock pinned to the exact pair titles so filler/candidate
    items can never accidentally trip the contradiction band."""

    def judge(at, ab, bt, bb):
        return {at, bt} == {"use mode 0", "use mode 1"}

    return judge


def test_new_contradiction_gets_evidence_same_night(contradiction_pair, monkeypatch):
    from kbworker import consolidate

    ids = contradiction_pair
    monkeypatch.setattr(llm, "items_contradict", _pinned_contradict("use mode 0"))
    monkeypatch.setattr(
        llm,
        "evidence_side",
        lambda at, ab, bt, bb, ct, cb: {
            "datasheet says mode 0": "a",
            "session said mode 1": "b",
        }.get(ct, "neither"),
    )
    monkeypatch.setattr(llm, "summarize_items", lambda topic, items: "")
    result = consolidate.main()
    assert result["contradictions_queued"] == 1
    assert result["evidence_refreshed"] == 1
    with db.connect() as conn:
        detail = conn.execute(
            "SELECT detail FROM review_queue WHERE reason='contradiction' AND resolved=false "
            "AND (detail->>'a_id')::bigint = %s",
            (ids["a"],),
        ).fetchone()[0]
    assert detail["in_favor_a"] == 1 and detail["weighted_a"] == 2
    assert detail["in_favor_b"] == 1 and detail["weighted_b"] == 1
    assert detail["evidence_updated"]
    # the count lands in the nightly report meta
    with db.connect() as conn:
        meta = conn.execute(
            "SELECT meta FROM items WHERE type='report' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert meta["evidence_refreshed"] == 1


def test_refresh_grows_counts_on_next_run(contradiction_pair, monkeypatch):
    from kbworker import consolidate

    ids = contradiction_pair
    monkeypatch.setattr(llm, "items_contradict", _pinned_contradict("use mode 0"))
    monkeypatch.setattr(llm, "summarize_items", lambda topic, items: "")
    sides = {"datasheet says mode 0": "a", "session said mode 1": "b"}
    monkeypatch.setattr(
        llm, "evidence_side", lambda at, ab, bt, bb, ct, cb: sides.get(ct, "neither")
    )
    consolidate.main()
    # a new corroborating VERIFIED item lands (nightly seed / session flush) —
    # in a THIRD project so it can't dedupe against the existing candidates.
    extra = ingest("evthird", "note", "field report favors mode 0", "mode 0 confirmed", uri="x")
    _force_lead(extra, _vec(0.7, 0.7141428))
    sides["field report favors mode 0"] = "a"
    result = consolidate.main()
    assert result["evidence_refreshed"] == 1
    with db.connect() as conn:
        detail = conn.execute(
            "SELECT detail FROM review_queue WHERE reason='contradiction' AND resolved=false "
            "AND (detail->>'a_id')::bigint = %s",
            (ids["a"],),
        ).fetchone()[0]
    assert detail["in_favor_a"] == 2 and detail["weighted_a"] == 4, "counts grew on refresh"


def test_evidence_cap_defers_and_logs(contradiction_pair, monkeypatch, caplog):
    from kbworker import consolidate

    ids = contradiction_pair
    # two open contradiction rows: the real pair + a synthetic one
    from kbcore.consolidation import record_contradiction

    record_contradiction(ids["a"], ids["b"])
    record_contradiction(ids["c_ver"], ids["c_unv"])
    monkeypatch.setattr(consolidate, "EVIDENCE_PAIR_CAP", 1)
    monkeypatch.setattr(llm, "evidence_side", lambda *a: "neither")
    with caplog.at_level("INFO", logger="kbworker.consolidate"):
        refreshed, _auto = consolidate._evidence()
    assert refreshed == 1, "cap limits the refresh to one pair"
    assert any("capped" in r.message for r in caplog.records), "deferral is logged"


def test_render_and_cli_show_evidence_counts(kb):
    from typer.testing import CliRunner

    from kbcli.main import app
    from kbcore.models import ReviewEntry
    from kbcore.render import render_review

    e = ReviewEntry(
        queue_id=7,
        reason="contradiction",
        item_id=1,
        title="pair a",
        status="active",
        trust="verified",
        detail={"in_favor_a": 3, "weighted_a": 5, "in_favor_b": 1, "weighted_b": 1},
    )
    out = render_review([e])
    assert "evidence: A 3 (w5) vs B 1 (w1)" in out
    # entries without counts render unchanged (single line)
    plain = ReviewEntry(queue_id=8, reason="unverified_extraction", item_id=2, detail={})
    assert "evidence:" not in render_review([plain])
    # the CLI inherits via render_review
    item = ingest("evcli", "note", "cli pair", "b", uri="cli", trust="unverified")
    qid = queue_review(item, "contradiction", {"a_id": item, "b_id": item})
    evidence.update_review_detail(
        qid, {"in_favor_a": 2, "weighted_a": 4, "in_favor_b": 0, "weighted_b": 0}
    )
    result = CliRunner().invoke(app, ["review", "list"])
    assert result.exit_code == 0
    assert "evidence: A 2 (w4) vs B 0 (w0)" in result.output


def test_web_review_shows_evidence_block(kb, monkeypatch):
    from starlette.testclient import TestClient

    from kbcore.config import settings as cfg

    token = "kbtest-" + "0" * 32
    monkeypatch.setattr(cfg, "kb_token", token)
    monkeypatch.setattr(cfg, "kb_bind", "127.0.0.1")
    item = ingest("evweb", "note", "web pair a", "b", uri="w", trust="unverified")
    qid = queue_review(item, "contradiction", {"a_id": item, "b_id": item})
    evidence.update_review_detail(
        qid,
        {
            "in_favor_a": 2,
            "weighted_a": 4,
            "in_favor_b": 1,
            "weighted_b": 1,
            "evidence": [{"item_id": item, "side": "a", "trust": "verified", "title": "web ev"}],
        },
    )
    from kbweb.server import build_app

    client = TestClient(build_app())
    client.post("/login", data={"token": token})
    r = client.get("/review")
    assert r.status_code == 200
    assert "Evidence:" in r.text
    assert "A\n      2 (w4)" in r.text or "2 (w4)" in r.text
    assert "web ev" in r.text and f"/item/{item}" in r.text


def test_update_review_detail_merges_open_rows_only(kb):
    item = ingest("evq", "note", "queued thing", "body", uri="q", trust="unverified")
    qid = queue_review(item, "contradiction", {"a_id": item, "b_id": item + 1})
    assert evidence.update_review_detail(qid, {"in_favor_a": 3, "weighted_a": 5}) is True
    with db.connect() as conn:
        detail = conn.execute("SELECT detail FROM review_queue WHERE id = %s", (qid,)).fetchone()[0]
    assert detail["a_id"] == item, "original keys survive the JSONB merge"
    assert detail["in_favor_a"] == 3 and detail["weighted_a"] == 5
    # resolved rows are never touched
    with db.transaction() as conn:
        conn.execute("UPDATE review_queue SET resolved = true WHERE id = %s", (qid,))
    assert evidence.update_review_detail(qid, {"in_favor_a": 99}) is False
    with db.connect() as conn:
        detail = conn.execute("SELECT detail FROM review_queue WHERE id = %s", (qid,)).fetchone()[0]
    assert detail["in_favor_a"] == 3


# --- doc-refresh sweep (auto-resolution tier 1b) ------------------------------------


def _mk_revisions(kb, uri="rm3:docs/X.md"):
    """Two ACTIVE items for the same git-doc uri (backlog shape, hook bypassed)."""
    old = ingest("sweep", "spec", "docs/X.md", "old revision content", kind="git", uri=uri)
    new = ingest("sweep", "spec", "docs/X.md", "new revision content", kind="git", uri=uri)
    return old, new


def test_doc_refresh_sweep_resolves_backlog_and_closes_rows(kb, monkeypatch):
    from kbcore.consolidation import record_contradiction
    from kbworker import consolidate

    monkeypatch.setattr(llm, "items_contradict", lambda *a: False)
    monkeypatch.setattr(llm, "summarize_items", lambda *a: "")
    old, new = _mk_revisions(kb)
    record_contradiction(old, new)  # the moot row the sweep must close
    result = consolidate.main()
    assert result["doc_refresh_resolved"] == 1
    with db.connect() as conn:
        status, by = conn.execute(
            "SELECT status, superseded_by FROM items WHERE id = %s", (old,)
        ).fetchone()
        assert status == "superseded" and by == new
        open_rows = conn.execute(
            "SELECT count(*) FROM review_queue WHERE reason='contradiction' AND resolved=false"
        ).fetchone()[0]
        assert open_rows == 0, "moot contradiction row auto-closed"
        meta = conn.execute(
            "SELECT meta FROM items WHERE type='report' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert meta["doc_refresh_resolved"] == 1


def test_doc_refresh_closes_reversed_pair_row(kb, monkeypatch):
    from kbcore.consolidation import record_contradiction
    from kbworker import consolidate

    monkeypatch.setattr(llm, "items_contradict", lambda *a: False)
    monkeypatch.setattr(llm, "summarize_items", lambda *a: "")
    old, new = _mk_revisions(kb, uri="rm3:docs/Y.md")
    record_contradiction(new, old)  # reversed order in the detail
    consolidate.main()
    with db.connect() as conn:
        open_rows = conn.execute(
            "SELECT count(*) FROM review_queue WHERE reason='contradiction' AND resolved=false"
        ).fetchone()[0]
    assert open_rows == 0


def test_doc_refresh_beats_dedupe(kb, monkeypatch):
    """A revision pair at dup-range cosine must end 'superseded', never 'duplicate'."""
    from kbworker import consolidate

    monkeypatch.setattr(llm, "items_contradict", lambda *a: False)
    monkeypatch.setattr(llm, "summarize_items", lambda *a: "")
    old, new = _mk_revisions(kb, uri="rm3:docs/Z.md")
    _force_lead(old, _vec(1.0))
    _force_lead(new, _vec(1.0))  # cosine 1.0 — squarely in dedupe range
    consolidate.main()
    with db.connect() as conn:
        status = conn.execute("SELECT status FROM items WHERE id = %s", (old,)).fetchone()[0]
    assert status == "superseded", f"expected supersession semantics, got {status!r}"


def test_doc_refresh_sweep_flag_off(kb, monkeypatch):
    from kbworker import consolidate

    monkeypatch.setattr(llm, "items_contradict", lambda *a: False)
    monkeypatch.setattr(llm, "summarize_items", lambda *a: "")
    monkeypatch.setattr(settings, "kb_auto_supersede_doc_refresh", False)
    monkeypatch.setattr(settings, "kb_dup_threshold", 1.01)  # keep dedupe out of the way
    old, new = _mk_revisions(kb, uri="rm3:docs/W.md")
    result = consolidate.main()
    assert result["doc_refresh_resolved"] == 0
    with db.connect() as conn:
        status = conn.execute("SELECT status FROM items WHERE id = %s", (old,)).fetchone()[0]
    assert status == "active"


# --- evidence auto-resolve (auto-resolution tier 2) ----------------------------------


def test_suggestion_thresholds():
    from kbcore.evidence import suggestion_for

    # decisive: 2 supporters, w4 vs w0
    s = suggestion_for(1, 2, {"a": 2, "b": 0}, {"a": 4, "b": 0})
    assert s and s["keep"] == 1 and s["retire"] == 2 and s["action"] == "supersede"
    # winner is B: keep/retire swap
    s = suggestion_for(1, 2, {"a": 0, "b": 3}, {"a": 0, "b": 6})
    assert s and s["keep"] == 2 and s["retire"] == 1
    # one supporter — not enough, however heavy
    assert suggestion_for(1, 2, {"a": 1, "b": 0}, {"a": 4, "b": 0}) is None
    # weight below floor
    assert suggestion_for(1, 2, {"a": 2, "b": 0}, {"a": 2, "b": 0}) is None
    # margin below 3x
    assert suggestion_for(1, 2, {"a": 2, "b": 1}, {"a": 4, "b": 2}) is None


@pytest.fixture()
def lopsided_pair(contradiction_pair, monkeypatch):
    """The contradiction_pair plus a second VERIFIED supporter of side A, and
    judges mapped so evidence is decisively lopsided toward A."""
    ids = contradiction_pair
    extra = ingest("evx", "note", "second datasheet also mode 0", "confirmed mode 0", uri="x2")
    _force_lead(extra, _vec(0.93, 0.3676955))
    monkeypatch.setattr(llm, "items_contradict", _pinned_contradict("use mode 0"))
    monkeypatch.setattr(llm, "summarize_items", lambda *a: "")
    monkeypatch.setattr(
        llm,
        "evidence_side",
        lambda at, ab, bt, bb, ct, cb: {
            "datasheet says mode 0": "a",
            "second datasheet also mode 0": "a",
        }.get(ct, "neither"),
    )
    return ids


def test_two_night_streak_auto_resolves(lopsided_pair):
    from kbworker import consolidate

    ids = lopsided_pair
    # NIGHT 1: contradiction flagged, suggestion recorded, streak 1, NO action
    r1 = consolidate.main()
    assert r1["contradictions_queued"] == 1
    assert r1["evidence_auto_resolved"] == 0
    with db.connect() as conn:
        detail = conn.execute(
            "SELECT detail FROM review_queue WHERE reason='contradiction' AND resolved=false"
        ).fetchone()[0]
    assert detail["suggested_resolution"]["keep"] == ids["a"]
    assert detail["suggestion_streak"] == 1
    # NIGHT 2: same evidence -> auto-resolved
    r2 = consolidate.main()
    assert r2["evidence_auto_resolved"] == 1
    with db.connect() as conn:
        status, by = conn.execute(
            "SELECT status, superseded_by FROM items WHERE id = %s", (ids["b"],)
        ).fetchone()
        open_rows = conn.execute(
            "SELECT count(*) FROM review_queue WHERE reason='contradiction' AND resolved=false"
        ).fetchone()[0]
        meta = conn.execute(
            "SELECT meta FROM items WHERE type='report' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert status == "superseded" and by == ids["a"], "loser retired by winner"
    assert open_rows == 0
    assert meta["evidence_auto_resolved"] == 1


def test_streak_resets_when_evidence_flips(lopsided_pair, monkeypatch):
    from kbworker import consolidate

    ids = lopsided_pair
    consolidate.main()  # night 1: suggestion for A, streak 1
    # night 2: evidence flips to B (e.g. new docs landed) -> streak resets, no action
    monkeypatch.setattr(
        llm,
        "evidence_side",
        lambda at, ab, bt, bb, ct, cb: {
            "datasheet says mode 0": "b",
            "second datasheet also mode 0": "b",
        }.get(ct, "neither"),
    )
    r2 = consolidate.main()
    assert r2["evidence_auto_resolved"] == 0
    with db.connect() as conn:
        detail = conn.execute(
            "SELECT detail FROM review_queue WHERE reason='contradiction' AND resolved=false"
        ).fetchone()[0]
    assert detail["suggested_resolution"]["keep"] == ids["b"], "suggestion flipped"
    assert detail["suggestion_streak"] == 1, "streak reset on flip"


def test_auto_resolve_flag_off_suggests_but_never_acts(lopsided_pair, monkeypatch):
    from kbworker import consolidate

    monkeypatch.setattr(settings, "kb_auto_resolve_evidence", False)
    consolidate.main()
    r2 = consolidate.main()
    assert r2["evidence_auto_resolved"] == 0
    with db.connect() as conn:
        detail = conn.execute(
            "SELECT detail FROM review_queue WHERE reason='contradiction' AND resolved=false"
        ).fetchone()[0]
    assert detail["suggestion_streak"] >= 2, "streak accumulates"
    assert detail["suggested_resolution"], "suggestion stays visible"


def test_apply_suggestion_manual_early(lopsided_pair):
    from kbcore.review import apply_suggestion
    from kbworker import consolidate

    ids = lopsided_pair
    consolidate.main()  # streak 1 — auto hasn't fired yet
    with db.connect() as conn:
        qid = conn.execute(
            "SELECT id FROM review_queue WHERE reason='contradiction' AND resolved=false"
        ).fetchone()[0]
    retired = apply_suggestion(qid)
    assert retired == ids["b"]
    with db.connect() as conn:
        status = conn.execute("SELECT status FROM items WHERE id = %s", (ids["b"],)).fetchone()[0]
        resolved = conn.execute(
            "SELECT resolved FROM review_queue WHERE id = %s", (qid,)
        ).fetchone()[0]
    assert status == "superseded" and resolved is True
    # applying again / no-suggestion rows raise cleanly
    import pytest as _pytest

    from kbcore.errors import InvalidOperation

    with _pytest.raises(InvalidOperation):
        apply_suggestion(qid)


def test_render_and_cli_show_suggestion(kb):
    from typer.testing import CliRunner

    from kbcli.main import app
    from kbcore.models import ReviewEntry
    from kbcore.render import render_review

    e = ReviewEntry(
        queue_id=9,
        reason="contradiction",
        item_id=1,
        detail={
            "in_favor_a": 2,
            "weighted_a": 4,
            "in_favor_b": 0,
            "weighted_b": 0,
            "suggested_resolution": {"action": "supersede", "keep": 1, "retire": 2, "rule": "r"},
            "suggestion_streak": 1,
        },
    )
    out = render_review([e])
    assert "suggested: supersede item 2 → keep item 1" in out
    assert "kb review apply 9" in out
    # CLI apply --all executes open suggestions
    a = ingest("applyall", "note", "keepme", "k", uri="ka")
    b = ingest("applyall", "note", "retireme", "r", uri="rb")
    qid = queue_review(a, "contradiction", {"a_id": a, "b_id": b})
    evidence.update_review_detail(
        qid,
        {"suggested_resolution": {"action": "supersede", "keep": a, "retire": b, "rule": "t"}},
    )
    result = CliRunner().invoke(app, ["review", "apply", "--all"])
    assert result.exit_code == 0
    assert "applied 1 suggestion(s)" in result.output
    with db.connect() as conn:
        status = conn.execute("SELECT status FROM items WHERE id = %s", (b,)).fetchone()[0]
    assert status == "superseded"


def test_web_review_shows_suggestion_block(kb, monkeypatch):
    from starlette.testclient import TestClient

    token = "kbtest-" + "0" * 32
    monkeypatch.setattr(settings, "kb_token", token)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    a = ingest("webs", "note", "web keep", "k", uri="wk", trust="unverified")
    qid = queue_review(a, "contradiction", {"a_id": a, "b_id": a})
    evidence.update_review_detail(
        qid,
        {
            "suggested_resolution": {
                "action": "supersede",
                "keep": a,
                "retire": a + 1,
                "rule": "t",
            },
            "suggestion_streak": 1,
        },
    )
    from kbweb.server import build_app

    client = TestClient(build_app())
    client.post("/login", data={"token": token})
    r = client.get("/review")
    assert r.status_code == 200
    assert "Suggested:" in r.text and f"kb review apply {qid}" in r.text

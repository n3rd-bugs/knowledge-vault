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
    # Cluster everything near the pair so similar() finds the candidates.
    _force_lead(a, _vec(1.0))
    _force_lead(b, _vec(0.999, 0.0447))
    _force_lead(c_ver, _vec(0.998, 0.0632))
    _force_lead(c_unv, _vec(0.997, 0.0774))
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

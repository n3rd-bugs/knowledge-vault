"""Pipeline v2 WP-B2 — ensemble/converge/verify stage (real DB kb_test).

All LLM calls (extractors, factored judge) and embeddings are monkeypatched —
mock only Ollama, per CLAUDE.md. Covers: v1 byte-identical when the ensemble is
off (spy-asserted), cross-model clustering at the measured 0.80 threshold +
threshold sensitivity, the survivor policy matrix (primary-source /
corroboration / primary-judge / judge-fail / judge-error), representative
choice, shadow mode (DB-identical to v1 + the one-line report), live mode
(meta.extraction_pipeline, review rows, idempotence — hard rule 3), and the
quote gate running before policy even in live mode.
"""

from __future__ import annotations

import logging
import math

import pytest

from kbcore import db, embed, llm
from kbcore.config import settings
from kbcore.errors import ExtractionFailed, ExtractorUnavailable
from kbcore.items import get_item
from kbworker import consolidate, ensemble
from kbworker.sessions import extract_and_ingest
from tests.conftest import fake_vector

pytestmark = pytest.mark.usefixtures("kb")

POOL_SPAN = (
    "Decision: pin the psycopg pool to 8 connections; larger pools starved the "
    "embedder during the nightly drain."
)
TMP_SPAN = (
    "Gotcha: systemd PrivateTmp=yes hid /tmp/kb.sock from the worker, so the "
    "queue drain silently stalled."
)
RERANK_SPAN = (
    "Note: the reranker sidecar must load the official Q4_K_M GGUF; community "
    "conversions return garbage scores."
)
TEXT = (
    "Session transcript for the WP-B2 bench.\n\n"
    f"{POOL_SPAN}\n\n{TMP_SPAN}\n\n{RERANK_SPAN}\n\nEnd of session."
)
# Near-miss: fails the exact check, fuzzy-repairs at ratio >= 0.95 (WP-B1).
NEAR_MISS = TMP_SPAN.replace("PrivateTmp=yes", "PrivateTmp = yes").rstrip(".")
FABRICATED_QUOTE = "the moon is made of green cheese and telepathy"


def _item(title: str, body: str, quote: str, type_: str = "note") -> dict:
    return {"type": type_, "title": title, "body": body, "quote": quote, "tags": []}


SHARED = _item("pool size decision", "Pin the psycopg pool to 8.", POOL_SPAN, "decision")
P_ONLY = _item("primary singleton", "PrivateTmp hides /tmp sockets from services.", TMP_SPAN)
A_ONLY = _item("granite singleton", "Use the official GGUF for the reranker sidecar.", RERANK_SPAN)
B_ONLY = _item("gemma singleton", "Queue drain stalls trace back to socket visibility.", TMP_SPAN)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """Pin every knob the ensemble reads; keep raw archives off the real ~/kb."""
    monkeypatch.setattr(settings, "kb_raw", tmp_path / "kbraw")
    monkeypatch.setattr(settings, "kb_extract_model", "prime")
    monkeypatch.setattr(settings, "kb_quote_repair_min_ratio", 0.95)
    monkeypatch.setattr(settings, "kb_ensemble_cluster_threshold", 0.80)
    monkeypatch.setattr(settings, "kb_ensemble_shadow", True)
    monkeypatch.setattr(settings, "kb_extract_ensemble", "")
    ensemble.reset_shadow_counter()


def _install_extractors(monkeypatch, per_model: dict) -> list[dict]:
    """Fake llm.extract_items keyed by model; an Exception value raises."""
    calls: list[dict] = []

    def fake(text, *, model=None, keep_alive=None):
        m = model or settings.kb_extract_model
        calls.append({"model": m, "keep_alive": keep_alive})
        out = per_model.get(m, [])
        if isinstance(out, Exception):
            raise out
        return [dict(it) for it in out]

    monkeypatch.setattr(llm, "extract_items", fake)
    return calls


def _install_judge(monkeypatch, supported=frozenset(), errors=frozenset()) -> list[dict]:
    """Fake llm.verify_item: verdict by item title; titles in `errors` raise."""
    calls: list[dict] = []

    def fake(title, body, transcript, *, judge_model=None, keep_alive=None):
        calls.append({"title": title, "judge_model": judge_model})
        if title in errors:
            raise ExtractionFailed("judge blew up (test)")
        return {"supported": title in supported, "reason": "test verdict"}

    monkeypatch.setattr(llm, "verify_item", fake)
    return calls


def _ctext(it: dict) -> str:
    return f"{it['title']}: {it['body']}"


def _install_vectors(monkeypatch, vec_map: dict) -> None:
    """Controlled clustering geometry; unmapped texts (item chunks during ingest)
    fall back to the deterministic conftest vectors."""
    monkeypatch.setattr(
        embed, "embed_texts", lambda texts: [vec_map.get(t, fake_vector(t)) for t in texts]
    )


def _session_items() -> dict:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT i.id, i.title FROM items i JOIN sources s ON s.id = i.source_id "
            "WHERE s.kind = 'session'"
        ).fetchall()
    return {title: get_item(item_id) for item_id, title in rows}


def _counts() -> tuple[int, int, int]:
    with db.connect() as conn:
        items = conn.execute("SELECT count(*) FROM items").fetchone()[0]
        sources = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
        queue = conn.execute("SELECT count(*) FROM review_queue").fetchone()[0]
    return items, sources, queue


# --- v1 untouched when the ensemble is off -----------------------------------


def _v1_extractor(text: str) -> list[dict]:
    return [
        _item("exact item", "grounded exactly", POOL_SPAN, "decision"),
        _item("near-miss item", "grounded after repair", NEAR_MISS, "gotcha"),
        _item("fabricated item", "invented claim", FABRICATED_QUOTE),
    ]


def test_v1_untouched_when_ensemble_empty(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("no ensemble/LLM call may happen when kb_extract_ensemble is empty")

    monkeypatch.setattr(ensemble, "run_ensemble", _boom)  # spy: must never fire
    monkeypatch.setattr(llm, "extract_items", _boom)  # v1 must use the passed extractor
    assert ensemble.mode() == "off"

    saved, rejected = extract_and_ingest(TEXT, "proj", "test://wpb2/v1", extractor=_v1_extractor)
    assert (saved, rejected) == (2, 1), "exact + repaired save, fabricated rejects — as before"

    items = _session_items()
    assert set(items) == {"exact item", "near-miss item"}
    for it in items.values():
        assert "extraction_pipeline" not in it.meta, "v1 meta is unchanged"
    assert items["near-miss item"].meta["quote_repair"]["ratio"] >= 0.95
    assert ensemble.shadow_survivor_total() == 0


# --- clustering (converge step) ----------------------------------------------


def test_clustering_unions_across_models(monkeypatch):
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a,ex-b")
    calls = _install_extractors(
        monkeypatch,
        {
            "prime": [SHARED, P_ONLY],
            "ex-a": [dict(SHARED), A_ONLY],
            "ex-b": [dict(SHARED), B_ONLY],
        },
    )
    _install_judge(monkeypatch, supported={A_ONLY["title"], B_ONLY["title"]})

    report = ensemble.run_ensemble(TEXT, "proj", "test://wpb2/cluster")

    assert report["candidates"] == {"prime": 2, "ex-a": 2, "ex-b": 2}
    assert report["usable"] == {"prime": 2, "ex-a": 2, "ex-b": 2}
    assert report["clusters"] == 4, "one 3-model cluster + three singletons"
    assert {"models": ["prime", "ex-a", "ex-b"], "size": 3} in report["cluster_detail"]
    assert report["corroborated"] == 1
    titles = {s["item"]["title"] for s in report["survivors"]}
    assert titles == {
        "pool size decision",
        "primary singleton",
        "granite singleton",
        "gemma singleton",
    }
    # Invariant 8: extras unload immediately; the primary keeps the settings
    # default (None sentinel = resident keep_alive).
    assert {c["model"]: c["keep_alive"] for c in calls} == {"prime": None, "ex-a": 0, "ex-b": 0}


def test_cluster_threshold_sensitivity(monkeypatch):
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    near_a = _item("pool tuning", "Pin the pool to 8.", POOL_SPAN, "decision")
    near_b = _item("pool sizing", "A pool of 8 connections.", POOL_SPAN, "decision")
    # Near-parallel vectors: cosine exactly 0.90 — merges at 0.80, splits at 0.99.
    _install_vectors(
        monkeypatch,
        {_ctext(near_a): [1.0, 0.0], _ctext(near_b): [0.9, math.sqrt(1 - 0.9**2)]},
    )
    _install_extractors(monkeypatch, {"prime": [near_a], "ex-a": [near_b]})
    judge_calls = _install_judge(monkeypatch)

    report = ensemble.run_ensemble(TEXT, "proj", "test://wpb2/thresh")
    assert (report["clusters"], report["corroborated"]) == (1, 1)
    assert judge_calls == [], "a corroborated cluster never reaches the judge"

    monkeypatch.setattr(settings, "kb_ensemble_cluster_threshold", 0.99)
    report2 = ensemble.run_ensemble(TEXT, "proj", "test://wpb2/thresh")
    assert (report2["clusters"], report2["corroborated"]) == (2, 0), "0.99 fragments the pair"


# --- survivor policy matrix (e) ----------------------------------------------


def test_policy_matrix(monkeypatch):
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a,ex-b")
    shared_a = _item("corroborated fact", "Two models found this fact.", POOL_SPAN)
    judge_err = _item("judge error item", "judge infrastructure dies here", RERANK_SPAN)
    judge_fail = _item("judge fail item", "claim the judge rejects", RERANK_SPAN)
    judge_pass = _item("judge pass item", "claim the judge supports", RERANK_SPAN)
    _install_extractors(
        monkeypatch,
        {
            "prime": [P_ONLY],
            # judge_err BEFORE judge_pass: a judge error must not abort the run.
            "ex-a": [shared_a, judge_err, judge_fail, judge_pass],
            "ex-b": [dict(shared_a)],
        },
    )
    judge_calls = _install_judge(
        monkeypatch, supported={"judge pass item"}, errors={"judge error item"}
    )

    report = ensemble.run_ensemble(TEXT, "proj", "test://wpb2/policy")

    by_title = {s["item"]["title"]: s["extraction_pipeline"] for s in report["survivors"]}
    assert set(by_title) == {"primary singleton", "corroborated fact", "judge pass item"}
    assert by_title["primary singleton"]["verified_by"] == "primary-source"
    assert by_title["corroborated fact"]["verified_by"] == "corroboration"
    assert by_title["corroborated fact"]["corroboration"] == 2
    assert by_title["judge pass item"]["verified_by"] == "primary-judge"

    judged = sorted(c["title"] for c in judge_calls)
    assert judged == ["judge error item", "judge fail item", "judge pass item"], (
        "only non-primary singletons are judged — never corroborated or primary items"
    )
    assert all(c["judge_model"] == "prime" for c in judge_calls), "the judge IS the primary"
    assert report["judged"] == 3
    assert report["judge_errors"] == 1, "judge error counted, run continued"
    assert report["rejected_reps"] == 2


def test_secondary_judge_for_primary_singletons(monkeypatch):
    """kb_ensemble_secondary_judge set: primary SINGLETONS get a cross-family
    judge; a judge FAIL still survives (demote-to-review, flagged), and
    corroborated primary items are never judged."""
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    monkeypatch.setattr(settings, "kb_ensemble_secondary_judge", "second")
    p_fail = _item("primary judged fail", "claim second rejects", RERANK_SPAN)
    _install_extractors(
        monkeypatch,
        {"prime": [SHARED, P_ONLY, p_fail], "ex-a": [dict(SHARED)]},
    )
    judge_calls = _install_judge(monkeypatch, supported={"primary singleton"})

    report = ensemble.run_ensemble(TEXT, "proj", "test://wpb2/secondary")

    by_title = {s["item"]["title"]: s["extraction_pipeline"] for s in report["survivors"]}
    # ALL three survive — a secondary-judge fail never drops a primary item.
    assert set(by_title) == {"pool size decision", "primary singleton", "primary judged fail"}
    # Corroborated primary rep: label stays 'primary-source' (pinned contract);
    # corroboration only exempts it from the secondary judge.
    assert by_title["pool size decision"]["verified_by"] == "primary-source"
    assert by_title["primary singleton"]["verified_by"] == "secondary-judge"
    assert by_title["primary judged fail"]["verified_by"] == "secondary-judge-failed"
    judged = sorted(c["title"] for c in judge_calls)
    assert judged == ["primary judged fail", "primary singleton"], (
        "only primary SINGLETONS reach the secondary judge"
    )
    assert all(c["judge_model"] == "second" for c in judge_calls)


def test_secondary_judge_unset_keeps_old_behavior(monkeypatch):
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    _install_extractors(monkeypatch, {"prime": [P_ONLY], "ex-a": []})
    judge_calls = _install_judge(monkeypatch)
    report = ensemble.run_ensemble(TEXT, "proj", "test://wpb2/secondary-off")
    (surv,) = report["survivors"]
    assert surv["extraction_pipeline"]["verified_by"] == "primary-source"
    assert judge_calls == []


def test_extra_model_failure_skipped_primary_failure_raises(monkeypatch):
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a,ex-b")
    _install_extractors(
        monkeypatch,
        {
            "prime": [P_ONLY],
            "ex-a": ExtractorUnavailable("ollama hiccup (test)"),
            "ex-b": [B_ONLY],
        },
    )
    _install_judge(monkeypatch, supported={B_ONLY["title"]})

    report = ensemble.run_ensemble(TEXT, "proj", "test://wpb2/isolation")
    assert report["extractor_errors"] == ["ex-a"], "one extra failing skips only that model"
    assert set(report["candidates"]) == {"prime", "ex-b"}
    assert {s["item"]["title"] for s in report["survivors"]} == {
        "primary singleton",
        "gemma singleton",
    }

    _install_extractors(monkeypatch, {"prime": ExtractorUnavailable("primary down (test)")})
    with pytest.raises(ExtractorUnavailable):
        ensemble.run_ensemble(TEXT, "proj", "test://wpb2/isolation2")


# --- representative choice ----------------------------------------------------


def test_representative_grounded_beats_repaired(monkeypatch):
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    prime_it = _item("socket gotcha", "prime saw it via a drifted quote", NEAR_MISS, "gotcha")
    extra_it = _item("socket gotcha exact", "extra quoted it verbatim", TMP_SPAN, "gotcha")
    _install_vectors(monkeypatch, {_ctext(prime_it): [1.0, 0.0], _ctext(extra_it): [1.0, 0.0]})
    _install_extractors(monkeypatch, {"prime": [prime_it], "ex-a": [extra_it]})
    _install_judge(monkeypatch)

    report = ensemble.run_ensemble(TEXT, "proj", "test://wpb2/rep1")
    (surv,) = report["survivors"]
    assert surv["model"] == "ex-a", "the grounded member beats the primary's repaired one"
    assert surv["item"]["title"] == "socket gotcha exact"
    assert surv["repair"] is None
    assert surv["extraction_pipeline"]["quote_state"] == "grounded"
    assert surv["extraction_pipeline"]["verified_by"] == "corroboration"
    assert surv["extraction_pipeline"]["found_by"] == ["prime", "ex-a"]


def test_representative_primary_tie_break(monkeypatch):
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    prime_it = _item("socket gotcha", "prime quoted it verbatim", TMP_SPAN, "gotcha")
    extra_it = _item("socket gotcha too", "extra also quoted it verbatim", TMP_SPAN, "gotcha")
    _install_vectors(monkeypatch, {_ctext(prime_it): [1.0, 0.0], _ctext(extra_it): [1.0, 0.0]})
    _install_extractors(monkeypatch, {"prime": [prime_it], "ex-a": [extra_it]})
    _install_judge(monkeypatch)

    report = ensemble.run_ensemble(TEXT, "proj", "test://wpb2/rep2")
    (surv,) = report["survivors"]
    assert surv["model"] == "prime", "equal quote state → the primary's member wins"
    assert surv["extraction_pipeline"]["verified_by"] == "primary-source"


# --- shadow mode --------------------------------------------------------------


def test_shadow_mode_ingests_exactly_v1_and_logs(monkeypatch, caplog):
    _install_extractors(monkeypatch, {"prime": [SHARED, P_ONLY], "ex-a": [A_ONLY]})
    _install_judge(monkeypatch, supported={A_ONLY["title"]})

    # Baseline: pure v1 (ensemble off, from the fixture default).
    v1_result = extract_and_ingest(TEXT, "proj", "test://wpb2/v1only")
    assert v1_result == (2, 0)
    c1 = _counts()

    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    assert ensemble.mode() == "shadow"
    caplog.set_level(logging.INFO, logger="kbworker.ensemble")
    shadow_result = extract_and_ingest(TEXT, "proj", "test://wpb2/shadow")

    assert shadow_result == v1_result, "shadow returns v1's (saved, rejected) untouched"
    assert _counts() == tuple(2 * n for n in c1), (
        "the shadow run wrote exactly what a v1-only run writes — nothing more"
    )
    for it in _session_items().values():
        assert "extraction_pipeline" not in it.meta, "no ensemble meta leaks in shadow mode"

    msgs = [r.getMessage() for r in caplog.records if "ensemble shadow" in r.getMessage()]
    assert len(msgs) == 1, "exactly one shadow report line"
    assert "would_save=3" in msgs[0] and "v1_saved=2" in msgs[0]
    assert "prime=2" in msgs[0] and "ex-a=1" in msgs[0]
    assert ensemble.shadow_survivor_total() == 3


def test_shadow_failure_never_affects_v1(monkeypatch):
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    _install_extractors(monkeypatch, {"prime": [SHARED]})

    def _boom(*a, **k):
        raise RuntimeError("shadow exploded (test)")

    monkeypatch.setattr(ensemble, "run_ensemble", _boom)
    saved, rejected = extract_and_ingest(TEXT, "proj", "test://wpb2/shadowboom")
    assert (saved, rejected) == (1, 0), "v1's result stands when the shadow run crashes"
    assert set(_session_items()) == {"pool size decision"}


# --- live mode ----------------------------------------------------------------


def test_live_mode_ingests_survivors_with_pipeline_meta(monkeypatch):
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a,ex-b")
    monkeypatch.setattr(settings, "kb_ensemble_shadow", False)
    assert ensemble.mode() == "live"
    near_item = _item("repaired singleton", "found via a drifted quote", NEAR_MISS, "gotcha")
    fabricated = _item("fabricated extra", "invented claim", FABRICATED_QUOTE)
    _install_extractors(
        monkeypatch,
        {
            "prime": [SHARED, P_ONLY],
            "ex-a": [dict(SHARED), A_ONLY],
            "ex-b": [near_item, fabricated],
        },
    )
    judge_calls = _install_judge(
        monkeypatch,
        supported={"granite singleton", "repaired singleton", "fabricated extra"},
    )

    uri = "test://wpb2/live"
    saved, rejected = extract_and_ingest(TEXT, "proj", uri, branch="main")
    assert saved == 4
    assert rejected == 1, "the unanchored ensemble item counts as rejected"

    items = _session_items()
    assert set(items) == {
        "pool size decision",
        "primary singleton",
        "granite singleton",
        "repaired singleton",
    }
    # Quote gate precedes policy even in live mode: a fabricated quote dies
    # before verification, no matter how supportive the judge would be.
    assert all(c["title"] != "fabricated extra" for c in judge_calls)

    shared = items["pool size decision"]
    assert shared.meta["extraction_pipeline"] == {
        "found_by": ["prime", "ex-a"],
        "corroboration": 2,
        "quote_state": "grounded",
        "verified_by": "primary-source",
    }
    assert shared.meta["extractor"] == "prime"
    assert shared.meta["branch"] == "main"
    assert shared.trust == "unverified"

    granite = items["granite singleton"]
    assert granite.meta["extraction_pipeline"]["verified_by"] == "primary-judge"
    assert granite.meta["extractor"] == "ex-a"

    repaired = items["repaired singleton"]
    assert repaired.meta["extraction_pipeline"]["quote_state"] == "repaired"
    assert repaired.meta["quote"] in TEXT, "stored quote is the LOCATED verbatim span (SEC-3)"
    assert repaired.meta["quote_repair"]["ratio"] >= 0.95
    assert repaired.meta["quote_repair"]["model_quote"] == NEAR_MISS

    with db.connect() as conn:
        review = conn.execute("SELECT detail FROM review_queue").fetchall()
    assert len(review) == 4, "every survivor got its review row"
    assert all(d["rejected_count"] == 1 for (d,) in review)

    # Idempotence (hard rule 3): the same window re-processed is a no-op.
    before = _counts()
    again = extract_and_ingest(TEXT, "proj", uri, branch="main")
    assert again == (0, 1), "second run saves nothing (dedupe), re-counts the same reject"
    assert _counts() == before, "no duplicate items/sources/review rows"


def test_unanchored_only_live_run_saves_nothing(monkeypatch):
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    monkeypatch.setattr(settings, "kb_ensemble_shadow", False)
    fabricated = _item("fabricated extra", "invented claim", FABRICATED_QUOTE)
    _install_extractors(monkeypatch, {"prime": [], "ex-a": [fabricated]})
    judge_calls = _install_judge(monkeypatch, supported={"fabricated extra"})

    saved, rejected = extract_and_ingest(TEXT, "proj", "test://wpb2/unanchored")
    assert (saved, rejected) == (0, 1)
    assert judge_calls == [], "unanchored items never reach the judge (quote gate first)"
    assert _counts() == (0, 0, 0), "nothing ingested"


# --- worker report plumbing ---------------------------------------------------


def test_mode_reflects_settings(monkeypatch):
    assert ensemble.mode() == "off"
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    assert ensemble.mode() == "shadow"
    monkeypatch.setattr(settings, "kb_ensemble_shadow", False)
    assert ensemble.mode() == "live"


def test_consolidate_reports_ensemble_counts(monkeypatch):
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    result = consolidate.main()
    assert result["ensemble_mode"] == "shadow"
    assert result["ensemble_shadow_survivors"] == 0, "empty queue → no shadow runs"

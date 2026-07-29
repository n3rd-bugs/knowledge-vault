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
from itertools import zip_longest

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
        calls.append({"title": title, "judge_model": judge_model, "keep_alive": keep_alive})
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
    # Invariant 8 (amendment, corrected 2026-07-29): the primary keeps the
    # settings default (None sentinel = resident keep_alive); EXTRAS ride the
    # drain PHASE keep-alive, never 0. keep_alive=0 unloaded each extra the
    # instant its call returned, so on a one-slot card the next run paid a full
    # cold load for it — while the phase value costs nothing extra, because the
    # next model requested evicts it anyway. Same knob prefetch_extractions uses,
    # so the prefetched and non-prefetched paths cannot disagree.
    ka = settings.kb_drain_phase_keep_alive
    assert {c["model"]: c["keep_alive"] for c in calls} == {"prime": None, "ex-a": ka, "ex-b": ka}
    assert all(c["keep_alive"] != 0 for c in calls if c["model"] != "prime")


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


# --------------------------------------------------------------------------
# Cohort prefetch. With OLLAMA_MAX_LOADED_MODELS=1 every model switch is a full
# load, so the drain runs each extractor ONCE across a cohort instead of once
# per job. Only extraction is batched; everything downstream stays per job.
# --------------------------------------------------------------------------


def test_prefetch_loads_each_model_once_across_the_cohort(monkeypatch):
    """The ORDER is the whole point: model-major, not job-major."""
    from kbcore.config import settings
    from kbworker import ensemble

    monkeypatch.setattr(settings, "kb_extract_model", "primary")
    monkeypatch.setattr(settings, "kb_extract_ensemble", "primary,extraA,extraB")

    calls: list[str] = []

    def fake_extract(text, model=None, keep_alive=None):
        calls.append(model)
        return [{"title": f"t-{model}", "body": text}]

    monkeypatch.setattr(ensemble.llm, "extract_items", fake_extract)
    out = ensemble.prefetch_extractions(["one", "two", "three"])

    assert len(out) == 3
    assert set(out[0]) == {"primary", "extraA", "extraB"}
    # Model-major: all three texts for one model before moving on. Job-major
    # would read primary,extraA,extraB,primary,... and reload every model per row.
    assert calls == ["primary"] * 3 + ["extraA"] * 3 + ["extraB"] * 3


def test_prefetch_uses_an_INT_keep_alive(monkeypatch):
    """llm.extract_items is typed keep_alive: int | None — only the low-level
    _chat accepts Ollama's "10m" string. A string here would be a TypeError at
    the worst possible moment, mid-drain."""
    from kbcore.config import settings
    from kbworker import ensemble

    monkeypatch.setattr(settings, "kb_extract_model", "primary")
    monkeypatch.setattr(settings, "kb_extract_ensemble", "primary")
    seen: list = []
    monkeypatch.setattr(
        ensemble.llm,
        "extract_items",
        lambda text, model=None, keep_alive=None: (seen.append(keep_alive), [])[1],
    )
    ensemble.prefetch_extractions(["x"])
    assert isinstance(seen[0], int) and seen[0] > 0


def test_a_failing_extractor_does_not_abort_the_cohort(monkeypatch):
    """One model failing must cost that model, not the whole batch — the same
    rule the per-row path uses for a failing extra."""
    from kbcore.config import settings
    from kbcore.errors import ExtractorUnavailable
    from kbworker import ensemble

    monkeypatch.setattr(settings, "kb_extract_model", "primary")
    monkeypatch.setattr(settings, "kb_extract_ensemble", "primary,flaky")

    def fake_extract(text, model=None, keep_alive=None):
        if model == "flaky":
            raise ExtractorUnavailable("down")
        return [{"title": "ok", "body": text}]

    monkeypatch.setattr(ensemble.llm, "extract_items", fake_extract)
    out = ensemble.prefetch_extractions(["a", "b"])
    assert all("primary" in o for o in out), "a failing extra took the primary with it"
    assert all("flaky" not in o for o in out)


def test_run_ensemble_consumes_the_prefetch_instead_of_re_extracting(monkeypatch):
    """The saving only exists if run_ensemble does NOT extract again."""
    from kbcore.config import settings
    from kbworker import ensemble

    monkeypatch.setattr(settings, "kb_extract_model", "primary")
    monkeypatch.setattr(settings, "kb_extract_ensemble", "primary")
    called: list = []
    monkeypatch.setattr(
        ensemble.llm,
        "extract_items",
        lambda *a, **k: (called.append(1), [])[1],
    )
    text = "the DRV8706 needs SPI mode 1"
    pre = {"primary": [{"title": "SPI mode", "body": "the DRV8706 needs SPI mode 1"}]}
    ensemble.run_ensemble(text, "proj", "uri", pre_extracted=pre)
    assert not called, "run_ensemble re-extracted despite being handed a prefetch"


# --------------------------------------------------------------------------
# Model-major JUDGING (2026-07-29). The card holds ONE model
# (OLLAMA_MAX_LOADED_MODELS=1), so every alternation between the secondary and
# the primary judge was a full cold load — and keep_alive=0 on the secondary
# judge unloaded it after each call, guaranteeing the next reload. A run now
# collects every judgement it needs and executes them GROUPED BY MODEL.
# --------------------------------------------------------------------------


def _runs(seq: list[str]) -> list[str]:
    """Collapse a sequence to its contiguous runs: [a,a,b,b] -> [a,b]."""
    out: list[str] = []
    for x in seq:
        if not out or out[-1] != x:
            out.append(x)
    return out


def _force_interleaved_clusters(monkeypatch, primary: str = "prime") -> None:
    """Hand run_ensemble singleton clusters that ALTERNATE primary / non-primary.

    Entries are built primary-first, so the natural cluster order already groups
    the two judge kinds and would hide a per-cluster swap. Alternating them is
    the adversarial order: a judge loop that walks clusters swaps models on
    every single cluster.
    """

    real_cluster = ensemble._cluster

    def fake_cluster(entries, threshold):
        clusters = real_cluster(entries, threshold)  # real geometry, shuffled order
        prime = [c for c in clusters if c[0]["model"] == primary]
        other = [c for c in clusters if c[0]["model"] != primary]
        out = []
        for a, b in zip_longest(prime, other):
            if a is not None:
                out.append(a)
            if b is not None:
                out.append(b)
        return out

    monkeypatch.setattr(ensemble, "_cluster", fake_cluster)


def test_judging_is_model_major_no_model_swaps(monkeypatch, caplog):
    """THE pinned requirement: one contiguous run per judge model, per batch."""
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    monkeypatch.setattr(settings, "kb_ensemble_secondary_judge", "second")
    prime_items = [
        _item("prime one", "primary singleton one", POOL_SPAN),
        _item("prime two", "primary singleton two", TMP_SPAN),
        _item("prime three", "primary singleton three", RERANK_SPAN),
    ]
    extra_items = [
        _item("extra one", "extra singleton one", POOL_SPAN),
        _item("extra two", "extra singleton two", TMP_SPAN),
        _item("extra three", "extra singleton three", RERANK_SPAN),
    ]
    _install_extractors(monkeypatch, {"prime": prime_items, "ex-a": extra_items})
    judge_calls = _install_judge(monkeypatch, supported={it["title"] for it in extra_items})
    _force_interleaved_clusters(monkeypatch)
    caplog.set_level(logging.INFO, logger="kbworker.ensemble")

    report = ensemble.run_ensemble(TEXT, "proj", "test://wpb2/modelmajor")

    seq = [c["judge_model"] for c in judge_calls]
    assert len(seq) == 6, "three primary singletons + three non-primary singletons"
    assert _runs(seq) == ["second", "prime"], (
        "each judge model must appear in exactly ONE contiguous run; a model "
        "appearing twice in the run list is a cold reload on a one-slot card"
    )
    assert report["judged"] == 6
    assert len(report["survivors"]) == 6, "all six survive — the batching changes no verdict"

    groups = [r.getMessage() for r in caplog.records if "ensemble judge group" in r.getMessage()]
    assert len(groups) == 2, "one observable log line per judge group"
    assert "model=second" in groups[0] and "judgements=3" in groups[0]
    assert "model=prime" in groups[1] and "judgements=3" in groups[1]


def test_secondary_judge_keep_alive_is_never_zero(monkeypatch):
    """keep_alive=0 on the secondary judge WAS the reload-per-call bug."""
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    monkeypatch.setattr(settings, "kb_ensemble_secondary_judge", "second")
    monkeypatch.setattr(settings, "kb_llm_keep_alive", -1)
    _install_extractors(monkeypatch, {"prime": [P_ONLY], "ex-a": [A_ONLY]})
    judge_calls = _install_judge(monkeypatch, supported={A_ONLY["title"]})

    ensemble.run_ensemble(TEXT, "proj", "test://wpb2/keepalive")

    by_model = {c["judge_model"]: c["keep_alive"] for c in judge_calls}
    assert set(by_model) == {"second", "prime"}
    assert all(c["keep_alive"] != 0 for c in judge_calls), "0 unloads the judge after every call"
    ka = by_model["second"]
    assert ka == settings.kb_drain_phase_keep_alive
    assert isinstance(ka, int) and ka > 0, "the phase keep-alive must be a positive int"
    # A NEGATIVE setting is CLAMPED, never propagated. An unbounded pin holds the
    # only slot under OLLAMA_MAX_LOADED_MODELS=1, so the next model to run — the
    # other judge, the next cohort's extractor, the embedder — waits forever and
    # dies on the client timeout. That pin stalled the entire drain on 2026-07-29.
    assert by_model["prime"] == settings.kb_drain_phase_keep_alive
    assert all(c["keep_alive"] > 0 for c in judge_calls), "an unbounded pin starves the slot"


def test_primary_judge_group_survives_a_zero_settings_keep_alive(monkeypatch):
    """With KB_LLM_KEEP_ALIVE=0 (owner reclaims VRAM) the primary judge would
    unload after each call; the GROUP keep-alive is the floor that stops it."""
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    monkeypatch.setattr(settings, "kb_llm_keep_alive", 0)
    _install_extractors(monkeypatch, {"prime": [], "ex-a": [A_ONLY, B_ONLY]})
    judge_calls = _install_judge(monkeypatch, supported={A_ONLY["title"], B_ONLY["title"]})

    ensemble.run_ensemble(TEXT, "proj", "test://wpb2/keepalive-zero")

    assert len(judge_calls) == 2
    assert all(c["keep_alive"] == settings.kb_drain_phase_keep_alive for c in judge_calls)

    # ...and the phase keep-alive is a FLOOR, never a cap: a longer configured
    # residency for the primary is left alone (batching may only lengthen it).
    judge_calls.clear()
    monkeypatch.setattr(settings, "kb_llm_keep_alive", settings.kb_drain_phase_keep_alive * 3)
    ensemble.run_ensemble(TEXT, "proj", "test://wpb2/keepalive-long")
    assert all(c["keep_alive"] == settings.kb_llm_keep_alive for c in judge_calls)


def test_secondary_judge_exception_still_ingests_as_primary_source(monkeypatch):
    """Demote-to-review semantics: a secondary-judge ERROR never drops a
    primary-sourced item, it only costs a judge_errors tick."""
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    monkeypatch.setattr(settings, "kb_ensemble_secondary_judge", "second")
    _install_extractors(monkeypatch, {"prime": [P_ONLY], "ex-a": []})
    _install_judge(monkeypatch, errors={P_ONLY["title"]})

    report = ensemble.run_ensemble(TEXT, "proj", "test://wpb2/secondary-error")

    (surv,) = report["survivors"]
    assert surv["extraction_pipeline"]["verified_by"] == "primary-source"
    assert (report["judged"], report["judge_errors"], report["rejected_reps"]) == (1, 1, 0)


def test_batched_judging_preserves_every_counter_and_label(monkeypatch):
    """The full policy matrix in ONE batch — both judge kinds x
    supported/unsupported/exception — with the counters pinned."""
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    monkeypatch.setattr(settings, "kb_ensemble_secondary_judge", "second")
    p_pass = _item("p pass", "primary singleton the second judge backs", POOL_SPAN)
    p_fail = _item("p fail", "primary singleton the second judge rejects", TMP_SPAN)
    p_err = _item("p err", "primary singleton whose judge dies", RERANK_SPAN)
    a_pass = _item("a pass", "extra singleton the primary judge backs", POOL_SPAN)
    a_fail = _item("a fail", "extra singleton the primary judge rejects", TMP_SPAN)
    a_err = _item("a err", "extra singleton whose judge dies", RERANK_SPAN)
    _install_extractors(
        monkeypatch,
        {
            "prime": [SHARED, p_pass, p_fail, p_err],
            "ex-a": [dict(SHARED), a_pass, a_fail, a_err],
        },
    )
    judge_calls = _install_judge(
        monkeypatch, supported={"p pass", "a pass"}, errors={"p err", "a err"}
    )
    _force_interleaved_clusters(monkeypatch)

    report = ensemble.run_ensemble(TEXT, "proj", "test://wpb2/matrix")

    by_title = {
        s["item"]["title"]: s["extraction_pipeline"]["verified_by"] for s in report["survivors"]
    }
    assert by_title == {
        "pool size decision": "primary-source",  # corroborated: never judged
        "p pass": "secondary-judge",
        "p fail": "secondary-judge-failed",  # demote-to-review, still ingests
        "p err": "primary-source",  # judge error, still ingests
        "a pass": "primary-judge",
    }, "a fail / a err are fail-closed: rejected, not ingested"
    assert report["corroborated"] == 1
    assert report["judged"] == 6, "both branches count a judgement each"
    assert report["judge_errors"] == 2, "one per branch"
    assert report["rejected_reps"] == 2, "only the fail-closed branch rejects"
    assert _runs([c["judge_model"] for c in judge_calls]) == ["second", "prime"]
    assert [c["title"] for c in judge_calls if c["judge_model"] == "second"] == [
        "p pass",
        "p fail",
        "p err",
    ], "cluster order is preserved WITHIN a judge group"


def test_corroborated_reps_never_reach_a_judge_group(monkeypatch):
    """A batch with nothing to judge issues no judge call and logs no group."""
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    monkeypatch.setattr(settings, "kb_ensemble_secondary_judge", "second")
    _install_extractors(monkeypatch, {"prime": [SHARED], "ex-a": [dict(SHARED)]})
    judge_calls = _install_judge(monkeypatch)

    report = ensemble.run_ensemble(TEXT, "proj", "test://wpb2/nojudge")

    assert judge_calls == []
    assert (report["corroborated"], report["judged"], report["judge_errors"]) == (1, 0, 0)


# --------------------------------------------------------------------------
# COHORT-WIDE judging (2026-07-29). Measured on the live drain: 50 ollama model
# loads for 37 chat calls, alternating qwen3 -> granite -> qwen3 -> granite,
# ~90% of wall clock spent loading (granite 4s, gemma 17s, qwen3 25s). Cause:
# OLLAMA_MAX_LOADED_MODELS=1 plus judging that was model-major only WITHIN one
# job, so a cohort of 8 jobs alternated ~16 times. Judging is now hoisted to
# COHORT scope: plan every job (no chat calls), judge the whole cohort
# model-major, then finish + ingest job by job.
# --------------------------------------------------------------------------


def _cohort_jobs(n: int) -> list[dict]:
    """n jobs, each with ONE primary singleton (→ secondary judge) and ONE
    non-primary singleton (→ primary judge, fail-closed).

    Both judge kinds in every job is the adversarial shape: per-job judging must
    alternate models once per job, so a cohort of n jobs collapses to 2n runs
    unless the batching is genuinely cohort-wide.
    """
    jobs = []
    for i in range(n):
        jobs.append(
            {
                "i": i,
                "text": f"{TEXT}\n\nJob {i} marker.",
                "uri": f"test://wpb2/cohort/{i}",
                "prime": _item(f"p{i}", f"primary singleton {i}", TMP_SPAN),
                "extra": _item(f"a{i}", f"extra singleton {i}", RERANK_SPAN),
            }
        )
    return jobs


def _install_cohort_extractors(monkeypatch, jobs: list[dict]) -> list[dict]:
    """Per-JOB extractor output, keyed by the job marker in the text.

    Keyed by marker rather than by identity because the queue scrubs content
    before storing it, so the drain hands back an equal-but-not-identical string.
    """
    by_index = {j["i"]: j for j in jobs}
    calls: list[dict] = []

    def fake(text, *, model=None, keep_alive=None):
        job = by_index[int(text.rsplit("Job ", 1)[1].split(" ")[0])]
        m = model or settings.kb_extract_model
        calls.append({"model": m, "job": job["i"], "keep_alive": keep_alive})
        if m == "prime":
            return [dict(job["prime"])]
        if m == "ex-a":
            return [dict(job["extra"])]
        return []

    monkeypatch.setattr(llm, "extract_items", fake)
    return calls


def _all_supported(jobs: list[dict]) -> set[str]:
    """Every item of every job judged SUPPORTED — isolates ordering/attribution
    from verdict policy, which the single-job matrix tests already pin."""
    return {j["prime"]["title"] for j in jobs} | {j["extra"]["title"] for j in jobs}


def _install_dying_judge(
    monkeypatch, *, supported=frozenset(), dies_at=None, content_errors=()
) -> list[dict]:
    """Judge fake that can go DARK (ExtractorUnavailable) from a given title on.

    ``dies_at`` is a (judge_model, title) pair: that call and every later call to
    that model raise ExtractorUnavailable — connectivity, not content. Titles in
    ``content_errors`` raise ExtractionFailed instead (content-deterministic,
    strictly per item).
    """
    calls: list[dict] = []
    dead: set[str] = set()

    def fake(title, body, transcript, *, judge_model=None, keep_alive=None):
        calls.append({"title": title, "judge_model": judge_model, "keep_alive": keep_alive})
        if judge_model in dead:
            raise ExtractorUnavailable(f"{judge_model} is gone (test)")
        if dies_at is not None and (judge_model, title) == dies_at:
            dead.add(judge_model)
            raise ExtractorUnavailable(f"{judge_model} died on {title} (test)")
        if title in content_errors:
            raise ExtractionFailed("judge blew up (test)")
        return {"supported": title in supported, "reason": "test verdict"}

    monkeypatch.setattr(llm, "verify_item", fake)
    return calls


def _cohort_env(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    monkeypatch.setattr(settings, "kb_ensemble_secondary_judge", "second")


def test_cohort_judging_is_one_contiguous_run_per_model_across_ALL_jobs(monkeypatch, caplog):
    """THE requirement: a batch never swaps back to a model it already ran.

    Each judge model appears in exactly ONE contiguous run across the ENTIRE
    cohort, so loads per cohort are bounded by the number of distinct judge
    models — not multiplied by the number of jobs.
    """
    _cohort_env(monkeypatch)
    jobs = _cohort_jobs(4)
    _install_cohort_extractors(monkeypatch, jobs)
    judge_calls = _install_judge(monkeypatch, supported=_all_supported(jobs))
    caplog.set_level(logging.INFO, logger="kbworker.ensemble")

    plans = [ensemble.plan_ensemble(j["text"], "proj", j["uri"]) for j in jobs]
    assert judge_calls == [], "planning must issue NO judge call — that is the whole point"

    ensemble.judge_cohort(plans)

    seq = [c["judge_model"] for c in judge_calls]
    assert len(seq) == 8, "four primary singletons + four non-primary singletons"
    assert _runs(seq) == ["second", "prime"], (
        "each judge model must appear in exactly ONE contiguous run across the "
        "whole cohort; a second run is a cold reload on a one-slot card"
    )
    assert len(_runs(seq)) == len({*seq}), "runs == distinct judge models, independent of job count"
    # Plan-major within a group, cluster order within a plan: deterministic.
    assert [c["title"] for c in judge_calls if c["judge_model"] == "second"] == [
        f"p{i}" for i in range(4)
    ]
    assert [c["title"] for c in judge_calls if c["judge_model"] == "prime"] == [
        f"a{i}" for i in range(4)
    ]

    reports = [ensemble.finish_ensemble(p) for p in plans]
    for i, report in enumerate(reports):
        labels = {
            s["item"]["title"]: s["extraction_pipeline"]["verified_by"] for s in report["survivors"]
        }
        assert labels == {f"p{i}": "secondary-judge", f"a{i}": "primary-judge"}
        assert (report["judged"], report["judge_errors"], report["rejected_reps"]) == (2, 0, 0)
        assert report["uri"] == jobs[i]["uri"]

    groups = [r.getMessage() for r in caplog.records if "ensemble judge group" in r.getMessage()]
    assert len(groups) == 2, "one observable log line per judge group, per COHORT"
    assert "model=second" in groups[0] and "judgements=4" in groups[0] and "plans=4" in groups[0]
    assert "model=prime" in groups[1] and "judgements=4" in groups[1] and "plans=4" in groups[1]


def test_cohort_judging_beats_per_job_judging_and_agrees_with_it_exactly(monkeypatch):
    """Same verdicts, same counters, same labels — 2 model runs instead of 2N."""
    _cohort_env(monkeypatch)
    jobs = _cohort_jobs(3)
    _install_cohort_extractors(monkeypatch, jobs)
    judge_calls = _install_judge(monkeypatch, supported=_all_supported(jobs))

    plans = [ensemble.plan_ensemble(j["text"], "proj", j["uri"]) for j in jobs]
    ensemble.judge_cohort(plans)
    cohort_reports = [ensemble.finish_ensemble(p) for p in plans]
    cohort_runs = _runs([c["judge_model"] for c in judge_calls])

    judge_calls.clear()
    single_reports = [ensemble.run_ensemble(j["text"], "proj", j["uri"]) for j in jobs]
    single_runs = _runs([c["judge_model"] for c in judge_calls])

    assert cohort_reports == single_reports, (
        "the cohort path must be byte-identical to the single-job path it replaces"
    )
    assert cohort_runs == ["second", "prime"]
    assert single_runs == ["second", "prime"] * 3, "per job: one alternation each (the old cost)"
    assert len(cohort_runs) == 2 and len(single_runs) == 6


def test_no_cohort_judge_call_uses_a_nonpositive_keep_alive(monkeypatch):
    """keep_alive=0 unloads the judge after every call; a negative value pins the
    only slot forever. Both stalled this pipeline; neither may come back."""
    _cohort_env(monkeypatch)
    monkeypatch.setattr(settings, "kb_llm_keep_alive", -1)  # "resident forever" — must be CLAMPED
    jobs = _cohort_jobs(3)
    _install_cohort_extractors(monkeypatch, jobs)
    judge_calls = _install_judge(monkeypatch, supported={f"a{i}" for i in range(3)})

    plans = [ensemble.plan_ensemble(j["text"], "proj", j["uri"]) for j in jobs]
    ensemble.judge_cohort(plans)

    assert len(judge_calls) == 6
    assert all(isinstance(c["keep_alive"], int) for c in judge_calls)
    assert all(c["keep_alive"] > 0 for c in judge_calls), "0 unloads, negative pins the slot"
    assert all(c["keep_alive"] == settings.kb_drain_phase_keep_alive for c in judge_calls)


def test_a_content_error_stays_per_item_across_the_whole_cohort(monkeypatch):
    """ExtractionFailed is about THAT item's text: no neighbour, in any job, moves."""
    _cohort_env(monkeypatch)
    jobs = _cohort_jobs(3)
    _install_cohort_extractors(monkeypatch, jobs)
    _install_dying_judge(
        monkeypatch,
        supported=_all_supported(jobs),
        content_errors=("a1", "p2"),
    )

    plans = [ensemble.plan_ensemble(j["text"], "proj", j["uri"]) for j in jobs]
    ensemble.judge_cohort(plans)
    reports = [ensemble.finish_ensemble(p) for p in plans]

    labels = [
        {s["item"]["title"]: s["extraction_pipeline"]["verified_by"] for s in r["survivors"]}
        for r in reports
    ]
    assert labels[0] == {"p0": "secondary-judge", "a0": "primary-judge"}, "untouched neighbour"
    assert labels[1] == {"p1": "secondary-judge"}, "a1 is fail-closed: rejected, not ingested"
    assert labels[2] == {"p2": "primary-source", "a2": "primary-judge"}, (
        "a secondary-judge error still ingests the primary-sourced item"
    )
    # Attribution: job A's judge error must never surface in job B's counters.
    assert [r["judge_errors"] for r in reports] == [0, 1, 1]
    assert [r["rejected_reps"] for r in reports] == [0, 1, 0]
    assert [r["judged"] for r in reports] == [2, 2, 2]


def test_a_primary_judge_dying_mid_cohort_defers_only_the_jobs_it_never_reached(monkeypatch):
    """Fail-closed means NEVER INGESTED WITHOUT A VERDICT. Verdicts stamped before
    the model died stand; the jobs it never reached DEFER whole (the drain's
    ExtractorUnavailable handler leaves attempts untouched) rather than silently
    dropping every non-primary singleton in the cohort."""
    _cohort_env(monkeypatch)
    jobs = _cohort_jobs(4)
    _install_cohort_extractors(monkeypatch, jobs)
    judge_calls = _install_dying_judge(
        monkeypatch, supported={f"a{i}" for i in range(4)}, dies_at=("prime", "a2")
    )

    plans = [ensemble.plan_ensemble(j["text"], "proj", j["uri"]) for j in jobs]
    ensemble.judge_cohort(plans)

    # The secondary group ran to completion BEFORE the primary group started, so
    # a dead primary judge costs it nothing.
    assert [c["title"] for c in judge_calls if c["judge_model"] == "second"] == [
        f"p{i}" for i in range(4)
    ]
    # ...and the dead group is ABANDONED at the first connectivity failure: a3 is
    # never attempted, because it would fail identically.
    assert [c["title"] for c in judge_calls if c["judge_model"] == "prime"] == ["a0", "a1", "a2"]

    for i in (0, 1):
        report = ensemble.finish_ensemble(plans[i])
        assert {s["item"]["title"] for s in report["survivors"]} == {f"p{i}", f"a{i}"}
        assert report["judge_errors"] == 0
    for i in (2, 3):
        with pytest.raises(ExtractorUnavailable) as excinfo:
            ensemble.finish_ensemble(plans[i])
        assert "prime" in str(excinfo.value), "the deferred row's error names the judge that died"
        assert plans[i].judge_errors == 1


def test_a_dead_secondary_judge_never_drops_a_primary_item_or_costs_the_primary_group(monkeypatch):
    """Demote-to-review holds cohort-wide: every job still PROCEEDS."""
    _cohort_env(monkeypatch)
    jobs = _cohort_jobs(3)
    _install_cohort_extractors(monkeypatch, jobs)
    judge_calls = _install_dying_judge(
        monkeypatch, supported={f"a{i}" for i in range(3)}, dies_at=("second", "p0")
    )

    plans = [ensemble.plan_ensemble(j["text"], "proj", j["uri"]) for j in jobs]
    ensemble.judge_cohort(plans)
    reports = [ensemble.finish_ensemble(p) for p in plans]  # no job defers

    assert [c["title"] for c in judge_calls] == ["p0", "a0", "a1", "a2"], (
        "the secondary group is abandoned at the first failure; the PRIMARY group "
        "still runs in full — one dead judge must not cost the other its group"
    )
    for i, report in enumerate(reports):
        labels = {
            s["item"]["title"]: s["extraction_pipeline"]["verified_by"] for s in report["survivors"]
        }
        assert labels == {f"p{i}": "primary-source", f"a{i}": "primary-judge"}
        assert (report["judge_errors"], report["rejected_reps"]) == (1, 0)


def test_a_cohort_prefetch_that_lost_the_primary_defers_instead_of_degrading(monkeypatch):
    """A primary-less prefetch used to produce a silent primary-less report: no
    extractor errors, no corroboration, every extra item a fail-closed singleton
    judged by the SAME dead primary. It now fails like a primary extractor
    failure, so the drain defers the group."""
    monkeypatch.setattr(settings, "kb_extract_ensemble", "ex-a")
    _install_extractors(monkeypatch, {"prime": [P_ONLY], "ex-a": [A_ONLY]})
    _install_judge(monkeypatch)

    with pytest.raises(ExtractorUnavailable):
        ensemble.plan_ensemble(TEXT, "proj", "test://wpb2/lostprimary", pre_extracted={"ex-a": []})
    # A prefetch that HAS the primary is used as-is (no re-extraction).
    plan = ensemble.plan_ensemble(
        TEXT, "proj", "test://wpb2/goodprefetch", pre_extracted={"prime": [dict(P_ONLY)]}
    )
    assert plan.partial["candidates"] == {"prime": 1}


def test_judge_cohort_tolerates_an_empty_cohort(monkeypatch):
    assert ensemble.judge_cohort([]) == []


def test_the_drain_judges_a_whole_cohort_model_major_end_to_end(monkeypatch, caplog):
    """The two-level proof: ingestion sits ONE LINE after judging inside the
    per-job call, so an ensemble-only split would not have batched anything. The
    drain must plan the cohort, judge the cohort, then ingest job by job."""
    from kbcore import ingest_queue

    _cohort_env(monkeypatch)
    monkeypatch.setattr(settings, "kb_ensemble_shadow", False)
    monkeypatch.setattr(settings, "kb_drain_cohort", 8)
    assert ensemble.mode() == "live"

    jobs = _cohort_jobs(3)
    extract_calls = _install_cohort_extractors(monkeypatch, jobs)
    judge_calls = _install_judge(monkeypatch, supported=_all_supported(jobs))
    for job in jobs:
        ingest_queue.enqueue("cohort-live", job["text"], kind="memory", source_uri=job["uri"])
    caplog.set_level(logging.INFO, logger="kbworker.ensemble")

    drained, saved = consolidate._drain_ingest_queue()

    assert (drained, saved) == (3, 6), "every row drained; both survivors of each job ingested"
    # Extraction: model-major across the cohort (the phase that already worked).
    assert [c["model"] for c in extract_calls] == ["prime"] * 3 + ["ex-a"] * 3
    # Judging: model-major across the cohort — 2 runs for 3 jobs, not 6.
    seq = [c["judge_model"] for c in judge_calls]
    assert len(seq) == 6
    assert _runs(seq) == ["second", "prime"], "the drain still alternated per job"
    groups = [r.getMessage() for r in caplog.records if "ensemble judge group" in r.getMessage()]
    assert len(groups) == 2 and all("plans=3" in g for g in groups)

    items = _session_items()
    assert set(items) == {f"p{i}" for i in range(3)} | {f"a{i}" for i in range(3)}
    assert items["a0"].meta["extraction_pipeline"]["verified_by"] == "primary-judge"
    assert items["p0"].meta["extraction_pipeline"]["verified_by"] == "secondary-judge"
    # Observable from the run report, not from journald archaeology.
    st = consolidate._COHORT_STATS
    assert (st["cohort_plans"], st["cohort_plan_failures"]) == (3, 0)
    assert (st["cohort_judge_batches"], st["cohort_judge_failures"]) == (1, 0)


def test_a_broken_cohort_judge_pass_falls_back_per_job_instead_of_ingesting_unjudged(monkeypatch):
    """Plans are mutated in place, so half-judged plans must NEVER be finished:
    an unjudged non-primary singleton would ingest as if approved."""
    from kbcore import ingest_queue

    _cohort_env(monkeypatch)
    monkeypatch.setattr(settings, "kb_ensemble_shadow", False)
    monkeypatch.setattr(settings, "kb_drain_cohort", 8)
    jobs = _cohort_jobs(2)
    _install_cohort_extractors(monkeypatch, jobs)
    judge_calls = _install_judge(monkeypatch, supported=_all_supported(jobs))
    for job in jobs:
        ingest_queue.enqueue("cohort-boom", job["text"], kind="memory", source_uri=job["uri"])

    real = ensemble.judge_cohort

    def boom(plans):
        # Only the COHORT pass explodes; the per-job fallback (run_ensemble's
        # cohort of one) must still work, which is the point of the fallback.
        if len(plans) > 1:
            for p in plans:  # leave them corrupt, exactly like a partial pass
                p.pending.clear()
            raise RuntimeError("judge layer exploded (test)")
        return real(plans)

    monkeypatch.setattr(ensemble, "judge_cohort", boom)
    drained, saved = consolidate._drain_ingest_queue()

    assert consolidate._COHORT_STATS["cohort_judge_failures"] == 1
    assert (drained, saved) == (2, 4), "the per-job path re-judged from scratch and ingested"
    items = _session_items()
    assert items["a0"].meta["extraction_pipeline"]["verified_by"] == "primary-judge"
    assert all(c["judge_model"] in ("second", "prime") for c in judge_calls)

"""Retrieval MEASUREMENT plumbing: graph-expansion A/B, trust composition, and
the rerank-evidence flag.

These are INSTRUMENTS, not ranking changes. The owner decides from the numbers;
these tests prove the numbers are real:

* the nightly A/B reports BOTH sides' hit@5/MRR over the same golden set, with a
  positive delta on a corpus where the answer is reachable only over a relation
  edge and a ZERO delta where expansion adds nothing;
* the trust metric computes the exact share of unverified top-5 results on a
  mixed corpus, writes no access_log row, and leaves ranking untouched;
* the rerank-evidence flag OFF leaves the reranker's inputs BYTE-IDENTICAL to
  today (regression pin) and ON feeds the best-matching chunk text.

Real DB in kb_test; conftest mocks the embedder; no LLM and no rerank sidecar is
ever contacted (the rerank HTTP call is captured by a stub). LIVE A/B numbers
still require the owner to run `make eval` against the real corpus.

Determinism note: conftest's fake embedder has no distance threshold, so the
vector leg returns EVERY embedded chunk. Fixtures here therefore NULL the
embeddings they do not want the vector leg to hand back — that is what makes
"reachable only via a relation edge" and "this chunk is the best match" true
statements rather than coincidences.
"""

from __future__ import annotations

import pytest

from kbcore import (
    db,
    eval_criteria,
    eval_harness,
    graph,
    rerank,
    search_expand,
    search_trace,
    supersede,
)
from kbcore.config import Settings, settings
from kbcore.ingest import ingest
from kbcore.search import search
from kbworker import eval_cases, eval_sentinel, run_report

pytestmark = pytest.mark.usefixtures("kb")


@pytest.fixture(autouse=True)
def _hops_default_off(monkeypatch):
    """Pin the shipped production default (expansion OFF) for every test here:
    the A/B's whole point is that this is what production runs today."""
    monkeypatch.setattr(settings, "kb_graph_expand_hops", 0)


# --- helpers -----------------------------------------------------------------


def _blind(item_id: int) -> int:
    """NULL every chunk embedding of an item so the vector leg cannot see it."""
    with db.transaction() as conn:
        conn.execute("UPDATE chunks SET embedding = NULL WHERE item_id = %s", (item_id,))
    return item_id


def _blind_except(item_id: int, needle: str) -> None:
    """Leave only the chunks containing ``needle`` visible to the vector leg."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE chunks SET embedding = NULL WHERE item_id = %s AND text NOT LIKE %s",
            (item_id, f"%{needle}%"),
        )


def _chunk_texts(item_id: int) -> list[str]:
    with db.connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT text FROM chunks WHERE item_id = %s ORDER BY ord", (item_id,)
            ).fetchall()
        ]


def _run_sentinel(monkeypatch, golden: list[dict]) -> dict:
    """Run the sentinel over ``golden``, exactly as nightly consolidation does."""
    monkeypatch.setattr(eval_sentinel.eval_harness, "load_golden", lambda golden_path=None: golden)
    counts: dict = {}
    eval_sentinel.run_sentinel(counts)
    return counts


# --- 1. graph-expansion A/B --------------------------------------------------


def _seed_edge_reachable_corpus() -> dict[str, int]:
    """Two rank-sensitive relation-reachable cases + one expansion-blind control.

    Pair 1: the bug item carries the query's words; the FIX item does not and
    hangs off a 'fixes' edge (seed -> neighbour).
    Pair 2: the same shape with the edge pointing the OTHER way ('depends_on'
    from neighbour to seed) — expansion is direction-agnostic and the A/B must
    not silently depend on edge orientation.
    Control: a direct hit with no edges at all, so its zero delta shows the A/B
    is measuring the edges rather than the harness.
    """
    bug = ingest(
        "motor-ctl",
        "gotcha",
        "UART RX drops bytes at 115200 baud during PWM bursts",
        "The UART receiver loses bytes at 115200 baud whenever the PWM burst fires.",
        kind="chat",
        uri="ab:bug",
    )
    fix = ingest(
        "motor-ctl",
        "decision",
        "Interrupt-driven staging buffer for the serial port",
        "We added a staging buffer drained by the ISR so no octet is lost mid-burst.",
        kind="chat",
        uri="ab:fix",
    )
    doc = ingest(
        "aquamonix",
        "note",
        "DNP3 outstation poll interval register map",
        "The DNP3 outstation exposes its register map and a configurable poll interval.",
        kind="chat",
        uri="ab:doc",
    )
    layer = ingest(
        "aquamonix",
        "note",
        "Link-layer confirmation timing on the field bus",
        "Confirmation timing on the field bus is what the poller waits for between frames.",
        kind="chat",
        uri="ab:layer",
    )
    ctl = ingest(
        "docx-tools",
        "gotcha",
        "PageNumber CURRENT field in a Word footer",
        "Insert a PageNumber CURRENT field node into the Footer to number pages.",
        kind="chat",
        uri="ab:ctl",
    )
    graph.link(bug, "fixes", fix)  # seed -> neighbour
    graph.link(layer, "depends_on", doc)  # neighbour -> seed (reverse edge)
    for neighbour in (fix, layer):
        _blind(neighbour)  # only the relation edge can surface these
    return {"bug": bug, "fix": fix, "doc": doc, "layer": layer, "ctl": ctl}


def _edge_golden(ids: dict[str, int]) -> list[dict]:
    return [
        {
            "q": "UART dropping bytes at high baud rate",
            "expect_item_id": ids["fix"],
            "expect_rank_le": 5,
        },
        {
            "q": "DNP3 outstation poll interval register map",
            "expect_item_id": ids["layer"],
            "expect_rank_le": 5,
        },
        {
            "q": "PageNumber CURRENT field Word footer",
            "expect_item_id": ids["ctl"],
            "expect_rank_le": 5,
        },
    ]


def test_ab_reports_both_sides_and_a_positive_delta_when_edges_help(kb, monkeypatch):
    monkeypatch.setattr(eval_sentinel, "GRAPH_AB_ENABLED", True)  # arm the parked A/B
    ids = _seed_edge_reachable_corpus()
    golden = _edge_golden(ids)

    # Sanity: the neighbours really are unreachable without expansion and
    # reachable with it — otherwise the delta below would be measuring noise.
    assert ids["fix"] not in [h.item_id for h in search(golden[0]["q"], hops=0, tool=None)]
    assert ids["fix"] in [h.item_id for h in search(golden[0]["q"], hops=1, tool=None)]
    assert ids["layer"] not in [h.item_id for h in search(golden[1]["q"], hops=0, tool=None)]

    counts = _run_sentinel(monkeypatch, golden)

    assert counts["eval_status"] == "ok"
    assert counts["eval_ab_status"] == "ok"
    # OFF: only the control case passes. ON: the two edge-reachable ones join it.
    assert counts["eval_ab_off_hit_at_5"] == pytest.approx(1 / 3, abs=1e-4)
    assert counts["eval_ab_on_hit_at_5"] == 1.0
    assert counts["eval_ab_delta_hit_at_5"] == pytest.approx(
        counts["eval_ab_on_hit_at_5"] - counts["eval_ab_off_hit_at_5"], abs=1e-4
    )
    assert counts["eval_ab_delta_mrr"] == pytest.approx(
        counts["eval_ab_on_mrr"] - counts["eval_ab_off_mrr"], abs=1e-4
    )
    assert counts["eval_ab_delta_mrr"] > 0
    # The ON side uses the SHIPPED expand→hops mapping, not a private constant.
    assert counts["eval_ab_hops_on"] == search_expand.hops_from_expand(True) == 1


def test_ab_delta_is_zero_when_expansion_adds_nothing(kb, monkeypatch):
    """No edges in the corpus → both sides identical. The instrument must report
    "expansion does not help HERE" rather than manufacture a difference."""
    monkeypatch.setattr(eval_sentinel, "GRAPH_AB_ENABLED", True)  # arm the parked A/B
    ingest(
        "docx-tools",
        "gotcha",
        "PageNumber CURRENT field in a Word footer",
        "Insert a PageNumber CURRENT field node into the Footer to number pages.",
        kind="chat",
        uri="ab0:ctl",
    )
    golden = [{"q": "PageNumber CURRENT field Word footer", "expect_substr": ["Footer"]}]

    counts = _run_sentinel(monkeypatch, golden)

    assert counts["eval_ab_off_hit_at_5"] == counts["eval_ab_on_hit_at_5"] == 1.0
    assert counts["eval_ab_delta_hit_at_5"] == 0.0
    assert counts["eval_ab_delta_mrr"] == 0.0


def test_ab_forces_expand_uniformly_over_a_cases_own_kwargs(kb, monkeypatch):
    """A case pinning kwargs.expand must be overridden on BOTH sides (golden.yaml
    has such a case) — otherwise the two sides measure different configurations."""
    monkeypatch.setattr(eval_sentinel, "GRAPH_AB_ENABLED", True)  # arm the parked A/B
    ids = _seed_edge_reachable_corpus()
    golden = [
        {
            "q": "UART dropping bytes at high baud rate",
            "expect_item_id": ids["fix"],
            "kwargs": {"expand": True},  # pinned ON by the case itself
        }
    ]

    counts = _run_sentinel(monkeypatch, golden)

    assert counts["eval_hit_at_5"] == 1.0, "the primary pass honours the case's own expand"
    assert counts["eval_ab_off_hit_at_5"] == 0.0, "the OFF side overrides expand=true"
    assert counts["eval_ab_on_hit_at_5"] == 1.0
    # The case objects themselves are untouched: the primary metrics must stay
    # reproducible from the same golden list.
    assert golden[0]["kwargs"] == {"expand": True}


def test_primary_metrics_survive_an_ab_failure(kb, monkeypatch):
    """The A/B is report-only: it cannot touch the regression-gating metrics, and
    an instrument that explodes costs nothing but its own status field."""
    monkeypatch.setattr(eval_sentinel, "GRAPH_AB_ENABLED", True)  # arm the parked A/B
    ids = _seed_edge_reachable_corpus()
    golden = _edge_golden(ids)

    ok = _run_sentinel(monkeypatch, golden)

    def _boom(*a, **k):
        raise RuntimeError("A/B side blew up")

    monkeypatch.setattr(eval_sentinel, "_ab_side", _boom)
    broken = _run_sentinel(monkeypatch, golden)

    assert broken["eval_status"] == "ok"
    assert broken["eval_ab_status"] == "error"
    assert broken["eval_hit_at_5"] == ok["eval_hit_at_5"]
    assert broken["eval_mrr"] == ok["eval_mrr"]
    assert broken["eval_queries"] == ok["eval_queries"] == 3
    # A failed instrument leaves NO partial numbers behind to be misread.
    for key in ("eval_ab_off_hit_at_5", "eval_ab_on_hit_at_5", "eval_ab_hops_on"):
        assert key not in broken
    # 'watch' is also a completed run (Wave F verified-lift watch) — only
    # 'error' would mean the other instrument was lost.
    assert broken["eval_trust_status"] in ("ok", "watch"), "the other instrument still ran"


def test_ab_can_be_switched_off(kb, monkeypatch):
    monkeypatch.setattr(eval_sentinel, "GRAPH_AB_ENABLED", False)

    def _boom(*a, **k):
        raise AssertionError("the A/B must not run when disabled")

    monkeypatch.setattr(eval_sentinel, "_ab_side", _boom)
    counts = _run_sentinel(monkeypatch, [{"q": "anything", "expect_substr": ["x"]}])

    assert counts["eval_ab_status"] == "disabled"
    assert "eval_ab_off_hit_at_5" not in counts


def test_ab_is_parked_off_by_default(kb, monkeypatch):
    """Wave-3: the A/B ships PARKED (no live relation-only fixture resolves on the
    corpus). The module default must be OFF, and a default run must replay neither
    A/B side — that is what reclaims the two nightly replays."""
    assert eval_sentinel.GRAPH_AB_ENABLED is False

    def _boom(*a, **k):
        raise AssertionError("the parked A/B must not replay a side")

    monkeypatch.setattr(eval_sentinel, "_ab_side", _boom)
    counts = _run_sentinel(monkeypatch, [{"q": "anything", "expect_substr": ["x"]}])

    assert counts["eval_ab_status"] == "disabled"
    assert "eval_ab_off_hit_at_5" not in counts


# --- 2. trust composition ----------------------------------------------------


def _seed_mixed_trust_corpus() -> None:
    """1 verified + 3 unverified items on one topic, 1 verified on another.

    All embeddings are blinded so the keyword leg alone decides the result sets:
    the topic query returns exactly 4 hits and the other exactly 1, which is what
    makes pooled-vs-averaged distinguishable (and the counts exact).
    """
    ids = [
        ingest(
            "kb-system",
            "decision",
            "Widget calibration decision",
            "The widget calibration routine settles on the median of three reads.",
            kind="chat",
            uri="tr:v",
            trust="verified",
        )
    ]
    for n in range(3):
        ids.append(
            ingest(
                "kb-system",
                "note",
                f"Widget calibration observation {n}",
                f"Observed widget calibration drift on bench {n} during the long soak.",
                kind="chat",
                uri=f"tr:u{n}",
                trust="unverified",
            )
        )
    ids.append(
        ingest(
            "kb-system",
            "note",
            "Zirconium cladding tolerances",
            "Zirconium cladding tolerances are recorded here and nowhere else.",
            kind="chat",
            uri="tr:z",
            trust="verified",
        )
    )
    for item_id in ids:
        _blind(item_id)


def test_trust_share_computes_on_a_mixed_corpus(kb, monkeypatch):
    _seed_mixed_trust_corpus()
    golden = [{"q": "widget calibration", "expect_substr": ["calibration"]}]

    counts = _run_sentinel(monkeypatch, golden)

    # Wave F: this corpus (2 verified of 5 → fraction 0.4, verified top-5 share
    # 0.25 → lift 0.625 < 5x floor) deterministically trips the verified-lift
    # watch; the composition numbers themselves are unchanged.
    assert counts["eval_trust_status"] == "watch"
    assert counts["eval_top5_results"] == 4
    assert counts["eval_top5_unverified"] == 3
    assert counts["eval_top5_unverified_share"] == 0.75


def test_trust_share_is_pooled_over_queries_not_averaged(kb, monkeypatch):
    """Micro-average: a query returning one hit must not weigh as much as one
    returning four — that is the entire difference between the two formulas."""
    _seed_mixed_trust_corpus()
    golden = [
        {"q": "widget calibration", "expect_substr": ["calibration"]},
        {"q": "zirconium cladding tolerances", "expect_substr": ["Zirconium"]},
    ]

    counts = _run_sentinel(monkeypatch, golden)

    # Pooled: 3 unverified of 5 pooled hits (4 + 1) = 0.6.
    # A mean of per-query shares would report (0.75 + 0.0) / 2 = 0.375.
    assert counts["eval_top5_results"] == 5
    assert counts["eval_top5_unverified"] == 3
    assert counts["eval_top5_unverified_share"] == 0.6


def test_summary_share_counts_summary_typed_top5_hits(kb, monkeypatch):
    """eval_top5_summary_share: the POOLED share of top-5 hits whose type is
    'summary', reported alongside the unverified share over the same read-only
    pass — the signal for whether consolidation summaries earn their slots."""
    s = ingest(
        "kb-system",
        "summary",
        "Widget calibration rollup",
        "The widget calibration rollup summarizes the bench soak observations.",
        kind="chat",
        uri="sm:s",
        trust="unverified",
    )
    n = ingest(
        "kb-system",
        "note",
        "Widget calibration observation",
        "A widget calibration bench observation recorded during the long soak.",
        kind="chat",
        uri="sm:n",
        trust="unverified",
    )
    for item_id in (s, n):
        _blind(item_id)  # keyword-only, so the result set is exactly these two
    golden = [{"q": "widget calibration", "expect_substr": ["calibration"]}]

    counts = _run_sentinel(monkeypatch, golden)

    # Wave F: an all-unverified two-item corpus (share 1.0 > 0.75 ceiling)
    # deterministically trips the verified-lift watch; the summary-share
    # numbers themselves are unchanged.
    assert counts["eval_trust_status"] == "watch"
    assert counts["eval_top5_results"] == 2
    assert counts["eval_top5_summary"] == 1
    assert counts["eval_top5_summary_share"] == 0.5
    # both hits are unverified, so the new signal is orthogonal to the trust share
    assert counts["eval_top5_unverified_share"] == 1.0


def test_trust_metric_is_report_only(kb, monkeypatch):
    """It must not change ranking and must not stamp items 'recently used'
    (tool=None): eval reads are not usage, and a pinned item never archives."""
    _seed_mixed_trust_corpus()
    golden = [{"q": "widget calibration", "expect_substr": ["calibration"]}]
    before = [(h.item_id, h.score) for h in search("widget calibration", tool=None)]

    counts = _run_sentinel(monkeypatch, golden)

    after = [(h.item_id, h.score) for h in search("widget calibration", tool=None)]
    assert after == before, "the instrument must not perturb ranking"
    assert counts["eval_top5_results"] > 0
    with db.connect() as conn:
        logged = conn.execute("SELECT count(*) FROM access_log").fetchone()[0]
    assert logged == 0, "the eval's replays must never write access_log"


def test_trust_metric_failure_does_not_break_the_sentinel(kb, monkeypatch):
    _seed_mixed_trust_corpus()

    def _boom(*a, **k):
        raise RuntimeError("search down")

    monkeypatch.setattr(eval_sentinel, "search", _boom)
    counts = _run_sentinel(monkeypatch, [{"q": "widget calibration", "expect_substr": ["widget"]}])

    assert counts["eval_status"] == "ok", "the primary metrics still stand"
    assert counts["eval_trust_status"] == "error"
    assert "eval_top5_unverified_share" not in counts


def test_trust_metric_can_be_switched_off(kb, monkeypatch):
    monkeypatch.setattr(eval_sentinel, "TRUST_METRIC_ENABLED", False)
    counts = _run_sentinel(monkeypatch, [{"q": "anything", "expect_substr": ["x"]}])

    assert counts["eval_trust_status"] == "disabled"
    assert "eval_top5_unverified_share" not in counts


# --- 3. rerank evidence flag -------------------------------------------------


@pytest.fixture()
def _capture_rerank(monkeypatch):
    """Point the rerank leg at a stub sidecar and capture the documents sent."""
    monkeypatch.setattr(settings, "kb_rerank_url", "http://127.0.0.1:9")
    sent: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": [{"index": i, "relevance_score": 0.5} for i in range(sent["n"])]}

    def post(url, *, json=None, timeout=None):
        sent["documents"] = list(json["documents"])
        sent["n"] = len(json["documents"])
        return _Resp()

    monkeypatch.setattr(rerank.httpx, "post", post)
    return sent


NEEDLE = "drops frames"


def _seed_long_bodied_items() -> dict[int, tuple[str, str]]:
    """Multi-chunk items whose matching chunk is NOT the body prefix.

    Each body opens with filler and buries the query terms in a closing section,
    so `title + best chunk` and `title + left(body, 1500)` are different strings
    — without that the flag ON/OFF comparison would prove nothing. Non-matching
    chunks are blinded so the fused snippet is deterministically the chunk that
    carries the query terms.
    """
    out: dict[int, tuple[str, str]] = {}
    for tag in ("alpha", "beta"):
        filler = " ".join(
            f"Preamble sentence {i} about unrelated housekeeping." for i in range(200)
        )
        body = (
            f"# Overview\n\n{filler}\n\n"
            f"# Findings\n\nThe {tag} telemetry pipeline {NEEDLE} when the "
            f"{tag} collector restarts mid-window.\n"
        )
        title = f"{tag} telemetry pipeline"
        item_id = ingest("kb-system", "note", title, body, kind="chat", uri=f"rr:{tag}")
        _blind_except(item_id, NEEDLE)
        out[item_id] = (title, body)
    return out


def test_flag_ships_off():
    assert Settings.model_fields["kb_rerank_use_chunk"].default is False


def test_flag_off_is_byte_identical_to_today(kb, monkeypatch, _capture_rerank):
    """REGRESSION PIN: with kb_rerank_use_chunk OFF the reranker still receives
    exactly ``title + left(body, 1500)`` — the pre-flag document, byte for byte."""
    monkeypatch.setattr(settings, "kb_rerank_use_chunk", False)
    items = _seed_long_bodied_items()

    search("telemetry pipeline drops frames", tool=None)

    expected = {f"{title}\n{body[:1500]}" for title, body in items.values()}
    assert set(_capture_rerank["documents"]) == expected


def test_flag_on_feeds_the_best_matching_chunk(kb, monkeypatch, _capture_rerank):
    items = _seed_long_bodied_items()
    monkeypatch.setattr(settings, "kb_rerank_use_chunk", True)

    search("telemetry pipeline drops frames", tool=None)

    docs = set(_capture_rerank["documents"])
    assert docs != {f"{title}\n{body[:1500]}" for title, body in items.values()}
    for item_id, (title, body) in items.items():
        chunks = _chunk_texts(item_id)
        assert len(chunks) > 1, "the fixture must be multi-chunk or it proves nothing"
        matching = [c for c in chunks if NEEDLE in c]
        assert matching, "the fixture's query terms must live in a chunk"
        assert any(f"{title}\n{c}" in docs for c in matching)
        assert f"{title}\n{body[:1500]}" not in docs


def test_evidence_helper_respects_the_flag(monkeypatch):
    """Unit pin on the pass-through: OFF → None (the reranker keeps its body
    prefix); ON → the fused rows' chunk text, empty snippets omitted."""
    from kbcore import search as search_mod

    fused = [(1, 0.5, "chunk one"), (2, 0.3, ""), (3, 0.2, "chunk three")]

    monkeypatch.setattr(settings, "kb_rerank_use_chunk", False)
    assert search_mod._rerank_evidence(fused) is None

    monkeypatch.setattr(settings, "kb_rerank_use_chunk", True)
    assert search_mod._rerank_evidence(fused) == {1: "chunk one", 3: "chunk three"}


def test_documents_fall_back_to_body_for_chunkless_candidates():
    """A pure graph neighbour carries no chunk snippet; it must keep the body
    prefix rather than becoming an empty document the reranker cannot score."""

    class _Conn:
        def execute(self, *a):
            class _R:
                def fetchall(self):
                    return [(1, "t1", "b1"), (2, "t2", "b2")]

            return _R()

    assert rerank._documents(_Conn(), [1, 2], {1: "the matching chunk"}) == {
        1: "t1\nthe matching chunk",
        2: "t2\nb2",
    }


# --- 4. E2: the pre-rerank trace + per-case telemetry (review #10) -----------------


@pytest.fixture()
def _reversing_rerank(monkeypatch):
    """A stub sidecar that scores the window in REVERSE, plus a spy on the fused
    candidates exactly as ``rerank_fused`` receives them (sorted, its own view)."""
    monkeypatch.setattr(settings, "kb_rerank_url", "http://127.0.0.1:9")
    seen: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            n = seen["n"]
            return {"results": [{"index": i, "relevance_score": (i + 1) / n} for i in range(n)]}

    def post(url, *, json=None, timeout=None):
        seen["n"] = len(json["documents"])
        return _Resp()

    real = rerank.rerank_fused

    def spy(conn, query, fused, top_n, evidence=None):
        seen["pre"] = [t[0] for t in sorted(fused, key=lambda t: -t[1])]
        return real(conn, query, fused, top_n, evidence=evidence)

    monkeypatch.setattr(rerank.httpx, "post", post)
    monkeypatch.setattr(rerank, "rerank_fused", spy)
    return seen


def _traced(query: str) -> tuple[list, list[int] | None]:
    search_trace.begin()
    try:
        hits = search(query, tool=None)
    finally:
        captured = search_trace.end()
    return hits, captured


def test_search_trace_capture_equals_the_pre_rerank_order(kb, _reversing_rerank):
    _seed_mixed_trust_corpus()
    hits, captured = _traced("widget calibration")
    assert captured == _reversing_rerank["pre"], "the reranker's own input order"
    assert len(captured) == 4 and rerank.last_outcome() == "active"
    assert [h.item_id for h in hits] == captured[::-1], "the stub reversed the window"


def test_with_rerank_off_the_capture_is_the_final_order(kb):
    _seed_mixed_trust_corpus()
    hits, captured = _traced("widget calibration")
    assert captured == [h.item_id for h in hits], "distinct fused scores: _to_hits keeps the order"


def test_search_results_are_identical_with_and_without_an_active_trace(kb, _reversing_rerank):
    _seed_mixed_trust_corpus()
    plain = [(h.item_id, h.score, h.snippet) for h in search("widget calibration", tool=None)]
    hits, captured = _traced("widget calibration")
    assert [(h.item_id, h.score, h.snippet) for h in hits] == plain
    assert captured is not None
    # Inert outside a trace: nothing is recorded, nothing raises — even on junk rows.
    search_trace.capture([(1, 0.5, "x")])
    assert search_trace.end() is None
    search_trace.begin()
    search_trace.capture([("not-an-id",)])
    assert search_trace.end() is None, "a malformed row records no order, never raises"


def test_above_satisfier_and_fused_telemetry_shape(kb, monkeypatch, _reversing_rerank):
    _seed_mixed_trust_corpus()
    q = "widget calibration"
    final = search(q, tool=None)
    pre = _reversing_rerank["pre"]
    target = final[2]  # sits at final rank 3 — two hits above it
    golden = [
        {"q": q, "expect_item_id": target.item_id},
        {"q": q, "expect_substr": ["zz-no-such-token"]},  # unsatisfied: all of top-K is above
    ]
    monkeypatch.setattr(eval_sentinel.eval_harness, "load_golden", lambda golden_path=None: golden)
    monkeypatch.setattr(eval_sentinel.eval_epoch, "golden_hash", lambda path=None: "a1a1a1a1a1a1")
    counts: dict = {}
    eval_sentinel.run_sentinel(counts)
    assert counts["eval_case_passed"] == {q: True, f"{q} #2": False}
    assert counts["eval_case_top5"][q] == [h.item_id for h in final]
    assert counts["eval_case_satisfier"] == {q: target.item_id, f"{q} #2": None}
    assert counts["eval_case_above_satisfier"][q] == [
        [h.item_id, h.type, h.trust] for h in final[:2]
    ], "[id, type, trust] per hit ranked above the satisfier"
    assert counts["eval_case_above_satisfier"][f"{q} #2"] == [
        [h.item_id, h.type, h.trust] for h in final
    ]
    assert counts["eval_case_fused_rank"] == {q: pre.index(target.item_id) + 1, f"{q} #2": None}
    assert counts["eval_case_ranks"][q] == 3 != counts["eval_case_fused_rank"][q], (
        "the fused position is the PRE-rerank one"
    )
    assert counts["eval_case_n_fused"] == {q: 4, f"{q} #2": 4}
    assert counts["eval_echo_skips"] == {} and counts["eval_echo_skipped"] == 0
    report_id = run_report._write_report(dict(counts))
    with db.connect() as conn:
        body = conn.execute("SELECT body FROM items WHERE id = %s", (report_id,)).fetchone()[0]
    assert q not in body and "eval_case_" not in body, "per-case dicts are META only"


def test_a_search_the_trace_never_sees_leaves_the_fused_keys_unset(kb, monkeypatch):
    """A stubbed search never reaches capture: no fake zeros, the case simply has no key."""
    monkeypatch.setattr(eval_harness, "search", lambda *a, **k: [])
    (r,), _h, _m = eval_harness.evaluate(golden=[{"q": "x y", "expect_substr": ["z"]}])
    assert (r.n_fused, r.fused_rank, r.top_ids, r.above) == (None, None, [], [])
    keys = eval_cases.replay_keys([r])
    assert "x y" not in keys["eval_case_n_fused"] and "x y" not in keys["eval_case_fused_rank"]
    assert keys["eval_case_passed"] == {"x y": False} and keys["eval_case_satisfier"] == {
        "x y": None
    }


# --- 5. review #10 fix round: the verdict, the capture's sort, fused-rank edges ------


def _note(n: int, project: str = "hw-lab") -> int:
    return ingest(
        project,
        "note",
        f"Widget calibration observation {n}",
        f"Observed widget calibration drift on bench {n}; median of three reads.",
        kind="chat",
        uri=f"probe:{n}",
        trust="verified",
    )


def test_eval_case_passed_is_the_full_verdict(kb):
    """TA-2: a matched positive criterion with a failing forbid is a FAILED case."""
    item = _note(1)
    q = "widget calibration"
    golden = [{"q": q, "expect_substr": ["median of three"], "forbid_item_id": item}]
    results, _h, _m = eval_harness.evaluate(golden=golden)
    (r,) = results
    assert r.rank == 1 and r.passed is False
    keys = eval_cases.replay_keys(results)
    assert keys["eval_case_passed"] == {q: False}
    assert keys["eval_case_satisfier"] == {q: item}, "the matched id, though the case failed"


def test_the_capture_sorts_by_fused_score_not_list_order():
    """TA-4: _apply_bonuses rescores without re-sorting; capture is the reranker's view."""
    search_trace.begin()
    search_trace.capture([(1, 0.1, ""), (2, 0.5, ""), (3, 0.3, "")])
    assert search_trace.end() == [2, 3, 1]


def test_the_capture_matches_the_rerank_input_under_a_reordering_bonus(
    kb, monkeypatch, _reversing_rerank
):
    _seed_mixed_trust_corpus()  # blinded: the keyword leg alone ranks them
    boosted = _blind(_note(9, project="hw-lab"))  # ranks below rank 1; the 1.25x boost lifts it
    handed: dict = {}
    real = search_trace.capture

    def spy(fused):
        handed["raw"] = [t[0] for t in fused]
        real(fused)

    monkeypatch.setattr(search_trace, "capture", spy)
    search_trace.begin()
    try:
        search("widget calibration", tool=None, boost_project="hw-lab")
    finally:
        captured = search_trace.end()
    assert boosted in captured and handed["raw"] != captured, "fixture: the bonus re-scored"
    assert captured == _reversing_rerank["pre"], "the capture is the reranker's own order"


def test_n_fused_is_the_fused_length_not_the_top_k(kb):
    """TA-8: seven candidates fuse; only K reach the top-K."""
    for n in range(7):
        _note(n)
    golden = [{"q": "widget calibration", "expect_substr": ["bench 6"]}]
    (r,), _h, _m = eval_harness.evaluate(golden=golden)
    assert len(r.top_ids) == eval_harness.K and r.n_fused == 7
    keys = eval_cases.replay_keys([r])
    assert keys["eval_case_n_fused"]["widget calibration"] > len(
        keys["eval_case_top5"]["widget calibration"]
    )


def test_the_fused_satisfier_is_the_chain_head_or_the_raw_id(kb):
    """TA-12: expect_item_id resolves to the CHAIN HEAD, expect_item_id_raw never does."""
    old = _note(1)
    new = supersede.supersede(
        old, new_body="Widget calibration revised: median of five.", reason="r"
    )
    assert eval_criteria.fused_rank({"q": "x", "expect_item_id": old}, [new, old]) == 1
    assert eval_criteria.fused_rank({"q": "x", "expect_item_id_raw": old}, [new, old]) == 2


def test_the_fused_substr_satisfier_reads_the_title_too(kb):
    item = ingest("hw-lab", "note", "Zephyr torque stripe", "nothing else here", kind="chat")
    assert eval_criteria.fused_rank({"q": "z", "expect_substr": ["torque stripe"]}, [item]) == 1


def test_a_raising_fused_read_omits_the_fused_keys_and_names_the_case(kb, monkeypatch):
    """F13: an error is never written as a null that reads 'no satisfier fused'."""
    _seed_mixed_trust_corpus()
    real = eval_criteria.fused_rank

    def flaky(case, fused_ids):
        if case.get("expect_substr") == ["zz-broken"]:
            raise RuntimeError("fused read broke")
        return real(case, fused_ids)

    monkeypatch.setattr(eval_criteria, "fused_rank", flaky)
    q = "widget calibration"
    golden = [{"q": q, "expect_substr": ["zz-broken"]}, {"q": q, "expect_substr": ["median"]}]
    results, _h, _m = eval_harness.evaluate(golden=golden)
    assert [r.fused_error for r in results] == [True, False]
    assert (results[0].n_fused, results[0].fused_rank) == (None, None)
    keys = eval_cases.replay_keys(results)
    assert keys["eval_case_fused_error"] == [q], "the case key, by name"
    assert q not in keys["eval_case_fused_rank"] and q not in keys["eval_case_n_fused"]
    assert (
        keys["eval_case_fused_rank"][f"{q} #2"] == 1 and keys["eval_case_n_fused"][f"{q} #2"] == 4
    )
    assert "eval_case_fused_error" not in eval_cases.replay_keys(results[1:]), "only when one broke"

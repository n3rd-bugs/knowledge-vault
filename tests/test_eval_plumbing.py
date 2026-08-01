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

from kbcore import db, graph, rerank, search_expand
from kbcore.config import Settings, settings
from kbcore.ingest import ingest
from kbcore.search import search
from kbworker import eval_sentinel

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
    assert broken["eval_trust_status"] == "ok", "the other instrument still ran"


def test_ab_can_be_switched_off(kb, monkeypatch):
    monkeypatch.setattr(eval_sentinel, "GRAPH_AB_ENABLED", False)

    def _boom(*a, **k):
        raise AssertionError("the A/B must not run when disabled")

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

    assert counts["eval_trust_status"] == "ok"
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

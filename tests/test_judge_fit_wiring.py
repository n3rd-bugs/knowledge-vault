"""KB 55589 wiring: every pair judge sends the ONE fitted request (no DB, Ollama stubbed).

The fitter itself is pinned in ``test_judge_fit``; this file pins that the contradiction
seat, the seat bench, the evidence judge and the same-fact judge all go through it, that
their seeds stay drawn from the ORIGINAL texts, and that the same-fact cut does not move
with the per-call nonce.
"""

from __future__ import annotations

import re

import pytest

from kbcore import bench_judge, judge_fit, llm, twin_judge
from kbcore.chunk import count_tokens
from kbcore.config import settings


def _words(n: int, tag: str) -> str:
    return " ".join(f"{tag}{i}" for i in range(n))


BIG_A, BIG_B, BIG_C = _words(12000, "a"), _words(12000, "b"), _words(9000, "c")


@pytest.fixture(autouse=True)
def _window(monkeypatch):
    monkeypatch.setattr(settings, "kb_llm_num_ctx", 16384)


@pytest.fixture()
def sent(monkeypatch) -> list[tuple[list[dict], dict]]:
    calls: list[tuple[list[dict], dict]] = []

    def chat(messages, **kw):
        calls.append((messages, kw))
        return '{"contradict": true, "side": "a"}'

    monkeypatch.setattr(llm, "_chat", chat)
    return calls


def _size(messages: list[dict]) -> int:
    return sum(count_tokens(m["content"]) for m in messages)


def test_an_oversized_contradiction_is_fitted_and_seeded_from_the_original_texts(sent):
    assert llm.items_contradict("A", BIG_A, "B", BIG_B) is True
    ((messages, kw),) = sent
    assert _size(messages) <= judge_fit.judge_ceiling()
    assert "tokens omitted ...]" in messages[1]["content"]
    assert kw["seed"] == llm._draw_seed("A", BIG_A, "B", BIG_B)  # not the fitted texts
    assert kw["model"] == settings.contradiction_judge_model


def test_the_bench_re_asks_exactly_the_fitted_production_request(sent):
    llm.items_contradict("A", BIG_A, "B", BIG_B)
    assert bench_judge.contradict_messages("A", BIG_A, "B", BIG_B) == sent[0][0]


def test_the_bench_asks_twice_like_production_when_the_first_draw_is_blank(monkeypatch):
    seeds: list[int] = []
    replies = iter(["", '{"contradict": true}'])
    monkeypatch.setattr(llm, "_chat", lambda m, **k: seeds.append(k["seed"]) or next(replies))
    monkeypatch.setattr(
        bench_judge, "_pair_rows", lambda a, b: (("p", "A", "x is 5"), ("p", "B", "x is 7"))
    )
    pair = bench_judge.BenchPair(1, 2, bench_judge.KIND_CONTRADICT, True, "file")
    verdict = bench_judge.judge_pair(pair, model="seat:27b", think=None)
    base = llm._draw_seed("A", "x is 5", "B", "x is 7")
    assert verdict.verdict is True and seeds == [base, base + 1]


def test_an_oversized_evidence_prompt_is_fitted_and_seeded_from_the_original_texts(sent):
    assert llm.evidence_side("A", BIG_A, "B", BIG_B, "C", BIG_C) == "a"
    ((messages, kw),) = sent
    assert _size(messages) <= judge_fit.judge_ceiling()
    assert messages[1]["content"].count("tokens omitted ...]") == 3
    assert kw["seed"] == llm._draw_seed("A", BIG_A, "B", BIG_B, "C", BIG_C)


def test_the_same_fact_cut_is_the_same_on_every_call_whatever_the_nonce(monkeypatch):
    # A light nonce (2 cl100k tokens) and the heaviest shape (16 single-digit tokens): if the
    # fit were measured with the REAL nonce, the cut would move between these two calls.
    nonces = iter(["ffffffffffffffff", "1a2b3c4d5e6f7a8b"])
    monkeypatch.setattr(twin_judge.secrets, "token_hex", lambda _n: next(nonces))
    first = twin_judge.same_fact_messages("p1", "A", BIG_A, "p2", "B", BIG_B)
    second = twin_judge.same_fact_messages("p1", "A", BIG_A, "p2", "B", BIG_B)
    assert first[1]["content"] != second[1]["content"], "a fresh nonce per call"

    def unfenced(m: list[dict]) -> str:
        return re.sub(r"\b[0-9a-f]{16}\b", "NONCE", m[1]["content"])

    assert unfenced(first) == unfenced(second), "the cut must not move with the nonce"
    for m in (first, second):
        assert _size(m) <= judge_fit.judge_ceiling()
        assert m[1]["content"].count("tokens omitted ...]") == 2


def test_a_same_fact_pair_that_fits_keeps_the_fenced_shape_untouched():
    (system, user) = twin_judge.same_fact_messages("p1", "A", "x is 5", "p2", "B", "x is 7")
    nonce = re.search(r"BEGIN ITEM_A ([0-9a-f]{16})", user["content"]).group(1)
    assert system["content"] == twin_judge.SAME_FACT_SYSTEM
    assert user["content"] == (
        f"BEGIN ITEM_A {nonce} (project p1)\nA\nx is 5\nEND ITEM_A {nonce}\n\n"
        f"BEGIN ITEM_B {nonce} (project p2)\nB\nx is 7\nEND ITEM_B {nonce}\n\n"
        "Do ITEM_A and ITEM_B record the same underlying fact?"
    )


# The prompt bonsai-2:27b's 0.78 precision was benched on (KB 54572), as it stood in llm.py
# before the move — any change to it needs a new bench, like EVIDENCE_SYSTEM.
BENCHED_CONTRADICT_SYSTEM = (
    "You compare two knowledge items and decide whether they factually CONTRADICT — "
    "state incompatible facts or decisions about the same thing (not merely different "
    'topics). Answer STRICT JSON only: {"contradict": true} or {"contradict": false}. '
    "If you are unsure, answer false."
)


def test_the_contradiction_prompt_is_the_one_moved_verbatim():
    assert judge_fit.CONTRADICT_SYSTEM == BENCHED_CONTRADICT_SYSTEM
    assert llm._CONTRADICT_SYSTEM is judge_fit.CONTRADICT_SYSTEM


# --- the nightly digest -------------------------------------------------------


@pytest.mark.parametrize(
    ("counts", "line"),
    [
        ({}, None),  # a failed step carries no count: no line, as before
        ({"contradiction_pairs_judged": 0}, "judged pairs: 0"),
        ({"contradiction_pairs_judged": 40, "contradiction_pairs_trimmed": 0}, "judged pairs: 40"),
        (
            {"contradiction_pairs_judged": 40, "contradiction_pairs_trimmed": 3},
            "judged pairs: 40 (3 trimmed)",
        ),
        (
            {
                "contradiction_pairs_judged": 40,
                "contradiction_pairs_trimmed": 3,
                "contradiction_pairs_no_verdict": 1,
            },
            "judged pairs: 40 (3 trimmed, 1 no verdict)",
        ),
        (
            {"contradiction_pairs_judged": 12, "contradiction_pairs_resting": 4},
            "judged pairs: 12 (4 resting)",
        ),
    ],
)
def test_the_judged_pairs_digest_line(counts, line):
    from kbworker import digest, digest_recency_lines

    assert digest_recency_lines.judged_line(counts) == line
    lines = digest.digest_lines(counts)
    assert (line in lines) if line else not any(x.startswith("judged pairs") for x in lines)


# --- the no-verdict breaker never swallows the run's own stop ----------------------


def test_the_breaker_passes_the_runs_stop_through_and_it_wins():
    from kbworker.contradiction_budget import no_verdict_guard

    broken = {"judged": 10, "no_verdict": 10, "fresh_judged": 10, "fresh_blank": 10}
    clean = {"judged": 10, "no_verdict": 0, "fresh_judged": 10, "fresh_blank": 0}
    assert no_verdict_guard(lambda: "battery", broken)() == "battery"
    assert no_verdict_guard(lambda: "deadline", clean)() == "deadline"
    assert no_verdict_guard(lambda: None, broken)() == "no verdict"
    assert no_verdict_guard(None, broken)() == "no verdict"
    assert no_verdict_guard(None, clean)() is None
    early = {"judged": 5, "no_verdict": 5, "fresh_judged": 5, "fresh_blank": 5}
    assert no_verdict_guard(None, early)() is None, "under the floor: never on a hunch"


def test_the_contradiction_step_still_stands_down_on_battery(monkeypatch):
    from kbworker import tail_ops

    pairs = [(1, 2, 0.9), (3, 4, 0.9), (5, 6, 0.9)]
    monkeypatch.setattr(tail_ops.consolidation, "contradiction_pairs", lambda *a, **k: pairs)
    monkeypatch.setattr(
        tail_ops.consolidation, "item_pair_texts", lambda *a: pytest.fail("judged on battery")
    )
    tail_ops._STOP_LOG.clear()
    assert tail_ops._contradictions_all(["p"], lambda: "battery") == 0
    assert tail_ops._STOP_LOG["contradictions_queued"] == {"reason": "battery", "deferred": 3}

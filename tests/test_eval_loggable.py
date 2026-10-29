"""Query-keyed eval dicts never reach a log line or a stored job result (review #10 F16).

Every per-case dict the sentinel mints is keyed by the golden QUERY TEXT
(kbworker.eval_cases.case_key). They belong in the report META — never in the
"consolidation done" journald line, the one-shot's stdout, or the on-demand
result kept in consolidation_requests (kb_consolidate / the CLI / GET
/api/consolidate). ``eval_cases.loggable`` is the one filter; its constant
``QUERY_KEYED`` must name EVERY such key, so this file fails when a new
case_key producer is added without listing it.

Synthetic queries only (golden-text hygiene). Real kb_test DB for the run.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from kbcore import eval_harness, llm
from kbcore.config import settings
from kbcore.items import get_item
from kbworker import consolidate, eval_cases, eval_composition, eval_sentinel, eval_trust

Q = "synthetic prose question about the bench widget drift"
SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(autouse=True)
def _mock_extractor(monkeypatch, tmp_path):
    """The 14B is never called (Invariant 8); raw reads stay off the real ~/kb."""
    monkeypatch.setattr(llm, "summarize_items", lambda topic, items: f"Summary of {topic}.")
    monkeypatch.setattr(llm, "items_contradict", lambda *a, **k: False)
    monkeypatch.setattr(settings, "kb_raw", tmp_path / "kbraw")


def _results() -> list:
    """CaseResults exercising EVERY per-case field (two cases share the query)."""
    return [
        eval_harness.CaseResult(
            Q, 1, True, top_ids=[1, 2], above=[], echo_skipped=[2], fused_rank=1, n_fused=3
        ),
        eval_harness.CaseResult(Q, None, False, top_ids=[3], above=[[3, "note", "verified"]]),
        eval_harness.CaseResult(Q, 2, True, top_ids=[4, 5], fused_error=True),
    ]


def _produced(monkeypatch) -> dict:
    """Every producer's keys over the synthetic replay — the sentinel's own fold."""
    monkeypatch.setattr(eval_cases, "_fingerprint", lambda: {})
    counts: dict = {}
    eval_cases.replay_telemetry(counts, _results())
    hit = SimpleNamespace(item_id=1, title="t", snippet="", type="note", trust="verified")
    monkeypatch.setattr(eval_sentinel, "search", lambda *a, **k: [hit])
    monkeypatch.setattr(eval_trust, "verified_lift", lambda share, n: {})
    counts.update(eval_composition._trust_composition([{"q": Q, "expect_substr": ["t"]}]))
    return counts


def test_every_key_carrying_query_text_is_listed_and_dropped(monkeypatch):
    counts = _produced(monkeypatch)
    carrying = {k for k, v in counts.items() if Q in json.dumps(v)}
    assert carrying, "fixture: the producers ran"
    missing = carrying - set(eval_cases.QUERY_KEYED)
    assert not missing, f"query-keyed keys not in eval_cases.QUERY_KEYED: {sorted(missing)}"
    assert {"eval_case_fused_error", "eval_case_rerank_composition"} <= carrying
    kept = eval_cases.loggable(counts)
    assert Q not in json.dumps(kept) and Q not in str(kept)  # what a %s log line renders
    assert kept["eval_echo_skipped"] == 1, "the scalars stay"
    assert set(counts) - set(kept) == carrying, "only the query-keyed keys go"
    assert Q in json.dumps(counts), "loggable copies: the input (report meta) keeps everything"


def test_case_key_is_minted_only_by_the_listed_producers():
    """A NEW module minting case_key-keyed dicts must be added to QUERY_KEYED + this file."""
    minting = sorted(
        p.relative_to(SRC).as_posix()
        for p in SRC.rglob("*.py")
        if re.search(r"\bcase_key\(", p.read_text(encoding="utf-8"))
    )
    assert minting == ["kbworker/eval_cases.py", "kbworker/eval_composition.py"]


def test_the_consolidation_done_line_carries_no_query_keyed_dict(kb, monkeypatch, caplog):
    golden = [{"q": Q, "expect_substr": ["x"]}]
    monkeypatch.setattr(eval_harness, "load_golden", lambda golden_path=None: golden)
    monkeypatch.setattr(
        eval_harness, "evaluate", lambda golden=None, golden_path=None: (_results()[:1], 1.0, 1.0)
    )
    with caplog.at_level(logging.INFO, logger="kbworker.consolidate"):
        result = consolidate.main()
    (line,) = [r.getMessage() for r in caplog.records if "consolidation done" in r.getMessage()]
    assert Q not in line and not any(f"'{k}'" in line for k in eval_cases.QUERY_KEYED)
    assert "'eval_hit_at_5': 1.0" in line, "the run's scalars still log"
    assert result["eval_case_passed"] == {Q: True}, "main's return value is unchanged"
    meta = get_item(result["report_item_id"]).meta
    assert meta["eval_case_ranks"] == {Q: 1} and meta["eval_case_top5"] == {Q: [1, 2]}
    assert Q in meta["eval_case_rerank_composition"], "the report META keeps every dict"


def test_the_one_shot_prints_only_the_loggable_counts():
    """``python -m kbworker.consolidate``'s stdout is journald too, so loggable() wraps
    it. A SOURCE check on purpose: executing the one-shot is a real consolidation."""
    tree = ast.parse((SRC / "kbworker" / "consolidate.py").read_text(encoding="utf-8"))
    (main,) = [n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test)]
    calls = [
        n for n in ast.walk(main) if isinstance(n, ast.Call) and ast.unparse(n.func) == "print"
    ]
    assert [ast.unparse(c.args[0]) for c in calls] == ["loggable(nightly())"]

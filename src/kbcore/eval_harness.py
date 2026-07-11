"""kbcore.eval_harness — golden-set retrieval eval (hit@5 + MRR), shared logic.

Single source for the eval used by BOTH the `make eval` CLI (eval/run_eval.py)
and the nightly regression sentinel (kbworker.eval_sentinel). A query is a hit if
any of its expected substrings appears in the title, snippet, or full body of a
top-K result.

GPU courtesy (Invariant 8): this replays REAL searches → bge-m3 embed calls only;
kbcore.search has no extractor path, so the 14B is never invoked here.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from kbcore.items import get_item
from kbcore.search import search

# Repo root is two levels up from src/kbcore/eval_harness.py (mirrors config.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = _REPO_ROOT / "eval" / "golden.yaml"
K = 5


def load_golden(golden_path: Path | None = None) -> list[dict]:
    """Parse the golden set. An ABSENT file yields ``[]`` so the sentinel can
    record eval=skipped instead of erroring."""
    path = golden_path or GOLDEN_PATH
    if not path.exists():
        return []
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


def evaluate(
    golden: list[dict] | None = None, golden_path: Path | None = None
) -> tuple[list[tuple[str, int | None]], float, float]:
    """Returns (per-query [(query, rank|None)], hit@5, MRR).

    ``golden`` may be preloaded (the sentinel loads once to tell an empty set
    from a real run); otherwise it is read from ``golden_path`` / GOLDEN_PATH.
    """
    cases = golden if golden is not None else load_golden(golden_path)
    results: list[tuple[str, int | None]] = []
    for case in cases:
        query, expected = case["q"], case["expect_substr"]
        hits = search(query, k=K, tool="eval")
        rank: int | None = None
        for i, hit in enumerate(hits, start=1):
            haystack = f"{hit.title}\n{hit.snippet}\n{get_item(hit.item_id).body}"
            if any(sub.lower() in haystack.lower() for sub in expected):
                rank = i
                break
        results.append((query, rank))
    n = len(results)
    hit_at_5 = sum(1 for _, r in results if r is not None) / n if n else 0.0
    mrr = sum(1.0 / r for _, r in results if r is not None) / n if n else 0.0
    return results, hit_at_5, mrr

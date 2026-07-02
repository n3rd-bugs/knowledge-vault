"""eval/run_eval.py — retrieval eval: hit@5 and MRR per golden query (DATA-5).

A query is a hit if any expected substring appears in the title, snippet, or
full body of a top-5 result. Run via `make eval`. Exits non-zero when --gate
is given and hit@5 falls below it (Phase 1 informal gate 0.7; Phase 5 AC 0.8).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from kbcore.items import get_item
from kbcore.search import search

GOLDEN = Path(__file__).resolve().parent / "golden.yaml"
K = 5


def evaluate() -> tuple[list[tuple[str, int | None]], float, float]:
    """Returns (per-query [(query, rank|None)], hit@5, MRR)."""
    golden = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    results: list[tuple[str, int | None]] = []
    for case in golden:
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", type=float, default=None, help="fail (exit 1) if hit@5 < GATE")
    args = parser.parse_args()

    results, hit_at_5, mrr = evaluate()
    width = max(len(q) for q, _ in results)
    print(f"{'query':<{width}}  rank")
    print("-" * (width + 6))
    for query, rank in results:
        print(f"{query:<{width}}  {rank if rank is not None else 'MISS'}")
    print("-" * (width + 6))
    print(f"queries: {len(results)}   hit@5: {hit_at_5:.2f}   MRR: {mrr:.2f}")
    if args.gate is not None and hit_at_5 < args.gate:
        print(f"FAIL: hit@5 {hit_at_5:.2f} < gate {args.gate:.2f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

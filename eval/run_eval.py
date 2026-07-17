"""eval/run_eval.py — retrieval eval CLI: hit@5 and MRR per golden query (DATA-5).

Thin wrapper over ``kbcore.eval_harness`` (the same logic the nightly regression
sentinel replays). Run via `make eval`. Exits non-zero when --gate is given and
hit@5 falls below it (Phase 1 informal gate 0.7; Phase 5 AC 0.8).
"""

from __future__ import annotations

import argparse
import sys

from kbcore.eval_harness import evaluate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", type=float, default=None, help="fail (exit 1) if hit@5 < GATE")
    args = parser.parse_args()

    results, hit_at_5, mrr = evaluate()
    if not results:
        print("golden set is empty or missing; nothing to evaluate")
        return 0
    width = max(len(r.query) for r in results)
    print(f"{'query':<{width}}  rank  status")
    print("-" * (width + 14))
    for r in results:
        rank = r.rank if r.rank is not None else "MISS"
        if r.passed:
            status = "ok"
        else:
            failed = "; ".join(f"{c.name}: {c.detail}" for c in r.criteria if not c.ok)
            status = f"FAIL ({failed})"
        print(f"{r.query:<{width}}  {rank!s:<4}  {status}")
    print("-" * (width + 14))
    print(f"queries: {len(results)}   hit@5: {hit_at_5:.2f}   MRR: {mrr:.2f}")
    if args.gate is not None and hit_at_5 < args.gate:
        print(f"FAIL: hit@5 {hit_at_5:.2f} < gate {args.gate:.2f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

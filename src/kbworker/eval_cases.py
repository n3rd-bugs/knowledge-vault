"""kbworker.eval_cases — per-case eval telemetry keyed by QUERY (review #9a EV-4).

E1's per-case diff ('which golden case slipped one quantum') had no instrument:
the harness's ``CaseResult`` list was consumed for ``eval_chain_resolved`` and
discarded, ``eval_top5_item_ids`` / ``eval_satisfier_ids`` are sorted DISTINCT
sets, and the rerank tally is a whole-replay count. So a +1.6-quanta night had
three indistinguishable decompositions and a -1.7 one five.

Two ADDED meta keys per replay, both keyed by the case's query text (never by
golden.yaml index — E1 rewrites the set and a diff by position would lie):

* ``eval_case_ranks``  — ``{query: rank | null}`` for the PRIMARY replay
  (``null`` = the positive criterion did not match in the top-K; a forbid-only
  case has no rank and reads ``null`` too);
* ``eval_case_rerank`` — ``{query: 'active' | 'fallback' | 'off'}``, the
  rerank participation of THAT case's search; and for the composition replay
  (the satisfier keys' producer, which the primary tally never described)
  ``eval_case_rerank_composition`` plus its rolled-up
  ``eval_rerank_tally_composition``.

Golden queries are not unique (five texts repeat today: the same question asked
with different criteria), so the second and later cases of one query text key
as ``'<query> #2'``, ``'<query> #3'`` … in golden order — the bare text stays
the first case's key, which is what a query-keyed diff wants. Pure functions;
shape-tolerant so a stubbed ``evaluate()`` returning bare tuples costs nothing.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

RERANK_OUTCOMES = ("active", "fallback", "off")


def case_key(query: str, seen: dict[str, Any]) -> str:
    """The key for ``query`` given the keys already taken: bare, then ``' #2'``, …"""
    if query not in seen:
        return query
    n = 2
    while f"{query} #{n}" in seen:
        n += 1
    return f"{query} #{n}"


def per_case_keys(results: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """``{'eval_case_ranks': {...}, 'eval_case_rerank': {...}}`` from CaseResults.

    Reads ``query`` / ``rank`` / ``rerank`` by attribute; an element without a
    string ``query`` (a stub tuple) is skipped, an unknown rerank value reads
    as ``'off'`` (an older harness that never noted one).
    """
    ranks: dict[str, int | None] = {}
    outcomes: dict[str, str] = {}
    for r in results:
        query = getattr(r, "query", None)
        if not isinstance(query, str):
            continue
        key = case_key(query, ranks)
        rank = getattr(r, "rank", None)
        ranks[key] = rank if isinstance(rank, int) and not isinstance(rank, bool) else None
        outcome = getattr(r, "rerank", None)
        outcomes[key] = outcome if outcome in RERANK_OUTCOMES else "off"
    return {"eval_case_ranks": ranks, "eval_case_rerank": outcomes}


def rerank_tally(outcomes: dict[str, str]) -> dict[str, int]:
    """``{'active', 'fallback', 'off'}`` counts over a per-case outcome map."""
    tally = dict.fromkeys(RERANK_OUTCOMES, 0)
    for outcome in outcomes.values():
        tally[outcome if outcome in RERANK_OUTCOMES else "off"] += 1
    return tally

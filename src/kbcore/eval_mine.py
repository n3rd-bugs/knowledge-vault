"""kbcore.eval_mine — usage mining: real search→get confirmations as candidates.

Split from :mod:`kbcore.eval_harness` (mechanical move, review #5 Wave F): the
chain-head resolution pushed that file to the 300-line ratchet, and mining is
its own concern — it reads access_log for GOLDEN-CASE CANDIDATES, while the
harness replays the golden set. ``eval_harness`` re-imports the name (the
established seam — kbworker.tail_steps / kbworker.digest), so ``kb eval mine``
and every other caller keeps reaching it as ``eval_harness.mine_candidates``.
"""

from __future__ import annotations

from kbcore.db import connect

_SEARCH_TOOL_LIKE = "%kb_search%"
_GET_TOOL_LIKE = "%kb_get%"


def mine_candidates(window_minutes: int = 15, limit: int = 50) -> list[dict]:
    """Mine access_log for golden-case candidates: a kb_search hit that the user
    then opened via kb_get within ``window_minutes`` is real evidence that
    (query → item) was a good retrieval. READ-ONLY, parameterized; never writes.

    Returns dicts: {q, expect_item_id, title, confirmations, first_searched,
    last_confirmed}, most-confirmed first. Un-followed searches (no later
    kb_get for the same item inside the window) never appear.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.query, s.item_id, i.title,
                   COUNT(DISTINCT g.id) AS confirmations,
                   MIN(s.ts) AS first_searched,
                   MAX(g.ts) AS last_confirmed
            FROM access_log s
            JOIN access_log g
              ON g.item_id = s.item_id
             AND g.tool LIKE %(get_like)s
             AND g.ts > s.ts
             AND g.ts <= s.ts + make_interval(mins => %(window)s)
            JOIN items i ON i.id = s.item_id
            WHERE s.tool LIKE %(search_like)s
              AND s.query IS NOT NULL AND s.query <> ''
            GROUP BY s.query, s.item_id, i.title
            ORDER BY confirmations DESC, last_confirmed DESC
            LIMIT %(limit)s
            """,
            {
                "get_like": _GET_TOOL_LIKE,
                "search_like": _SEARCH_TOOL_LIKE,
                "window": window_minutes,
                "limit": limit,
            },
        ).fetchall()
    return [
        {
            "q": query,
            "expect_item_id": item_id,
            "title": title,
            "confirmations": confirmations,
            "first_searched": first_searched,
            "last_confirmed": last_confirmed,
        }
        for query, item_id, title, confirmations, first_searched, last_confirmed in rows
    ]

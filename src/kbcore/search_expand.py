"""kbcore.search_expand — WP2 graph-traversal retrieval expansion for search().

After the vector+keyword+RRF fusion, optionally pull each strong hit's graph
neighborhood (items reachable over the typed relation edges — fixes/depends_on/
part_of/contradicts/…) into the result with a hop-decayed score (Zep-style). A
neighbor the keyword/vector legs missed but that sits 1 hop from a strong hit
gets surfaced below the direct hits. Inert unless search() is called with
hops>0 (or KB_GRAPH_EXPAND_HOPS>0).

Split out of search.py to keep that module lean (CLAUDE.md: files < ~300 lines).
Safety: every expanded neighbor is re-checked against the SAME scope/status/
valid-time WHERE the legs applied (:func:`_scoped_ids`), so expansion can never
leak a withheld, non-active, out-of-scope, or as_of-invalid item — by
construction.
"""

from __future__ import annotations

from typing import Any

import psycopg

from kbcore import graph
from kbcore.config import settings

# A neighbor h hops from a seed inherits seed_score * DECAY**h — so a 1-hop
# neighbor sits at half its own seed's score and ranks strictly below it.
GRAPH_DECAY = 0.5
GRAPH_MAX_HOPS = 3  # cap the effective hop count to bound BFS cost


def hops_from_expand(expand: bool | None) -> int | None:
    """Map a surface ``expand`` flag to :func:`search`'s ``hops`` argument.

    None → None (use the configured default); False → 0 (expansion OFF, which the
    config default alone could never express); True → at least one hop, but never
    fewer than the configured default (so expand=true is never NARROWER than the
    server's own default — the pre-fix inversion).
    """
    if expand is None:
        return None
    if not expand:
        return 0
    return max(settings.kb_graph_expand_hops, 1)


def expand_fused(
    conn: psycopg.Connection,
    fused: list[tuple[int, float, str]],
    hops: int,
    where: str,
    params: list[Any],
    seed_cap: int,
) -> list[tuple[int, float, str]]:
    """Merge scope-safe graph neighbors of the top hits into ``fused``.

    ``fused`` is the RAW RRF output ``(item_id, score, snippet)`` (no bonuses yet),
    already sorted score-descending. Seeds are its STRONG hits (score at/above
    ``best_raw * DECAY``); a scored BFS out ``hops`` over item↔item edges scores
    each neighbor by the SEED that reaches it — ``seed_score * DECAY**hop``, max
    over reaching seeds — not by the global best hit (review finding). Each
    neighbor is merged in (keeping the max for an id already present, never
    demoting a direct hit); survivors carry an empty snippet — _to_hits fills it
    from the item body. Scope/status/valid-time walling happens per BFS level
    inside :func:`graph.expand_items_scored` via ``_scoped_ids``.

    Seed selection is score-relative on the RAW scores (bonuses are applied by the
    caller AFTER this merge, so the floor is no longer bonus-inflated — a rank-1
    un-boosted hit can seed again). A hit whose own raw score is below
    ``best_raw * DECAY`` belongs in the boost-eligible CANDIDATE pool, not the seed
    pool: seeding the weak tail was a live regression (real embeddings give every
    item a small vector score, so a genuine neighbor of the top hit lands in the
    tail; because BFS excludes seeds it never received the decay floor) and it
    expanded the noisy neighborhoods of barely-relevant tail hits.
    """
    best = fused[0][1]  # strongest RAW hit (fused is score-descending)
    seed_floor = best * GRAPH_DECAY
    seeds = {item_id: score for item_id, score, _snip in fused if score >= seed_floor}
    if seed_cap is not None and len(seeds) > seed_cap:
        seeds = dict(list(seeds.items())[:seed_cap])
    if not seeds:
        return fused

    def scope_ok(ids: list[int]) -> set[int]:
        return set(_scoped_ids(conn, ids, where, params))

    reached = graph.expand_items_scored(conn, seeds, hops, GRAPH_DECAY, scope_ok)
    if not reached:
        return fused
    index = {item_id: pos for pos, (item_id, _s, _n) in enumerate(fused)}
    out = list(fused)
    for nid, decayed in reached.items():
        if nid in index:  # already a direct hit — keep the higher score
            item_id, score, snippet = out[index[nid]]
            if decayed > score:
                out[index[nid]] = (item_id, decayed, snippet)
        else:
            out.append((nid, decayed, ""))
    out.sort(key=lambda row: row[1], reverse=True)
    return out


def _scoped_ids(
    conn: psycopg.Connection, ids: list[int], where: str, params: list[Any]
) -> list[int]:
    """Subset of ``ids`` satisfying the EXACT search ``where`` (scope safety).

    Re-runs the same fragment the vector/keyword legs used — project/domain/type/
    tags, the status set, public-only scoping, and the as_of valid-time predicate
    — against items joined to projects, plus ``i.id = ANY(...)``. Reusing the
    identical ``where``/``params`` is what guarantees an expanded neighbor can
    never bypass scope, status, or validity.
    """
    rows = conn.execute(
        f"""
        SELECT i.id FROM items i
        LEFT JOIN projects p ON p.name = i.project
        WHERE {where} AND i.id = ANY(%s)
        """,  # noqa: S608 — `where` is built only from constant fragments (search._filters)
        [*params, ids],
    ).fetchall()
    return [r[0] for r in rows]

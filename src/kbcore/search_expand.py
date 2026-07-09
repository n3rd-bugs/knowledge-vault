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

# A neighbor h hops from a seed inherits best_seed_score * DECAY**h — so a 1-hop
# neighbor sits at half the strongest hit's score and ranks below direct hits.
GRAPH_DECAY = 0.5
GRAPH_MAX_HOPS = 3  # cap the effective hop count to bound BFS cost


def expand_fused(
    conn: psycopg.Connection,
    fused: list[tuple[int, float, str]],
    hops: int,
    where: str,
    params: list[Any],
    seed_cap: int,
) -> list[tuple[int, float, str]]:
    """Merge scope-safe graph neighbors of the top hits into ``fused``.

    ``fused`` is the RRF output ``(item_id, score, snippet)``, already sorted
    score-descending. Seeds from its STRONG hits (see below); BFS out ``hops``
    over item↔item edges; keep only neighbors that still pass the search's
    ``where``; add each survivor at ``best_seed_score * DECAY**hop`` (keeping the
    max for an id already present, never demoting a direct hit). Survivors carry
    an empty snippet — _to_hits fills it from the item body.

    Seed selection is score-relative, not a flat top-N. A hit whose own score is
    below ``best * DECAY`` can never outrank the floor a single hop from the top
    hit would give it, so it belongs in the boost-eligible CANDIDATE pool, not
    the seed pool. Seeding the weak tail instead (which a flat top-N does) was a
    live-only regression: real embeddings give every item a small vector score,
    so a genuine neighbor of the top hit lands in the tail, becomes a seed, and —
    because expand_items excludes seeds — never receives the decay floor. It also
    expanded the noisy neighborhoods of barely-relevant tail hits. Restricting
    seeds to hits at/above ``best * DECAY`` keeps real neighbors boost-eligible
    and confines expansion to hits confident enough to vouch for their graph.
    """
    best = fused[0][1]  # strongest hit (fused is score-descending)
    seed_floor = best * GRAPH_DECAY
    seeds = [item_id for item_id, score, _snip in fused if score >= seed_floor][:seed_cap]
    reached = graph.expand_items(conn, seeds, hops)
    if not reached:
        return fused
    survivors = _scoped_ids(conn, list(reached), where, params)
    if not survivors:
        return fused
    index = {item_id: pos for pos, (item_id, _s, _n) in enumerate(fused)}
    out = list(fused)
    for nid in survivors:
        decayed = best * (GRAPH_DECAY ** reached[nid])
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

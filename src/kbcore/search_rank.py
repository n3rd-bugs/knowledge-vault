"""kbcore.search_rank — Wave-3 additive ranking dials (design review #2, KB 16601).

Split out of search.py/search_legs.py (CLAUDE.md: split modules rather than grow
gods, and the size ratchet) — the same reason search_legs and search_expand were
carved off. Every helper here is reached only through a config dial that ships at
its NO-OP default, so today's ranking is byte-identical until an owner flips one:

- ``rare_identifier_tokens`` + ``fuse_rare_id_rows`` — (3) the rare-token
  identifier leg: for a NON-id-dominant query, admit the identifier leg for
  tokens too rare in the corpus to be noise, fused BELOW any real lexical hit
  (kb_id_leg_rare_tokens);
- ``collapse_alternatives`` — (4) rank-time cross-project twin collapse: two
  candidates in the SAME result set joined by a cross-project ``alternative_to``
  edge fold to the higher-scored one, no status flip (kb_collapse_alternatives).
"""

from __future__ import annotations

from kbcore.search_legs import CANDIDATES, RRF_K

# A rare-token identifier row enters fusion at this rank OFFSET: the strongest
# (rank-1) rare row scores 1/(RRF_K + OFFSET + 1) which, with OFFSET = CANDIDATES,
# is strictly below 1/(RRF_K + CANDIDATES) — the weakest possible single-leg
# lexical hit. So a rare-token-ONLY candidate can never outrank a real lexical hit.
RARE_ID_RANK_OFFSET = CANDIDATES


def rare_identifier_tokens(conn, tokens: list[str], max_df: int) -> list[str]:
    """Subset of ``tokens`` whose corpus document-frequency is <= ``max_df``.

    df is counted over the chunks ``tsv`` GIN index (``c.tsv @@
    websearch_to_tsquery('simple', token)``) — NOT the identifier regex — so a
    ubiquitous acronym (high df) is excluded while a rare part number (low/zero
    df, including symbols the tsv never lexed as their own lexeme) is admitted.
    Reuses the exact df primitive the keyword leg's OR fallback already uses
    (search_legs._or_coverage_rows), so it is index-backed and injection-safe.
    Order-preserving over ``tokens``.
    """
    if not tokens:
        return []
    rows = conn.execute(
        """
        SELECT t, (SELECT count(DISTINCT c.item_id) FROM chunks c
                   WHERE c.tsv @@ websearch_to_tsquery('simple', t)) AS df
        FROM unnest(%s::text[]) t
        """,
        (tokens,),
    ).fetchall()
    df = {row[0]: row[1] for row in rows}
    return [t for t in tokens if df.get(t, 0) <= max_df]


def fuse_rare_id_rows(
    fused: list[tuple[int, float, str]], rare_rows: list[tuple[int, str]]
) -> list[tuple[int, float, str]]:
    """Fold rare-token identifier rows into an already-fused list at a rank OFFSET.

    Each rare row at position ``rank`` contributes ``1/(RRF_K + RARE_ID_RANK_OFFSET
    + rank)`` — ADDED to a candidate already present (a small nudge) or introduced
    as a NEW candidate (snippet = the matched chunk text). The offset keeps every
    rare-only candidate strictly below the weakest real lexical hit. Empty
    ``rare_rows`` returns ``fused`` unchanged (byte-identical no-op).
    """
    if not rare_rows:
        return fused
    scores = {item_id: s for item_id, s, _snip in fused}
    snippets = {item_id: snip for item_id, _s, snip in fused}
    seen: set[int] = set()
    for rank, (item_id, text) in enumerate(rare_rows, start=1):
        if item_id in seen:
            continue  # best (highest) rare rank per item
        seen.add(item_id)
        scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (RRF_K + RARE_ID_RANK_OFFSET + rank)
        if not snippets.get(item_id):
            snippets[item_id] = text
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [(item_id, s, snippets.get(item_id, "")) for item_id, s in ranked]


def collapse_alternatives(
    conn, fused: list[tuple[int, float, str]]
) -> tuple[list[tuple[int, float, str]], dict[int, list[int]]]:
    """Collapse cross-project ``alternative_to`` twins WITHIN one result set.

    When two candidates in ``fused`` are joined by an ``alternative_to`` edge and
    live in DIFFERENT projects, keep the higher-scored candidate at the MAX of
    the pair's scores (dedupe, never boost) and DROP the loser. No status flip — the
    loser row is untouched in the DB (Invariant 5 stays intact); this is a
    rank-time render decision only. Returns ``(pruned_fused, {winner_id:
    [collapsed_loser_ids]})``; the map lets the caller record the collapse.

    A pure no-op — returns ``fused`` unchanged and an empty map — when no
    qualifying edge exists, so the OFF path is byte-identical. Pairs are resolved
    strongest-first (max pair score, id tie-break) so the collapse is
    deterministic; an item already collapsed into a winner is not re-collapsed.
    """
    if len(fused) < 2:
        return fused, {}
    ids = [item_id for item_id, _s, _snip in fused]
    proj = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT id, project FROM items WHERE id = ANY(%s)", (ids,)
        ).fetchall()
    }
    edges = conn.execute(
        """
        SELECT src_id, dst_id FROM relations
        WHERE rel = 'alternative_to' AND src_kind = 'item' AND dst_kind = 'item'
          AND src_id = ANY(%s) AND dst_id = ANY(%s)
        """,
        (ids, ids),
    ).fetchall()
    scores = {item_id: s for item_id, s, _snip in fused}
    pairs = sorted(
        ((a, b) for a, b in edges if a != b and proj.get(a) != proj.get(b)),
        key=lambda ab: max(scores[ab[0]], scores[ab[1]]),
        reverse=True,
    )
    collapsed: dict[int, int] = {}  # loser id -> winner id
    folded: dict[int, list[int]] = {}
    for a, b in pairs:
        if a in collapsed or b in collapsed:
            continue
        winner, loser = (a, b) if (scores[a], a) >= (scores[b], b) else (b, a)
        # MAX, not SUM (validation-day-one finding, 2026-08-14): summing let two
        # tangential twins at ranks ~6-7 COMBINE their way into the top-5 and
        # push out the query's actual answer (the WireGuard golden case caught
        # it within the hour). A twin pair is one piece of knowledge captured
        # twice, not two independent votes: deduplication removes the redundant
        # slot, it must never manufacture rank the pair never earned alone.
        scores[winner] = max(scores[winner], scores[loser])
        collapsed[loser] = winner
        folded.setdefault(winner, []).append(loser)
    if not collapsed:
        return fused, {}
    pruned = [
        (item_id, scores[item_id], snip) for item_id, _s, snip in fused if item_id not in collapsed
    ]
    pruned.sort(key=lambda row: row[1], reverse=True)
    return pruned, folded

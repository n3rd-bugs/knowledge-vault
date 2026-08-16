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
  edge fold to the higher-scored one, no status flip (kb_collapse_alternatives);
- ``rerank_evidence`` — the reranker's optional chunk-evidence documents
  (kb_rerank_use_chunk), moved here from search.py under the same ratchet.
"""

from __future__ import annotations

from kbcore.config import settings
from kbcore.search_legs import CANDIDATES, RRF_K

# A rare-token identifier row enters fusion at this rank OFFSET: the strongest
# (rank-1) rare row scores 1/(RRF_K + OFFSET + 1) which, with OFFSET = CANDIDATES,
# is strictly below 1/(RRF_K + CANDIDATES) — the weakest possible single-leg
# lexical hit. So a rare-token-ONLY candidate can never outrank a real lexical hit.
# That bound only holds if the SAME token cannot then re-enter as an exact-id
# bonus on top of the offset (review #4 P1) — see exact_id_match_counts.
RARE_ID_RANK_OFFSET = CANDIDATES

# Collapse ACCESS-PARITY marker (review #4 P2, coordinates with review_promote):
# search._log_access stamps a folded loser's row with the search tool name PLUS
# this suffix ('mcp.kb_search' -> 'mcp.kb_search+collapsed'). Every existing
# never-recalled consumer (expiry/archive/apply-rails) keys on row EXISTENCE, so
# parity rows still protect the loser there; counting surfaces that must not be
# inflatable by folds (the promote-candidate queue) filter the suffix out. This
# convention is owned end to end by this package: written only by search.py,
# filtered only by review_promote.py.
COLLAPSE_PARITY_SUFFIX = "+collapsed"

# Trust tie-break window for collapse winner selection (review #4 P2). Sized to
# the pipeline's own near-tie quantum: every sanctioned tie-break nudge is at or
# below ~one rank-1 RRF increment (1/(RRF_K+1) ≈ 0.0164), and the exact-id
# bonus per token is exactly half of it. Post-rerank scores are the fused-score
# MULTISET reassigned (kbcore.rerank), so adjacent-slot gaps stay RRF-sized
# (~1e-4..3e-4): half a rank-1 increment (~0.0082) spans dozens of slots — a
# pair inside it is indistinguishable by rank ("near-tie", covering the
# observed +0.001 verified-drop with margin), while a pair beyond it differs by
# more than any single nudge could produce ("clearly outscored").
COLLAPSE_TRUST_EPSILON = 1.0 / (2 * (RRF_K + 1))


def rerank_evidence(fused: list[tuple[int, float, str]]) -> dict[int, str] | None:
    """Best-matching CHUNK text per candidate for the reranker, or None when OFF.

    The fusion step already collapsed each item to the chunk that actually
    matched the query (``_rrf_fuse`` keeps the best chunk's text as the third
    tuple element), and that text was then DISCARDED: kbcore.rerank re-reads a
    body PREFIX from ``items``, so the reranker never sees the matching passage
    of a long item. Handing it that passage is a plausible retrieval win and a
    plausible loss (less surrounding context), so it is gated on
    ``kb_rerank_use_chunk`` — default False, awaiting the owner's A/B on the real
    corpus (config.py). With the flag OFF this returns None and the reranker's
    inputs stay BYTE-IDENTICAL to the pre-flag behaviour (regression-pinned in
    tests/test_eval_plumbing.py).

    Candidates with no chunk text (pure graph neighbours, identifier title
    matches) are omitted and keep the body-prefix document.
    """
    if not settings.kb_rerank_use_chunk:
        return None
    return {item_id: snippet for item_id, _score, snippet in fused if snippet}


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
) -> tuple[list[tuple[int, float, str]], set[int]]:
    """Fold rare-token identifier rows into an already-fused list at a rank OFFSET.

    Each rare row at position ``rank`` contributes ``1/(RRF_K + RARE_ID_RANK_OFFSET
    + rank)`` — ADDED to a candidate already present (a small nudge) or introduced
    as a NEW candidate (snippet = the matched chunk text). The offset keeps every
    rare-only candidate strictly below the weakest real lexical hit. Empty
    ``rare_rows`` returns ``fused`` unchanged (byte-identical no-op).

    Returns ``(ranked, introduced)`` where ``introduced`` is the PROVENANCE the
    caller needs (review #4 P1): the ids that exist in the result ONLY because
    of the rare leg. Those must be exempted from the exact-identifier bonus for
    the admitting tokens — the same token both admitting a candidate AND
    bonusing it composes above the rank offset and voids the guarantee above. A
    candidate the legs already found (merely nudged here) is NOT rare-only and
    keeps every bonus.
    """
    if not rare_rows:
        return fused, set()
    scores = {item_id: s for item_id, s, _snip in fused}
    snippets = {item_id: snip for item_id, _s, snip in fused}
    existing = set(scores)
    seen: set[int] = set()
    for rank, (item_id, text) in enumerate(rare_rows, start=1):
        if item_id in seen:
            continue  # best (highest) rare rank per item
        seen.add(item_id)
        scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (RRF_K + RARE_ID_RANK_OFFSET + rank)
        if not snippets.get(item_id):
            snippets[item_id] = text
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [(item_id, s, snippets.get(item_id, "")) for item_id, s in ranked], seen - existing


def exact_id_match_counts(
    conn,
    ids: list[int],
    tokens: list[str],
    exempt_ids: set[int] | frozenset[int] = frozenset(),
    exempt_tokens: set[str] | frozenset[str] = frozenset(),
) -> dict[int, int]:
    """Distinct-token whole-word match counts for the exact-identifier bonus.

    Moved out of search._apply_bonuses so the rare-leg EXEMPTION lives next to
    the never-outrank guarantee it protects (review #4 P1): a candidate in
    ``exempt_ids`` (introduced ONLY by the rare-id leg) earns NO match credit
    for a token in ``exempt_tokens`` (the tokens that admitted it) — that token
    already paid the candidate its bounded rank-offset contribution, and
    counting it again would lift a rare-only candidate above real lexical hits.
    Matches for tokens OUTSIDE ``exempt_tokens`` still count for everyone:
    independent evidence stays rewarded. With the default empty exemptions the
    arithmetic is byte-identical to the pre-split loop (id-dominant path
    untouched — the rare leg never runs there).
    """
    matches: dict[int, int] = {}
    for token in tokens:
        # \m..\M are Postgres word boundaries; identifier tokens are
        # [A-Za-z0-9_] so no regex metachar escaping is needed.
        pattern = rf"\m{token}\M"
        skip = exempt_ids if token in exempt_tokens else frozenset()
        for (item_id,) in conn.execute(
            """
            SELECT i.id FROM items i
            WHERE i.id = ANY(%(ids)s) AND (
                i.title ~* %(pat)s
                OR EXISTS (SELECT 1 FROM chunks c WHERE c.item_id = i.id AND c.text ~* %(pat)s)
            )
            """,
            {"ids": ids, "pat": pattern},
        ).fetchall():
            if item_id in skip:
                continue
            matches[item_id] = matches.get(item_id, 0) + 1
    return matches


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

    Winner selection prefers the VERIFIED twin inside COLLAPSE_TRUST_EPSILON
    (review #4 P2): score alone let an unverified twin at +0.001 drop its
    verified sibling. The kept score is still the pair MAX either way — the
    tie-break decides which item SURVIVES, never what it scores — and a clearly
    outscored verified twin (gap > epsilon) still loses.
    """
    if len(fused) < 2:
        return fused, {}
    ids = [item_id for item_id, _s, _snip in fused]
    proj: dict[int, str] = {}
    verified: dict[int, bool] = {}
    for row in conn.execute(
        # NULL trust = verified: parity with _to_hits/_apply_bonuses (search.py).
        "SELECT i.id, i.project, (s.trust IS NULL OR s.trust = 'verified') "
        "FROM items i LEFT JOIN sources s ON s.id = i.source_id WHERE i.id = ANY(%s)",
        (ids,),
    ).fetchall():
        proj[row[0]] = row[1]
        verified[row[0]] = bool(row[2])
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
        if (
            verified.get(loser, True)
            and not verified.get(winner, True)
            and scores[winner] - scores[loser] <= COLLAPSE_TRUST_EPSILON
        ):
            winner, loser = loser, winner  # near-tie: the verified twin survives
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

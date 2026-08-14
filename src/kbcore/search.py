"""kbcore.search — hybrid search fused with RRF (PLAN.md §5.4).

Three candidate legs (kbcore.search_legs): vector cosine, keyword
``websearch_to_tsquery('simple', q)`` with an OR fallback, and a word-boundary
identifier leg. Fused with RRF (Σ 1/(60+rank)), collapsed to items (best chunk
wins). boost_project multiplies a candidate's fused score by BOOST_FACTOR
(rank-preserving: it breaks near-ties toward the boosted project but can never
lift a weak local hit over a strong cross-project answer); exact-identifier
matches add a small additive bonus capped at ~one rank-1 RRF increment.
Verified >= unverified at equal score.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from kbcore import rerank as rerank_mod
from kbcore import scope, search_expand, search_rank
from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.ingest import _as_utc
from kbcore.models import CodeRef, SearchHit
from kbcore.search_legs import (
    MAX_ID_TOKENS,
    RRF_K,
    _id_dominant,
    _identifier_leg,
    _identifier_tokens,
    _keyword_leg,
    _rrf_fuse,
    _vector_leg,
)

# Project boost (rank-preserving, scale-free): a boosted candidate's fused
# score is MULTIPLIED by this factor, flipping only near-ties (score ratio <
# factor). The old flat +0.05 exceeded any raw RRF score, so every boosted tail
# hit categorically buried cross-project rank-1 answers — the exact hits the KB
# exists to surface, since agents always pass boost_project=<current project>.
# Max boost delta on a 3-leg rank-1 (~0.049) is ~0.012 < one rank-1 increment.
BOOST_FACTOR = 1.25
# Exact-identifier bonus: small additive per whole-word-matched token, TOTAL
# capped at ~one rank-1 RRF increment regardless of token count — breaks ties
# toward the exact symbol without flipping strong vs weak. (Candidate
# GENERATION for symbols is the identifier leg's job; this only nudges.)
EXACT_ID_BONUS = 1.0 / (2 * (RRF_K + 1))  # ~0.0082 per matched token
EXACT_ID_BONUS_CAP = 1.0 / (RRF_K + 1)  # ~0.0164 total — one rank-1 increment
SNIPPET_CHARS = 300


def _filters(
    project: str | None,
    domain: str | None,
    type: str | None,
    tags: list[str] | None,
    statuses: list[str],
    as_of: datetime | None = None,
) -> tuple[str, list[Any]]:
    # SAFETY: the returned `where` string is interpolated into SQL, so it must
    # only ever be assembled from the constant fragments below — every user
    # value travels in `params` (CLAUDE.md: parameterized SQL only).
    where = ["i.status = ANY(%s)"]
    params: list[Any] = [statuses]
    if project:
        where.append("i.project = %s")
        params.append(project)
    if domain:
        where.append("p.domain = %s")
        params.append(domain)
    if type:
        where.append("i.type = %s")
        params.append(type)
    if tags:
        where.append("i.tags @> %s")
        params.append(tags)
    if as_of is not None:
        # Valid-time as_of (WP1): keep only facts whose interval covers T.
        # Half-open (valid_to exclusive); NULL bounds are unbounded. All legs
        # JOIN items i and inherit this, so they stay consistent.
        where.append(
            "(i.valid_from IS NULL OR i.valid_from <= %s)"
            " AND (i.valid_to IS NULL OR i.valid_to > %s)"
        )
        params.extend([as_of, as_of])
    return " AND ".join(where), params


def search(
    query: str,
    project: str | None = None,
    domain: str | None = None,
    type: str | None = None,
    tags: list[str] | None = None,
    boost_project: str | None = None,
    include_superseded: bool = False,
    include_archived: bool = False,
    include_duplicate: bool = False,
    include_pending: bool = False,
    as_of: datetime | None = None,
    hops: int | None = None,
    k: int = 8,
    tool: str | None = "search",
) -> list[SearchHit]:
    """Hybrid search over all projects by default (cross-domain reuse is the point).

    as_of (WP1 valid-time): when set, restrict to facts whose validity interval
    covers that instant; unset (default) applies no valid-time filter. A naive
    as_of is normalized to UTC at this read point (Invariant 7 symmetry with
    ingest's write-point coercion).

    hops (WP2 graph expansion): pull each strong hit's n-hop neighborhood in,
    scored by the seed that reaches it (hop-decayed). None (default) uses
    KB_GRAPH_EXPAND_HOPS; explicit 0 forces expansion OFF; >0 overrides — capped at
    GRAPH_MAX_HOPS so a misconfigured value can't explode the BFS.

    include_pending (A11-3 finding 6): 'pending' (quarantined non-owner web saves)
    is ABSENT from the default status set, so a forged web contribution is NEVER an
    indirect prompt-injection channel into an agent session before review. This flag
    surfaces pending items ONLY for the OWNER (an owner-side review convenience) —
    a grantee passing it is a no-op, because the same quarantine bytes never reach
    a non-owner even by request (owner-gated at this single chokepoint).
    """
    if hops is None:
        hops = settings.kb_graph_expand_hops
    hops = min(max(hops, 0), search_expand.GRAPH_MAX_HOPS)
    as_of = _as_utc(as_of)

    statuses = ["active"]
    if include_superseded:
        statuses.append("superseded")
    if include_archived:
        statuses.append("archived")
    if include_duplicate:
        # Inv 5: marked-duplicate items stay retrievable WITH a flag. Default off.
        statuses.append("duplicate")
    if include_pending and scope.current().is_owner:
        statuses.append("pending")

    where, params = _filters(project, domain, type, tags, statuses, as_of)
    # Visibility wall (public-only instance AND/OR grantee grants): restrict all
    # legs to the projects this request may read. Because expand_fused/_scoped_ids
    # re-run this EXACT where+params per BFS level, graph expansion inherits the
    # same wall — a withheld item is neither surfaced nor used as a relay hop.
    vis_frag, vis_params = scope.project_predicate("i.project")
    if vis_frag:
        where = f"{where} AND {vis_frag}"
        params = [*params, *vis_params]

    id_tokens = _identifier_tokens(query)[:MAX_ID_TOKENS]
    dominant = _id_dominant(query, id_tokens)
    with connect() as conn:
        vec_rows = _vector_leg(conn, query, where, params)
        kw_rows = _keyword_leg(conn, query, where, params)
        # The identifier leg only fires for identifier-DOMINANT queries (bare
        # symbols, part numbers) — for prose that merely mentions an acronym it
        # would inject recency-ranked mentions as noise (search_legs).
        id_rows = _identifier_leg(conn, id_tokens, where, params) if dominant else []
        fused = _rrf_fuse(vec_rows, kw_rows, id_rows)  # RAW RRF, no bonuses yet
        # (3) Rare-token id leg (KB 16601): for a NON-dominant query, admit the id
        # leg for tokens too rare to be noise, fused below any real hit (search_rank).
        if settings.kb_id_leg_rare_tokens and id_tokens and not dominant:
            rare = search_rank.rare_identifier_tokens(
                conn, id_tokens, settings.kb_id_leg_rare_max_df
            )
            if rare:
                fused = search_rank.fuse_rare_id_rows(
                    fused, _identifier_leg(conn, rare, where, params)
                )
        expansion_origin: dict[int, int] = {}
        if hops > 0 and fused:
            # expand_fused walls every neighbor against the SAME where per BFS
            # level, so no scope/status/valid-time leak (search_expand). It also
            # returns each pure neighbor's vouching SEED so bonuses cannot lift a
            # neighbor above that seed (clamp in _apply_bonuses).
            fused, expansion_origin = search_expand.expand_fused(
                conn, fused, hops, where, params, max(k, 10)
            )
        # Bonuses apply AFTER the merge so they lift any candidate (direct OR
        # neighbor) and the seed_floor stays on raw scores (un-boosted rank-1 can
        # seed; finding). Reuse the legs' connection — no extra connect (finding).
        fused = _apply_bonuses(conn, fused, boost_project, id_tokens, expansion_origin)
        # Optional cross-encoder rerank of the top window (kb_rerank_url set):
        # reorders only, fail-open, scores remapped so the order survives the
        # _to_hits re-sort (see kbcore.rerank). Reuses the legs' connection.
        if settings.kb_rerank_url and fused:
            fused = rerank_mod.rerank_fused(
                conn,
                query,
                fused,
                settings.kb_rerank_top_n,
                evidence=_rerank_evidence(fused),
            )
        # (4) Rank-time twin collapse: fold a cross-project 'alternative_to' twin.
        # The {winner: [losers]} map is KEPT (review #3, KB 19663), not discarded.
        folded: dict[int, list[int]] = {}
        if settings.kb_collapse_alternatives and fused:
            fused, folded = search_rank.collapse_alternatives(conn, fused)
    # Materialize ALL fused candidates before truncating: the verified >=
    # unverified tie-break must apply globally, not only inside the top-k
    # window (review finding — an unverified item tied at the k boundary used
    # to displace a verified one).
    hits = _to_hits(fused)[:k]
    for h in hits:  # stamp the collapse map on the kept hit (additive; [] when off)
        h.collapsed = folded.get(h.item_id, [])
    # tool=None → skip access logging (the eval harness replays golden queries and
    # must not stamp items 'recently used', which would pin them against archiving).
    if tool is not None:
        _log_access(hits, tool, query)
    return hits


def _rerank_evidence(fused: list[tuple[int, float, str]]) -> dict[int, str] | None:
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


def _apply_bonuses(
    conn,
    fused: list[tuple[int, float, str]],
    boost_project: str | None,
    id_tokens: list[str],
    expansion_origin: dict[int, int],
) -> list[tuple[int, float, str]]:
    """Return ``fused`` re-scored with the project boost + exact-identifier bonus.

    Applied to every candidate (direct hits AND expanded neighbors) after the
    merge, reusing the legs' ``conn`` (no extra connect — finding). Arithmetic
    (rank-preserving by design): a boosted-project candidate's score is
    multiplied by BOOST_FACTOR; each whole-word identifier match adds
    EXACT_ID_BONUS, with the TOTAL capped at EXACT_ID_BONUS_CAP (~one rank-1
    RRF increment) no matter how many tokens match. Neither can lift a weak
    tail hit over a strong cross-project rank-1 hit. A pure expansion neighbor
    is then CLAMPED strictly below its vouching seed's POST-bonus score: a
    boosted+bonused query-blind neighbor could otherwise still edge past a
    low-scoring seed, breaking the documented 'never outranks its seed'
    guarantee (finding). ``expansion_origin`` maps each pure-neighbor id → the
    direct-hit seed that vouches for it (threaded out of search_expand).
    """
    if not fused:
        return fused
    scores = {item_id: score for item_id, score, _snip in fused}
    ids = list(scores)
    trust_factor = settings.kb_trust_factor
    if not boost_project and not id_tokens and trust_factor == 1.0:
        return fused
    # (2) Trust dial (KB 16601): VERIFIED candidates nudged up multiplicatively,
    # sized below one rank-1 RRF increment so it only breaks near-ties. NULL trust =
    # verified (parity with _to_hits); 1.0 default skips the query.
    if trust_factor != 1.0:
        for (item_id,) in conn.execute(
            "SELECT i.id FROM items i LEFT JOIN sources s ON s.id = i.source_id "
            "WHERE i.id = ANY(%s) AND (s.trust IS NULL OR s.trust = 'verified')",
            (ids,),
        ).fetchall():
            scores[item_id] *= trust_factor
    if boost_project:
        for (item_id,) in conn.execute(
            "SELECT id FROM items WHERE id = ANY(%s) AND project = %s",
            (ids, boost_project),
        ).fetchall():
            scores[item_id] *= BOOST_FACTOR
    matches: dict[int, int] = {}
    for token in id_tokens:
        # \m..\M are Postgres word boundaries; identifier tokens are
        # [A-Za-z0-9_] so no regex metachar escaping is needed.
        pattern = rf"\m{token}\M"
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
            matches[item_id] = matches.get(item_id, 0) + 1
    for item_id, n in matches.items():
        scores[item_id] += min(n * EXACT_ID_BONUS, EXACT_ID_BONUS_CAP)
    # Restore the seed guarantee post-bonus: a neighbor surfaced ONLY by expansion
    # (empty-snippet, query-blind) must rank strictly below the direct hit that
    # vouched for it. The seed is always a direct hit, hence present in `scores`.
    for nid, seed_id in expansion_origin.items():
        seed_score = scores.get(seed_id)
        if seed_score is not None and nid in scores and scores[nid] >= seed_score:
            scores[nid] = math.nextafter(seed_score, float("-inf"))
    return [(item_id, scores[item_id], snippet) for item_id, _score, snippet in fused]


def _to_hits(top: list[tuple[int, float, str]]) -> list[SearchHit]:
    if not top:
        return []
    by_id = {item_id: (score, snippet) for item_id, score, snippet in top}
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT i.id, i.title, i.type, i.project, p.domain, i.status,
                   s.trust, i.tags, i.created_at, s.uri, i.meta, i.superseded_by,
                   i.valid_from, i.valid_to, i.body
            FROM items i
            LEFT JOIN projects p ON p.name = i.project
            LEFT JOIN sources s ON s.id = i.source_id
            WHERE i.id = ANY(%s)
            """,
            (list(by_id),),
        ).fetchall()
        # Batch-mask superseded_by pointers into withheld projects (one scoped lookup)
        # — parity with get_item/timeline; owner sees everything. sup_by is column 11.
        readable_sup = scope.readable_item_ids(conn, [r[11] for r in rows if r[11] is not None])
    hits = []
    for (
        id_,
        title,
        type_,
        proj,
        dom,
        status,
        trust,
        tags,
        created,
        uri,
        meta,
        sup_by,
        valid_from,
        valid_to,
        body,
    ) in rows:
        score, snippet = by_id[id_]
        if sup_by is not None and sup_by not in readable_sup:
            sup_by = None  # withheld successor id must not leak via the hit scalar
        # An expanded graph neighbor or identifier-leg title match has no chunk
        # snippet (empty string); fall back to the item body prefix so it still
        # renders a preview (WP2).
        snippet = snippet or body or ""
        code_ref = None
        if isinstance(meta, dict) and meta.get("code_ref"):
            code_ref = CodeRef(**meta["code_ref"])
        hits.append(
            SearchHit(
                item_id=id_,
                title=title,
                type=type_,
                project=proj,
                domain=dom,
                status=status,
                trust=trust or "verified",
                tags=tags or [],
                created_at=created,
                snippet=snippet[:SNIPPET_CHARS],
                source_uri=uri or None,
                code_ref=code_ref,
                superseded_by=sup_by,
                valid_from=valid_from,
                valid_to=valid_to,
                score=score,
            )
        )
    # verified >= unverified at equal score, then recency for stability
    hits.sort(key=lambda h: (h.score, h.trust == "verified", h.created_at), reverse=True)
    return hits


def _log_access(hits: list[SearchHit], tool: str, query: str) -> None:
    if not hits:
        return
    p = scope.current()
    pname = None if p.is_owner else p.name  # audit: grantee name; NULL for owner
    with transaction() as conn:
        conn.cursor().executemany(
            "INSERT INTO access_log (item_id, tool, query, principal) VALUES (%s, %s, %s, %s)",
            # A hit's collapsed twins were RECALLED too — folded into the winner, not
            # absent — so the LOSER is logged at the same chokepoint (KB 19663): being
            # folded must never route an item into 'never recalled' expiry/archive paths.
            [(i, tool, query, pname) for h in hits for i in (h.item_id, *h.collapsed)],
        )

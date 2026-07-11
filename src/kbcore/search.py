"""kbcore.search — hybrid vector+keyword search fused with RRF (PLAN.md §5.4).

Vector leg: cosine over current-model chunks. Keyword leg:
``websearch_to_tsquery('simple', q)`` — 'simple' keeps Urdu and identifiers
like TPS55288 un-stemmed (DATA-6/V7). Fused with RRF (Σ 1/(60+rank)), collapsed
to items (best chunk wins), +0.05 boost_project bonus, verified ≥ unverified at
equal score.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any

from kbcore import embed as embed_mod
from kbcore import scope, search_expand
from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.ingest import _as_utc
from kbcore.models import CodeRef, SearchHit

RRF_K = 60
CANDIDATES = 40
BOOST = 0.05
# Exact-identifier bonus (§5.4 tuning): when the query carries an identifier
# token (see _is_identifier — a digit, an underscore, an all-caps acronym, or an
# internal capital: drv_spi_write, TPS55288, SPI, PageNumber) that appears as a
# WHOLE WORD in a candidate's title or body, add this. It rescues exact-symbol
# lookups whose extra common word ("drv_spi_write gotcha") dilutes the vector
# match under RRF and AND-fails the keyword leg. Principled: helps every
# exact-symbol query, and is a genuine no-op for plain-word searches — a
# sentence-initial capital ("How", "Postgres") is NOT treated as an identifier.
EXACT_ID_BONUS = 0.05
# Cap identifier tokens scanned in _apply_bonuses: each is one unindexed word-regex
# over the candidate items' chunks, and the id set now spans direct hits + expanded
# neighbors — an unbounded token count (a 500-token query) amplifies into 500
# chunk-scans per search. 8 matches the 'identifier rescue' intent (review finding).
MAX_ID_TOKENS = 8
SNIPPET_CHARS = 300

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _is_identifier(t: str) -> bool:
    """True for real code identifiers, not ordinary capitalized words.

    Qualifies: a digit or underscore anywhere; an all-caps acronym (len ≥ 2,
    e.g. SPI, OBD); an internal capital (camelCase/PascalCase, e.g. PageNumber).
    Excludes sentence-initial capitals and single letters (How, Postgres, A, I).
    """
    if any(c.isdigit() for c in t) or "_" in t:
        return True
    if len(t) >= 2 and t.isupper():
        return True
    return any(c.isupper() for c in t[1:])  # capital after the first char


def _identifier_tokens(query: str) -> list[str]:
    """Identifier-like query tokens (deduped, order-preserving)."""
    return list(dict.fromkeys(t for t in _WORD_RE.findall(query) if _is_identifier(t)))


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
        # Half-open (valid_to exclusive); NULL bounds are unbounded. Both legs
        # JOIN items i and inherit this, so vector + keyword stay consistent.
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

    where, params = _filters(project, domain, type, tags, statuses, as_of)
    # Public-only instance: both legs JOIN projects p, so restrict to public ones.
    if scope.public_only():
        where = f"{where} AND p.public IS TRUE"

    with connect() as conn:
        vec_rows = _vector_leg(conn, query, where, params)
        kw_rows = _keyword_leg(conn, query, where, params)
        fused = _rrf_fuse(vec_rows, kw_rows)  # RAW RRF, no bonuses yet
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
        fused = _apply_bonuses(conn, fused, boost_project, query, expansion_origin)
    # Materialize ALL fused candidates before truncating: the verified ≥
    # unverified tie-break must apply globally, not only inside the top-k
    # window (review finding — an unverified item tied at the k boundary used
    # to displace a verified one).
    hits = _to_hits(fused)[:k]
    # tool=None → skip access logging (the eval harness replays golden queries and
    # must not stamp items 'recently used', which would pin them against archiving).
    if tool is not None:
        _log_access(hits, tool, query)
    return hits


def _vector_leg(conn, query: str, where: str, params: list[Any]) -> list[tuple[int, str]]:
    qvec = json.dumps(embed_mod.embed_one(query), separators=(",", ":"))
    rows = conn.execute(
        f"""
        SELECT c.item_id, c.text
        FROM chunks c
        JOIN items i ON i.id = c.item_id
        LEFT JOIN projects p ON p.name = i.project
        WHERE c.embed_model = %s AND c.embedding IS NOT NULL AND {where}
        ORDER BY c.embedding <=> %s::vector
        LIMIT {CANDIDATES}
        """,
        [settings.kb_embed_model, *params, qvec],
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _keyword_leg(conn, query: str, where: str, params: list[Any]) -> list[tuple[int, str]]:
    rows = conn.execute(
        f"""
        SELECT c.item_id, c.text
        FROM chunks c
        JOIN items i ON i.id = c.item_id
        LEFT JOIN projects p ON p.name = i.project,
        websearch_to_tsquery('simple', %s) q
        WHERE c.tsv @@ q AND {where}
        ORDER BY ts_rank_cd(c.tsv, q) DESC
        LIMIT {CANDIDATES}
        """,
        [query, *params],
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _rrf_fuse(
    vec_rows: list[tuple[int, str]],
    kw_rows: list[tuple[int, str]],
) -> list[tuple[int, float, str]]:
    """RAW RRF over both legs, collapsed to items (best/earliest chunk is snippet).

    No bonuses here: boost_project / exact-identifier are applied by the caller
    AFTER graph expansion (search_expand seeds off these RAW scores).
    """
    scores: dict[int, float] = {}
    snippets: dict[int, str] = {}
    for leg in (vec_rows, kw_rows):
        seen: set[int] = set()
        for rank, (item_id, text) in enumerate(leg, start=1):
            if item_id in seen:
                continue  # best chunk per item per leg
            seen.add(item_id)
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (RRF_K + rank)
            snippets.setdefault(item_id, text)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [(item_id, score, snippets[item_id]) for item_id, score in ranked]


def _apply_bonuses(
    conn,
    fused: list[tuple[int, float, str]],
    boost_project: str | None,
    query: str,
    expansion_origin: dict[int, int],
) -> list[tuple[int, float, str]]:
    """Return ``fused`` re-scored with the project boost + exact-identifier bonus.

    Applied to every candidate (direct hits AND expanded neighbors) after the merge,
    reusing the legs' ``conn`` (no extra connect — finding). A pure expansion
    neighbor is then CLAMPED strictly below its vouching seed's POST-bonus score:
    BOOST/EXACT_ID_BONUS (0.05) each exceed the max raw RRF (~0.0328), so an
    un-clamped bonus on a query-blind neighbor could categorically outrank its own
    seed and genuine direct hits, breaking the documented 'never outranks its seed'
    guarantee (finding). ``expansion_origin`` maps each pure-neighbor id → the
    direct-hit seed that vouches for it (threaded out of search_expand).
    """
    if not fused:
        return fused
    scores = {item_id: score for item_id, score, _snip in fused}
    ids = list(scores)
    tokens = _identifier_tokens(query)[:MAX_ID_TOKENS]
    if not boost_project and not tokens:
        return fused
    if boost_project:
        for (item_id,) in conn.execute(
            "SELECT id FROM items WHERE id = ANY(%s) AND project = %s",
            (ids, boost_project),
        ).fetchall():
            scores[item_id] += BOOST
    for token in tokens:
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
            scores[item_id] += EXACT_ID_BONUS
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
        # An expanded graph neighbor has no matching chunk (empty snippet); fall
        # back to the item body prefix so it still renders a preview (WP2).
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
    # verified ≥ unverified at equal score, then recency for stability
    hits.sort(key=lambda h: (h.score, h.trust == "verified", h.created_at), reverse=True)
    return hits


def _log_access(hits: list[SearchHit], tool: str, query: str) -> None:
    if not hits:
        return
    with transaction() as conn:
        conn.cursor().executemany(
            "INSERT INTO access_log (item_id, tool, query) VALUES (%s, %s, %s)",
            [(h.item_id, tool, query) for h in hits],
        )

"""kbcore.search — hybrid vector+keyword search fused with RRF (PLAN.md §5.4).

Vector leg: cosine over current-model chunks. Keyword leg:
``websearch_to_tsquery('simple', q)`` — 'simple' keeps Urdu and identifiers
like TPS55288 un-stemmed (DATA-6/V7). Fused with RRF (Σ 1/(60+rank)), collapsed
to items (best chunk wins), +0.05 boost_project bonus, verified ≥ unverified at
equal score.
"""

from __future__ import annotations

import json
import re
from typing import Any

from kbcore import embed as embed_mod
from kbcore.config import settings
from kbcore.db import connect, transaction
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
    k: int = 8,
    tool: str = "search",
) -> list[SearchHit]:
    """Hybrid search over all projects by default (cross-domain reuse is the point)."""
    statuses = ["active"]
    if include_superseded:
        statuses.append("superseded")
    if include_archived:
        statuses.append("archived")

    where, params = _filters(project, domain, type, tags, statuses)

    with connect() as conn:
        vec_rows = _vector_leg(conn, query, where, params)
        kw_rows = _keyword_leg(conn, query, where, params)

    fused = _rrf_fuse(vec_rows, kw_rows, boost_project, query)
    # Materialize ALL fused candidates before truncating: the verified ≥
    # unverified tie-break must apply globally, not only inside the top-k
    # window (review finding — an unverified item tied at the k boundary used
    # to displace a verified one).
    hits = _to_hits(fused)[:k]
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
    boost_project: str | None,
    query: str,
) -> list[tuple[int, float, str]]:
    """RRF over both legs, collapsed to items (best/earliest chunk is the snippet)."""
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
    if scores:
        _apply_bonuses(scores, boost_project, query)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [(item_id, score, snippets[item_id]) for item_id, score in ranked]


def _apply_bonuses(scores: dict[int, float], boost_project: str | None, query: str) -> None:
    """Add the project boost and the exact-identifier bonus to the fused scores."""
    ids = list(scores)
    tokens = _identifier_tokens(query)
    if not boost_project and not tokens:
        return
    with connect() as conn:
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


def _to_hits(top: list[tuple[int, float, str]]) -> list[SearchHit]:
    if not top:
        return []
    by_id = {item_id: (score, snippet) for item_id, score, snippet in top}
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT i.id, i.title, i.type, i.project, p.domain, i.status,
                   s.trust, i.tags, i.created_at, s.uri, i.meta, i.superseded_by
            FROM items i
            LEFT JOIN projects p ON p.name = i.project
            LEFT JOIN sources s ON s.id = i.source_id
            WHERE i.id = ANY(%s)
            """,
            (list(by_id),),
        ).fetchall()
    hits = []
    for id_, title, type_, proj, dom, status, trust, tags, created, uri, meta, sup_by in rows:
        score, snippet = by_id[id_]
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

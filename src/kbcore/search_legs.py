r"""kbcore.search_legs — candidate-generation legs for hybrid search (PLAN.md §5.4).

Split out of search.py to keep that module lean (CLAUDE.md: files < ~300 lines),
mirroring the search_expand split. Three legs, each walled by the SAME
scope/status/valid-time WHERE fragment the caller (kbcore.search) builds:

- vector leg: cosine over current-model chunks;
- keyword leg: ``websearch_to_tsquery('simple', q)`` — 'simple' keeps Urdu and
  identifiers like TPS55288 un-stemmed (DATA-6/V7). AND semantics first; when
  AND yields fewer than KW_OR_FALLBACK_MIN rows on a multi-word query, rerun as
  an OR over the query's PARSED lexemes (so file/host tokens like
  ``psycopg.errors.undefinedcolumn`` still match) — one non-matching token no
  longer kills the whole leg. OR rows are ordered by how many DISTINCT query
  lexemes the whole item matches, then ts_rank_cd — an item stuffed with one
  common term cannot outrank a broad match — and must match at least two
  lexemes. AND matches keep their ranks ahead of OR-only matches;
- identifier leg: word-boundary (``\m..\M``) match of the query's identifier
  tokens over item titles + chunks, ranked by distinct-token match count then
  recency. Runs only for identifier-DOMINANT queries (at least half the words
  are identifiers — bare symbols, part numbers), so prose queries are not
  flooded by recency-ranked acronym mentions. When the full symbol matches
  nothing, falls back to word-PREFIX matches of its parts (``drv_spi_write`` →
  ``\mdrv|\mspi|\mwrite`` reaches DRV8706 SPI-write knowledge).

Legs are fused with RRF (Σ 1/(RRF_K + rank)), collapsed to items (best chunk
wins). Bonuses/boosts are applied by the caller AFTER graph expansion.
"""

from __future__ import annotations

import json
import re
from typing import Any

from kbcore import embed as embed_mod
from kbcore.config import settings

RRF_K = 60
CANDIDATES = 40
# OR-fallback threshold: with >= this many AND-matching chunk rows the strict
# leg has enough signal; below it, the query likely carries a poison token
# (AND semantics: one non-matching term kills every row) so the OR rerun fires.
KW_OR_FALLBACK_MIN = 5
# Cap identifier tokens used by the identifier leg and the exact-id bonus: each
# is one unindexed word-regex scan, so an unbounded token count (a 500-token
# pasted query) would amplify into 500 scans (review finding).
MAX_ID_TOKENS = 8
# Cap parsed lexemes fed to the OR fallback (bounds the per-row coverage count
# for pasted walls of text; lexemes beyond this are simply not OR'd).
# Each lexeme costs its own full scan of `chunks` to compute df, so this is a
# LATENCY cap, not a quality one. 64 was reachable in practice: the pre-brief
# hook seeds its query with recent git commit SUBJECTS, and a handful of
# conventional-commit lines tokenises to ~30 lexemes and took 18.8s — past the
# hook's 20s budget, so the brief silently returned nothing (sungrow, KB 1111).
# 12 is well above what carries signal: the IDF weighting means the few rarest
# terms dominate the ranking, and the tail of common words cannot outweigh them.
MAX_OR_LEXEMES = 12

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _is_identifier(t: str) -> bool:
    """True for real code identifiers, not ordinary capitalized words.

    Qualifies: a digit or underscore anywhere; an all-caps acronym (len >= 2,
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


def _id_dominant(query: str, id_tokens: list[str]) -> bool:
    """True when identifiers make up at least HALF the query's words.

    The identifier leg exists for symbol lookups ("drv_spi_write",
    "TPS55288 I2C") — for prose queries that merely mention an acronym it
    would inject recency-ranked mentions of that acronym as noise.
    """
    words = re.findall(r"\w+", query)
    return bool(id_tokens) and 2 * len(id_tokens) >= len(words)


def _token_parts(token: str) -> list[str]:
    """Alphabetic parts (len >= 3) of an identifier: underscores + camel humps.

    drv_spi_write → [drv, spi, write]; UndefinedColumn → [Undefined, Column].
    """
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", token.replace("_", " "))
    return re.findall(r"[A-Za-z]{3,}", spaced)


def _vector_leg(conn, query: str, where: str, params: list[Any]) -> list[tuple[int, str]]:
    # embed_query, not embed_one: the QUERY side carries the instruction prefix
    # required by instruction-aware embedders (asymmetric contract, embed.py).
    qvec = json.dumps(embed_mod.embed_query(query), separators=(",", ":"))
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
    """AND tsquery, with an OR fallback when AND yields < KW_OR_FALLBACK_MIN rows.

    The AND pass is the pre-existing leg, unchanged. The fallback ORs the
    query's parsed lexemes (still parameterized — never interpolated), same
    WHERE wall, same CANDIDATES cap, ordered by distinct-lexeme item coverage
    then ts_rank_cd, and requires >= 2 matched lexemes per item. Single-word
    queries never fall back (their OR is the AND query again; a lone symbol
    like drv_spi_write is the identifier leg's job, not a drv|spi|write soup).
    """
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
    rows = [(r[0], r[1]) for r in rows]
    if len(rows) >= KW_OR_FALLBACK_MIN or len(query.split()) < 2:
        return rows
    lexemes = [
        r[0]
        for r in conn.execute(
            "SELECT unnest(tsvector_to_array(to_tsvector('simple', %s)))", (query,)
        ).fetchall()
    ][:MAX_OR_LEXEMES]
    if len(lexemes) < 2:
        return rows
    or_rows = _or_coverage_rows(conn, lexemes, where, params)
    and_items = {item_id for item_id, _text in rows}
    merged = rows + [(i, t) for i, t in or_rows if i not in and_items]
    return merged[:CANDIDATES]


def _or_coverage_rows(
    conn, lexemes: list[str], where: str, params: list[Any]
) -> list[tuple[int, str]]:
    """OR-matched chunk rows ordered by IDF-WEIGHTED item lexeme coverage.

    Each lexeme carries weight ln(1 + N_items/df); an item's key is the sum over
    its DISTINCT matched lexemes, so one rare topic anchor ("polarity",
    "wireguard") outweighs several ubiquitous words ("mode", "driver") — a doc
    stuffed with common terms cannot bury the on-topic hit. Items matching a
    single lexeme are admitted only when that lexeme is RARE (df at most
    max(8, N/100)): the one term that matters, unstemmed variants and all.
    """
    return conn.execute(
        f"""
        WITH tok AS (
            SELECT websearch_to_tsquery('simple', t) AS tq,
                   (SELECT count(DISTINCT c.item_id) FROM chunks c
                    WHERE c.tsv @@ websearch_to_tsquery('simple', t)) AS df
            FROM unnest(%s::text[]) t
        ),
        stats AS (SELECT count(*)::float AS n_items FROM items),
        cov AS (
            SELECT item_id, count(*) AS n, sum(w) AS wsum, min(df) AS min_df
            FROM (
                SELECT DISTINCT c.item_id, tok.tq::text AS lex, tok.df,
                       ln(1.0 + (SELECT n_items FROM stats)
                                / GREATEST(tok.df, 1)) AS w
                FROM chunks c JOIN tok ON c.tsv @@ tok.tq
            ) m
            GROUP BY item_id
        )
        SELECT c.item_id, c.text
        FROM chunks c
        JOIN cov ON cov.item_id = c.item_id
        JOIN items i ON i.id = c.item_id
        LEFT JOIN projects p ON p.name = i.project,
        websearch_to_tsquery('simple', %s) q
        WHERE c.tsv @@ q AND {where}
              AND (cov.n >= 2
                   OR cov.min_df <= GREATEST(8, (SELECT n_items FROM stats) / 100))
        ORDER BY cov.wsum DESC, ts_rank_cd(c.tsv, q) DESC
        LIMIT {CANDIDATES}
        """,
        [lexemes, " OR ".join(lexemes), *params],
    ).fetchall()


def _identifier_leg(
    conn, tokens: list[str], where: str, params: list[Any]
) -> list[tuple[int, str]]:
    """Word-boundary identifier matches over titles+chunks under the same wall.

    Ranked by how many DISTINCT query identifiers an item matches, then recency.
    Tokens come from _identifier_tokens ([A-Za-z0-9_] only), so embedding them
    in a ``\\m..\\M`` pattern needs no regex escaping; the patterns themselves
    still travel as one parameterized text[] — never interpolated into SQL.
    When no item matches any FULL token, retries with word-PREFIX patterns of
    the tokens' parts (``\\mdrv`` reaches DRV8706 for query drv_spi_write).
    """
    tokens = tokens[:MAX_ID_TOKENS]
    if not tokens:
        return []
    rows = _id_match_rows(conn, [rf"\m{t}\M" for t in tokens], where, params, min_matches=1)
    if rows:
        return rows
    parts = list(dict.fromkeys(p for t in tokens for p in _token_parts(t)))[:MAX_ID_TOKENS]
    if not parts or set(p.lower() for p in parts) == set(t.lower() for t in tokens):
        return rows
    # High-precision fallback: EVERY part must appear ("mentions a DRV part AND
    # spi AND write") — any-part matches would flood the leg with items that
    # merely contain one common word and double-count against the vector leg.
    return _id_match_rows(conn, [rf"\m{p}" for p in parts], where, params, min_matches=len(parts))


def _id_match_rows(
    conn, patterns: list[str], where: str, params: list[Any], min_matches: int
) -> list[tuple[int, str]]:
    rows = conn.execute(
        f"""
        WITH pt AS (SELECT unnest(%s::text[]) AS pat),
        matched AS (
            SELECT i.id AS item_id, i.created_at,
                   (SELECT count(*) FROM pt WHERE i.title ~* pt.pat OR EXISTS (
                        SELECT 1 FROM chunks c
                        WHERE c.item_id = i.id AND c.text ~* pt.pat)) AS n_tokens
            FROM items i
            LEFT JOIN projects p ON p.name = i.project
            WHERE {where} AND EXISTS (
                SELECT 1 FROM pt WHERE i.title ~* pt.pat OR EXISTS (
                    SELECT 1 FROM chunks c WHERE c.item_id = i.id AND c.text ~* pt.pat))
        )
        SELECT m.item_id,
               COALESCE((SELECT c.text FROM chunks c JOIN pt ON c.text ~* pt.pat
                         WHERE c.item_id = m.item_id ORDER BY c.ord LIMIT 1), '')
        FROM matched m
        WHERE m.n_tokens >= %s
        ORDER BY m.n_tokens DESC, m.created_at DESC, m.item_id DESC
        LIMIT {CANDIDATES}
        """,
        [patterns, *params, min_matches],
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _rrf_fuse(*legs: list[tuple[int, str]]) -> list[tuple[int, float, str]]:
    """RAW RRF over N legs, collapsed to items (best/earliest chunk is snippet).

    No bonuses here: boost_project / exact-identifier are applied by the caller
    AFTER graph expansion (search_expand seeds off these RAW scores). An empty
    leg contributes nothing, so 2-leg callers fuse exactly as before.
    """
    scores: dict[int, float] = {}
    snippets: dict[int, str] = {}
    for leg in legs:
        seen: set[int] = set()
        for rank, (item_id, text) in enumerate(leg, start=1):
            if item_id in seen:
                continue  # best chunk per item per leg
            seen.add(item_id)
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (RRF_K + rank)
            if text:
                snippets.setdefault(item_id, text)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [(item_id, score, snippets.get(item_id, "")) for item_id, score in ranked]

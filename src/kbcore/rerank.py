"""kbcore.rerank — optional cross-encoder rerank leg over the fused candidates.

A llama-server sidecar (Qwen3-Reranker-0.6B GGUF, ``--reranking --pooling
rank``) scores the top fused candidates against the query and reorders them.
Measured 2026-07-18: ~425 ms warm for 20 x ~500-token docs on the 4080S GPU
(inside the 2 s interactive budget); golden-pair sanity 0.99994 vs 3e-05 —
the broken-GGUF failure mode (uniform ~e-23 scores) is guarded by tests and
the deploy AC.

Fail-open by design: ANY sidecar failure (down, timeout, malformed reply)
returns the fused order unchanged — the reranker may only ever REORDER, never
drop, add, or fail a search. Inert unless ``kb_rerank_url`` is set.

Score handling: ``_to_hits`` re-sorts by score (verified-first tie-break), so
reordering the list alone would be undone. Instead the existing score
MULTISET of the reranked window is reassigned to the new order (best rerank
gets the window's highest fused score, and so on) — order survives the
re-sort while the score scale, expansion clamps, and bonus semantics stay
exactly as fused.
"""

from __future__ import annotations

import logging

import httpx

from kbcore.config import settings

logger = logging.getLogger("kbcore.rerank")

_DOC_BODY_CHARS = 1500  # title + this much body per candidate document


def _documents(conn, item_ids: list[int]) -> dict[int, str]:
    """title + body-prefix per candidate, one parameterized query."""
    rows = conn.execute(
        "SELECT id, title, left(body, %s) FROM items WHERE id = ANY(%s)",
        (_DOC_BODY_CHARS, item_ids),
    ).fetchall()
    return {r[0]: f"{r[1]}\n{r[2]}" for r in rows}


def _scores(query: str, documents: list[str]) -> list[float] | None:
    """Sidecar /v1/rerank call; None on ANY failure (fail-open)."""
    try:
        resp = httpx.post(
            f"{settings.kb_rerank_url.rstrip('/')}/v1/rerank",
            json={"model": "reranker", "query": query, "documents": documents},
            timeout=settings.kb_rerank_timeout,
        )
        resp.raise_for_status()
        results = resp.json()["results"]
        out = [0.0] * len(documents)
        for r in results:
            out[int(r["index"])] = float(r["relevance_score"])
        return out
    except Exception as e:  # noqa: BLE001 — reranking must never fail a search
        logger.warning(
            "rerank sidecar unavailable/failed (%s: %s); fused order stands",
            e.__class__.__name__,
            e,
        )
        return None


def rerank_fused(
    conn, query: str, fused: list[tuple[int, float, str]], top_n: int
) -> list[tuple[int, float, str]]:
    """Reorder the top ``top_n`` fused candidates by cross-encoder relevance.

    Returns ``fused`` unchanged on any failure or when there is nothing to
    reorder. The tail beyond ``top_n`` is never touched.
    """
    if top_n < 2 or len(fused) < 2:
        return fused
    ordered = sorted(fused, key=lambda t: -t[1])
    head, tail = ordered[:top_n], ordered[top_n:]
    docs_by_id = _documents(conn, [t[0] for t in head])
    if len(docs_by_id) < 2:
        return fused
    ranked_ids = [t[0] for t in head if t[0] in docs_by_id]
    scores = _scores(query, [docs_by_id[i] for i in ranked_ids])
    if scores is None:
        return fused
    by_id = {t[0]: t for t in head}
    rerank_order = [i for _, i in sorted(zip(scores, ranked_ids, strict=True), key=lambda p: -p[0])]
    rerank_order += [t[0] for t in head if t[0] not in docs_by_id]  # doc-less keep rank
    # Reassign the window's own fused scores to the new order (see module docstring).
    window_scores = sorted((t[1] for t in head), reverse=True)
    new_head = [
        (item_id, score, by_id[item_id][2])
        for item_id, score in zip(rerank_order, window_scores, strict=True)
    ]
    return new_head + tail

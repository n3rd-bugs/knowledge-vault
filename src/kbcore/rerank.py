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
re-sort and the score SCALE stays as fused. Within the window that
reassignment CAN reorder an expansion neighbor above its seed (review #5
val-2): deliberate — the cross-encoder read both documents against the query,
which outranks the query-blind seed clamp. The tail beyond the window is
never touched.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

from kbcore.config import settings

logger = logging.getLogger("kbcore.rerank")

_DOC_BODY_CHARS = 1500  # title + this much body per candidate document

# --- participation telemetry (review #5 ops-4 / ret-rerank-2 / val-1) ---------
# Which ranking system did THIS search actually measure? Thread-local so
# concurrent server threads cannot cross-talk; writer and reader share a thread
# by construction (kbcore.search reads inline right after the rerank step, and
# the eval sentinel replays the harness synchronously on one thread). CORRECTED
# (review #6): a single read observes only the LAST rerank on its thread — a
# harness-wide read after N cases reflects case N alone. The per-case picture
# is the ARMED TALLY below (reset_tally / outcome_tally): _note accumulates
# the final-when-noted outcomes so the sentinel never has to hook the harness.
_participation = threading.local()


def _note(outcome: str, top_relevance: float | None = None) -> None:
    _participation.outcome = outcome
    _participation.top_relevance = top_relevance
    # Armed-tally accumulation (review #6): 'active'/'fallback' are each FINAL
    # when noted — at most one of the two fires per search, and nothing renotes
    # after it. 'off' is entry/reset noise here; outcome_tally derives it.
    if outcome in ("active", "fallback"):
        tally = getattr(_participation, "tally", None)
        if tally is not None:
            tally[outcome] += 1


def reset_tally() -> None:
    """Arm/zero this thread's per-search outcome tally (review #6).

    The eval sentinel calls this right before replaying the golden set;
    ordinary production searches never arm it, so nothing accumulates outside
    an eval window."""
    _participation.tally = {"active": 0, "fallback": 0}


def outcome_tally(searches: int) -> dict[str, int]:
    """``{'active', 'fallback', 'off'}`` counts since :func:`reset_tally`.

    'off' is DERIVED as ``searches - active - fallback`` (floored at 0): every
    search finishes having noted at most one of 'active'/'fallback' (in
    rerank_fused/_scores), and a search that noted neither ended 'off' —
    reset_outcome, no URL, or nothing to reorder. ``searches`` is the number of
    searches the caller ran in the window (one per golden case). Unarmed reads
    return an all-derived-'off' tally rather than raising."""
    tally = dict(getattr(_participation, "tally", None) or {"active": 0, "fallback": 0})
    tally["off"] = max(0, int(searches) - tally["active"] - tally["fallback"])
    return tally


def last_outcome() -> str:
    """Most recent rerank participation in this thread: 'active'|'fallback'|'off'.

    'active' = the sidecar scored the window; 'fallback' = it was attempted and
    failed, fused order stood; 'off' = it did not participate (no URL configured,
    or nothing to reorder). Read per search by kbcore.search_misses.observe;
    the eval sentinel now reads the ARMED TALLY instead (review #6 — a single
    read after N harness cases reflects case N alone), still attribution from
    meta, not journald archaeology.
    """
    return getattr(_participation, "outcome", "off")


def last_top_relevance() -> float | None:
    """Top-1 relevance score of this thread's last 'active' rerank, else None.

    Consumed by kbcore.search_misses.observe for the thin-miss floor and the
    calibration-week score distribution (review #5 val-1).
    """
    return getattr(_participation, "top_relevance", None)


def reset_outcome() -> None:
    """Mark 'off' — kbcore.search calls this when the rerank leg does not run."""
    _note("off")


# Fixed pair for :func:`probe`: the first document answers the query, the second
# cannot. A healthy sidecar separates them by orders of magnitude (golden-pair
# sanity 0.99994 vs 3e-05); the known-bad GGUF mode scores everything uniformly
# (~e-23), so the probe checks DISCRIMINATION, not just HTTP 200.
_PROBE_QUERY = "wireguard handshake timeout fix"
_PROBE_DOCS = [
    "WireGuard handshake timeouts: check PersistentKeepalive and that the UDP "
    "port is reachable; an MTU mismatch also stalls the handshake.",
    "The bakery's sourdough starter needs feeding twice a day in summer.",
]


def probe() -> dict[str, bool | int | None]:
    """One fixed two-document sidecar probe: ``{'ok': bool, 'latency_ms': int|None}``.

    Fail-open and NEVER raises (Wave-F cross-package contract): the nightly
    consolidate tail step calls this into ``counts['rerank_probe']`` /
    ``counts['rerank_probe_ms']`` and the digest renders a positive-only line on
    failure. ``ok`` requires the relevant document to OUTSCORE the irrelevant
    one — a liveness-only check would miss the broken-GGUF uniform-score mode.
    With ``kb_rerank_url`` unset this reports failure without any HTTP call
    (callers gate on the setting). Never touches the per-search participation
    state above.
    """
    if not settings.kb_rerank_url:
        return {"ok": False, "latency_ms": None}
    try:
        t0 = time.monotonic()
        resp = httpx.post(
            f"{settings.kb_rerank_url.rstrip('/')}/v1/rerank",
            json={"model": "reranker", "query": _PROBE_QUERY, "documents": _PROBE_DOCS},
            timeout=settings.kb_rerank_timeout,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        resp.raise_for_status()
        scores = [0.0, 0.0]
        for r in resp.json()["results"]:
            scores[int(r["index"])] = float(r["relevance_score"])
        return {"ok": scores[0] > scores[1], "latency_ms": latency_ms}
    except Exception as e:  # noqa: BLE001 — the probe itself must be fail-open
        logger.warning("rerank probe failed (%s: %s)", e.__class__.__name__, e)
        return {"ok": False, "latency_ms": None}


def _documents(conn, item_ids: list[int], evidence: dict[int, str] | None = None) -> dict[int, str]:
    """title + body-prefix per candidate, one parameterized query.

    ``evidence`` (search._rerank_evidence, gated on ``kb_rerank_use_chunk`` —
    code default OFF; this deployment's .env enabled it 2026-08-01 after the
    A/B, verified KB 6495) optionally supplies the best-matching CHUNK text per
    item to score INSTEAD of the body prefix. Absent/empty entries fall back to
    the body prefix, and with ``evidence=None`` every document is byte-identical
    to the pre-flag behaviour.
    """
    rows = conn.execute(
        "SELECT id, title, left(body, %s) FROM items WHERE id = ANY(%s)",
        (_DOC_BODY_CHARS, item_ids),
    ).fetchall()
    docs: dict[int, str] = {}
    for item_id, title, body in rows:
        chunk = evidence.get(item_id) if evidence else None
        docs[item_id] = f"{title}\n{chunk[:_DOC_BODY_CHARS] if chunk else body}"
    return docs


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
        # 'fallback' marks the search as measured on the FUSED order (review #5
        # ops-4): the WARNING below lives in <36h journald retention, so the
        # durable record of "which ranking system ran" is this marker, read by
        # search_misses.observe and the eval sentinel via last_outcome().
        _note("fallback")
        logger.warning(
            "rerank sidecar unavailable/failed (%s: %s); fused order stands",
            e.__class__.__name__,
            e,
        )
        return None


def rerank_fused(
    conn,
    query: str,
    fused: list[tuple[int, float, str]],
    top_n: int,
    evidence: dict[int, str] | None = None,
) -> list[tuple[int, float, str]]:
    """Reorder the top ``top_n`` fused candidates by cross-encoder relevance.

    Returns ``fused`` unchanged on any failure or when there is nothing to
    reorder. The tail beyond ``top_n`` is never touched. ``evidence`` is the
    optional per-item chunk text to score instead of the body prefix (see
    :func:`_documents`); None — the code default for a fresh install — keeps
    the documents byte-identical to the pre-flag behaviour (this deployment
    passes chunk evidence: .env flag ON since the 2026-08-01 A/B, KB 6495).

    Participation telemetry: entry marks 'off'; a failed sidecar call marks
    'fallback' (in :func:`_scores`); success marks 'active' with the top-1
    relevance score (see :func:`last_outcome` / :func:`last_top_relevance`).
    """
    _note("off")  # not attempted yet; early returns leave it that way
    if top_n < 2 or len(fused) < 2:
        return fused
    ordered = sorted(fused, key=lambda t: -t[1])
    head, tail = ordered[:top_n], ordered[top_n:]
    docs_by_id = _documents(conn, [t[0] for t in head], evidence)
    if len(docs_by_id) < 2:
        return fused
    ranked_ids = [t[0] for t in head if t[0] in docs_by_id]
    scores = _scores(query, [docs_by_id[i] for i in ranked_ids])
    if scores is None:
        return fused
    _note("active", max(scores))
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

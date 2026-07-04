"""kbcore.evidence — corroboration counts for open contradictions (weightage).

When the nightly pass flags ``a -contradicts-> b``, arbitration is easier with
evidence: items semantically near the pair are judged by the local LLM as
supporting side A, side B, or neither, and the counts (raw + trust-weighted)
are stored on the OPEN review row's ``detail`` JSONB. Nightly repo seeds and
session flushes keep adding corroborating items, so the nightly step refreshes
these counts until the row is resolved. Updating ``detail`` on an open row is
additive enrichment (JSONB merge — existing keys preserved, nothing deleted).
All SQL lives here (Invariant 2); the GPU-bound judging is invoked only from
the nightly consolidation worker (Invariant 8).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from kbcore import llm
from kbcore.consolidation import item_pair_texts
from kbcore.db import connect, transaction
from kbcore.graph import similar

# Verified sources (curated docs, human-approved items) outweigh unverified
# session extractions when tallying which side the corpus supports.
TRUST_WEIGHTS = {"verified": 2, "unverified": 1}
MAX_CANDIDATES = 8  # LLM judge calls per pair per night (cost bound)
SIMILAR_K = 8  # nearest-neighbour fan per side


def open_contradiction_rows() -> list[tuple[int, int, int, str | None]]:
    """Open contradiction rows whose BOTH items are still active.

    Returns ``(queue_id, a_id, b_id, evidence_updated_iso_or_None)`` ordered so
    never-enriched rows come first — new contradictions get evidence before
    refreshes consume the nightly cap. Rows whose items were resolved away
    (superseded/archived) are skipped: the conflict is moot, judging it would
    waste GPU.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT rq.id,
                   (rq.detail->>'a_id')::bigint,
                   (rq.detail->>'b_id')::bigint,
                   rq.detail->>'evidence_updated'
            FROM review_queue rq
            JOIN items ia ON ia.id = (rq.detail->>'a_id')::bigint AND ia.status = 'active'
            JOIN items ib ON ib.id = (rq.detail->>'b_id')::bigint AND ib.status = 'active'
            WHERE rq.resolved = false AND rq.reason = 'contradiction'
            ORDER BY (rq.detail->>'evidence_updated') NULLS FIRST, rq.id
            """
        ).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def candidate_texts(item_ids: Sequence[int]) -> dict[int, tuple[str, str, str]]:
    """``id -> (title, body, trust)`` for the judge, in one query."""
    ids = sorted({int(i) for i in item_ids})
    if not ids:
        return {}
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT i.id, i.title, i.body, COALESCE(s.trust, 'verified')
            FROM items i LEFT JOIN sources s ON s.id = i.source_id
            WHERE i.id = ANY(%s)
            """,
            (ids,),
        ).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def gather_evidence(a_id: int, b_id: int) -> dict[str, Any]:
    """Judge the items nearest to a contradiction pair; return the detail payload.

    Pure gather — no writes. Candidates are the union of ``similar()`` fans from
    both sides (dedup keeping the best score, minus the pair itself, minus
    summary/report items — a summary derived from one side would trivially echo
    it), capped at :data:`MAX_CANDIDATES`. 'neither' judgements are kept in the
    evidence list for audit but excluded from the counts.
    """
    fans: dict[int, tuple[float, str]] = {}  # id -> (best score, type)
    for anchor in (a_id, b_id):
        for h in similar(anchor, k=SIMILAR_K):
            if h.item_id in (a_id, b_id) or h.type in ("summary", "report"):
                continue
            prev = fans.get(h.item_id)
            if prev is None or h.score > prev[0]:
                fans[h.item_id] = (h.score, h.type)
    ranked = sorted(fans, key=lambda i: -fans[i][0])[:MAX_CANDIDATES]

    (a_title, a_body), (b_title, b_body) = item_pair_texts(a_id, b_id)
    texts = candidate_texts(ranked)
    in_favor = {"a": 0, "b": 0}
    weighted = {"a": 0, "b": 0}
    evidence: list[dict[str, Any]] = []
    for cid in ranked:
        if cid not in texts:
            continue
        c_title, c_body, trust = texts[cid]
        side = llm.evidence_side(a_title, a_body, b_title, b_body, c_title, c_body)
        evidence.append({"item_id": cid, "side": side, "trust": trust, "title": c_title})
        if side in in_favor:
            in_favor[side] += 1
            weighted[side] += TRUST_WEIGHTS.get(trust, 1)
    return {
        "a_id": a_id,
        "b_id": b_id,
        "in_favor_a": in_favor["a"],
        "in_favor_b": in_favor["b"],
        "weighted_a": weighted["a"],
        "weighted_b": weighted["b"],
        "evidence": evidence,
        "evidence_updated": datetime.now(UTC).isoformat(),
    }


def update_review_detail(queue_id: int, detail: dict[str, Any]) -> bool:
    """Merge ``detail`` into an OPEN review row's JSONB (additive enrichment).

    ``detail || %s`` preserves existing keys (``a_id``/``b_id`` and anything a
    future writer adds); resolved rows are never touched. Returns True iff a
    row was updated.
    """
    with transaction() as conn:
        row = conn.execute(
            "UPDATE review_queue SET detail = detail || %s "
            "WHERE id = %s AND resolved = false RETURNING id",
            (Jsonb(detail), queue_id),
        ).fetchone()
    return row is not None

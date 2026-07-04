"""kbcore.deepsearch — multi-query retrieval + grounded synthesis (local model).

An explicit, user-invoked "dig out the architectural details" run: plan 3-6
diverse sub-queries, retrieve across the KB with the existing hybrid search,
read the full bodies (bounded), and synthesize a cited answer — all on the
locally deployed Ollama model (deliberately slow-but-available; no cloud API).

Invariant 8 note: like ``kb ingest-session``, this is an explicit user-invoked
run, so the 14B may be used; intermediate calls hold the model for
:data:`RUN_KEEP_ALIVE` seconds to avoid reload thrash, and the FINAL call
passes ``keep_alive=0`` so nothing stays resident after the run. The report is
saved BEFORE returning (type='report', trust='unverified' — hard rule 4: LLM
synthesis is never auto-verified) so a timed-out MCP client can still fetch it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from kbcore import llm
from kbcore.config import settings
from kbcore.db import connect
from kbcore.errors import ExtractorUnavailable
from kbcore.ingest import ingest
from kbcore.models import DeepSearchResult, SearchHit
from kbcore.search import search

log = logging.getLogger("kbcore.deepsearch")

MIN_QUERIES, MAX_QUERIES = 3, 6
BODY_CHAR_CAP = 4_000  # per-item body budget in the synthesis context
CONTEXT_CHAR_CAP = 28_000  # total synthesis context budget
RUN_KEEP_ALIVE = 120  # seconds; intermediate calls only — the final call passes 0

_PLAN_SYSTEM = (
    "You turn one engineering question into diverse search queries for a "
    "knowledge base of architecture docs, decisions, gotchas and bug fixes.\n"
    'Return STRICT JSON only: {"queries": ["...", "..."]} with '
    f"{MIN_QUERIES}-{MAX_QUERIES} queries. Vary the angle: component names, "
    "protocols, error phrases, design terms. Short keyword-style queries work "
    "best. No prose."
)

_SYNTH_SYSTEM = (
    "You answer an engineering question ONLY from the numbered knowledge-base "
    "items provided. Rules:\n"
    "- Cite every claim inline as [item <id>].\n"
    "- If the items do not answer part of the question, say what is missing — "
    "never fill gaps from outside knowledge.\n"
    "- If items disagree (contradiction notes are provided), present both sides "
    "with their citations.\n"
    "- Structure: a direct answer first, then supporting detail. No preamble."
)


def plan_queries(question: str) -> list[str]:
    """3-6 diverse sub-queries via the local model; falls back to [question]."""
    try:
        content = llm.chat(
            [
                {"role": "system", "content": _PLAN_SYSTEM},
                {"role": "user", "content": question},
            ],
            json_format=True,
            model=settings.deep_model,
            keep_alive=RUN_KEEP_ALIVE,
        )
        data = llm.parse_model_json(content)
        raw = data.get("queries") if isinstance(data, dict) else None
        queries = [q.strip() for q in raw if isinstance(q, str) and q.strip()] if raw else []
    except (ValueError, TypeError):
        queries = []
    if not queries:
        log.warning("deep-search query planning failed; falling back to the question")
        return [question]
    deduped = list(dict.fromkeys(queries))[:MAX_QUERIES]
    if question not in deduped:  # the literal question is always one of the angles
        deduped = [question, *deduped][:MAX_QUERIES]
    return deduped


def _retrieve(queries: list[str], project: str | None, k_per_query: int) -> list[SearchHit]:
    """Union of hybrid-search hits across sub-queries, best score per item.

    ``type='report'`` hits are dropped — a previous deep-search report citing
    itself would be an echo chamber; summaries are kept (useful synthesis).
    """
    best: dict[int, SearchHit] = {}
    for q in queries:
        for h in search(q, project=project, k=k_per_query, tool="deepsearch"):
            if h.type == "report":
                continue
            prev = best.get(h.item_id)
            if prev is None or h.score > prev.score:
                best[h.item_id] = h
    return sorted(best.values(), key=lambda h: -h.score)


def _fetch_docs(ids: list[int]) -> list[dict[str, Any]]:
    """Full bodies for the selected ids, in the given (ranked) order."""
    if not ids:
        return []
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT i.id, i.title, i.body, i.project, i.type,
                   COALESCE(s.trust, 'verified'), i.created_at
            FROM items i LEFT JOIN sources s ON s.id = i.source_id
            WHERE i.id = ANY(%s)
            """,
            (ids,),
        ).fetchall()
    by_id = {
        r[0]: {
            "id": r[0],
            "title": r[1],
            "body": r[2],
            "project": r[3],
            "type": r[4],
            "trust": r[5],
            "created_at": r[6],
        }
        for r in rows
    }
    return [by_id[i] for i in ids if i in by_id]


def _bounded_docs(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enforce per-body and total context budgets; log what was cut (no silent caps)."""
    out: list[dict[str, Any]] = []
    total = 0
    truncated: list[int] = []
    dropped: list[int] = []
    for d in docs:
        body = d["body"]
        if len(body) > BODY_CHAR_CAP:
            body = body[:BODY_CHAR_CAP] + "\n[... truncated]"
            truncated.append(d["id"])
        if total + len(body) > CONTEXT_CHAR_CAP:
            dropped.append(d["id"])
            continue
        total += len(body)
        out.append({**d, "body": body})
    if truncated:
        log.info("deep-search truncated bodies of items %s to %d chars", truncated, BODY_CHAR_CAP)
    if dropped:
        log.info("deep-search dropped items %s (context budget %d)", dropped, CONTEXT_CHAR_CAP)
    return out


def _contradiction_edges(ids: list[int]) -> list[tuple[int, int]]:
    """contradicts relations where BOTH endpoints are among the selected items."""
    if len(ids) < 2:
        return []
    with connect() as conn:
        rows = conn.execute(
            "SELECT src_id, dst_id FROM relations "
            "WHERE rel = 'contradicts' AND src_kind = 'item' AND dst_kind = 'item' "
            "AND src_id = ANY(%s) AND dst_id = ANY(%s)",
            (ids, ids),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def synthesize(question: str, docs: list[dict[str, Any]], conflicts: list[tuple[int, int]]) -> str:
    """Grounded synthesis over the bounded docs. Final LLM call → keep_alive=0."""
    parts = []
    for d in docs:
        parts.append(
            f"[item {d['id']}] ({d['type']} · {d['project']} · {d['trust']} · "
            f"{d['created_at'].date().isoformat()}) {d['title']}\n{d['body']}"
        )
    if conflicts:
        notes = "\n".join(f"- item {a} contradicts item {b}" for a, b in conflicts)
        parts.append(f"Known contradictions among these items:\n{notes}")
    user = f"Question: {question}\n\nItems:\n\n" + "\n\n---\n\n".join(parts)
    return llm.chat(
        [{"role": "system", "content": _SYNTH_SYSTEM}, {"role": "user", "content": user}],
        model=settings.deep_model,
        keep_alive=0,  # final call — the model must not outlive the run (Inv. 8)
    ).strip()


def deep_search(
    question: str,
    project: str | None = None,
    k_per_query: int = 8,
    max_items: int | None = None,
    save: bool = True,
) -> DeepSearchResult:
    """Plan → retrieve → read → synthesize → save. SLOW (minutes on the 14B)."""
    if not question.strip():
        raise ExtractorUnavailable("deep search needs a non-empty question")
    limit = max_items or settings.kb_deep_max_items

    queries = plan_queries(question)
    hits = _retrieve(queries, project, k_per_query)[:limit]
    ids = [h.item_id for h in hits]
    log.info("deep-search %r: %d queries -> %d items", question[:60], len(queries), len(ids))
    if not ids:
        return DeepSearchResult(
            question=question,
            queries=queries,
            report="No KB items matched the planned queries — nothing to synthesize.",
            sources=[],
            item_id=None,
        )

    docs = _bounded_docs(_fetch_docs(ids))
    kept_ids = [d["id"] for d in docs]
    conflicts = _contradiction_edges(kept_ids)
    report = synthesize(question, docs, conflicts)

    footer = "\n\nSources:\n" + "\n".join(
        f"- [item {d['id']}] {d['title']} ({d['project']}, {d['trust']})" for d in docs
    )
    report_text = report + footer

    item_id: int | None = None
    if save:
        # Saved BEFORE returning so a timed-out MCP client can still fetch it.
        # Timestamped uri sidesteps content-hash dedupe (nightly-report precedent).
        item_id = ingest(
            project or "kb-system",
            "report",
            f"Deep search: {question[:80]}",
            report_text,
            kind="chat",
            uri=f"deepsearch:{datetime.now(UTC).isoformat()}",
            trust="unverified",
            meta={
                "deep_search": question,
                "queries": queries,
                "sources": kept_ids,
                "model": settings.deep_model,
            },
        )
    return DeepSearchResult(
        question=question,
        queries=queries,
        report=report_text,
        sources=kept_ids,
        item_id=item_id,
    )

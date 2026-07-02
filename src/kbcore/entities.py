"""kbcore.entities — entity graph derived from tags + code_ref (Phase 5).

Design decision (honors Invariant 8): PLAN §11 says "entity extraction on
ingest", but the 14B extractor may run only in consolidate / explicit
ingest-session. So entities are derived from the already-structured, verbatim
namespaced tags and code_ref anchor — NO LLM call, GPU-free — and can run
synchronously inside ``ingest_in``. Free-text NER can join as a later nightly
step; it is deliberately not added now.

Mapping: hw:→hardware, protocol:→protocol, framework:→library, tool:→tool,
api:→api, lang:/os:/domain:→concept; code_ref.repo→repo. Entity name is the tag
VALUE (namespace prefix dropped) or the repo name; the namespace becomes ``kind``.
"""

from __future__ import annotations

from typing import Any

import psycopg

# Namespace prefix → entities.kind (must satisfy the 001 CHECK constraint).
_TAG_KINDS = {
    "hw": "hardware",
    "protocol": "protocol",
    "framework": "library",
    "tool": "tool",
    "api": "api",
    "lang": "concept",
    "os": "concept",
    "domain": "concept",
}


def _derive(tags: list[str] | None, code_ref: Any) -> list[tuple[str, str]]:
    """(name, kind) pairs grounded verbatim in the tags + code_ref. Order-stable."""
    out: list[tuple[str, str]] = []
    for tag in tags or []:
        if not isinstance(tag, str) or ":" not in tag:
            continue
        ns, _, value = tag.partition(":")
        kind = _TAG_KINDS.get(ns.strip().lower())
        value = value.strip()
        if kind and value:
            out.append((value, kind))
    repo = None
    if isinstance(code_ref, dict):
        repo = code_ref.get("repo")
    elif code_ref is not None:
        repo = getattr(code_ref, "repo", None)
    if isinstance(repo, str) and repo.strip():
        out.append((repo.strip(), "repo"))
    return list(dict.fromkeys(out))  # dedupe, preserve order


def extract_entities_in(
    conn: psycopg.Connection,
    item_id: int,
    project: str,
    tags: list[str] | None,
    code_ref: Any = None,
) -> int:
    """Upsert tag/code_ref entities + ``item -mentions-> entity`` relations.

    Runs inside the caller's transaction (composition point for ingest_in).
    Idempotent: entity upsert is ON CONFLICT, the relation ON CONFLICT DO NOTHING.
    Returns the number of entities linked.
    """
    derived = _derive(tags, code_ref)
    for name, kind in derived:
        entity_id = conn.execute(
            """
            INSERT INTO entities (project, name, kind) VALUES (%s, %s, %s)
            ON CONFLICT (project, name, kind) DO UPDATE SET updated_at = now()
            RETURNING id
            """,
            (project, name, kind),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO relations (project, src_kind, src_id, rel, dst_kind, dst_id)
            VALUES (%s, 'item', %s, 'mentions', 'entity', %s)
            ON CONFLICT DO NOTHING
            """,
            (project, item_id, entity_id),
        )
    return len(derived)


def backfill_entities() -> int:
    """Populate entities/mentions for existing active items from stored tags+code_ref.

    For the live KB's pre-Phase-5 items. Idempotent — safe to re-run. Returns the
    number of items processed. Each item is handled in its own transaction.
    """
    from kbcore.db import connect, transaction

    with connect() as conn:
        rows = conn.execute(
            "SELECT id, project, tags, meta FROM items WHERE status = 'active' ORDER BY id"
        ).fetchall()
    processed = 0
    for item_id, project, tags, meta in rows:
        code_ref = meta.get("code_ref") if isinstance(meta, dict) else None
        with transaction() as tconn:
            extract_entities_in(tconn, item_id, project, tags or [], code_ref)
        processed += 1
    return processed

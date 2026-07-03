"""kbcore.ingest — idempotent ingest pipeline, one transaction (PLAN.md §5.1).

normalize → scrub → hash → upsert projects → upsert sources (dup ⇒ no-op) →
insert item → chunk → embed → insert chunks. Original file/session/git content
is stored zstd-compressed under KB_RAW (OPS-4).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import psycopg
import zstandard
from psycopg.types.json import Jsonb

from kbcore import chunk as chunk_mod
from kbcore import embed as embed_mod
from kbcore import entities as entities_mod
from kbcore import scope
from kbcore.config import settings
from kbcore.db import transaction
from kbcore.models import ITEM_TYPES, SOURCE_KINDS, CodeRef
from kbcore.scrub import scrub


def normalize(text: str) -> str:
    """Unify newlines, strip BOM and outer whitespace. Bodies are stored normalized."""
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()


def normalize_title(title: str) -> str:
    """Titles are single-line: collapse all whitespace runs to one space.

    Also keeps the ``sha256(title\\n\\ntitle-body)`` dedupe key unambiguous \u2014 a
    newline-bearing title could otherwise alias a different (title, body)
    pair (review finding).
    """
    return " ".join(title.split())


def content_hash(title: str, body: str) -> str:
    """sha256 over normalized title+body — the source-level dedupe key (DATA-1)."""
    return hashlib.sha256(f"{title}\n\n{body}".encode()).hexdigest()


def ingest(
    project: str,
    type: str,
    title: str,
    body: str,
    *,
    kind: str = "manual",
    uri: str = "",
    trust: str = "verified",
    tags: list[str] | None = None,
    meta: dict[str, Any] | None = None,
    code_ref: CodeRef | dict | None = None,
    raw_content: bytes | None = None,
    is_source: bool = False,
) -> int | None:
    """Ingest one item. Returns the new item id, or None if a content-hash duplicate."""
    with transaction() as conn:
        return ingest_in(
            conn,
            project,
            type,
            title,
            body,
            kind=kind,
            uri=uri,
            trust=trust,
            tags=tags,
            meta=meta,
            code_ref=code_ref,
            raw_content=raw_content,
            is_source=is_source,
        )


def ingest_in(
    conn: psycopg.Connection,
    project: str,
    type: str,
    title: str,
    body: str,
    *,
    kind: str = "manual",
    uri: str = "",
    trust: str = "verified",
    tags: list[str] | None = None,
    meta: dict[str, Any] | None = None,
    code_ref: CodeRef | dict | None = None,
    raw_content: bytes | None = None,
    is_source: bool = False,
) -> int | None:
    """Ingest within the caller's transaction (composition point for supersede)."""
    if type not in ITEM_TYPES:
        raise ValueError(f"unknown item type {type!r}")
    if kind not in SOURCE_KINDS:
        raise ValueError(f"unknown source kind {kind!r}")
    # Public-only instance: reject writes to non-public (or new, default-private)
    # projects. No-op on a full-access instance. Covers ingest/kb_save/summary/etc.
    scope.assert_writable(project)

    body = normalize(body)
    title = normalize_title(title)
    body, redactions = scrub(body)
    title, title_redactions = scrub(title)
    for k, v in title_redactions.items():
        redactions[k] = redactions.get(k, 0) + v
    chash = content_hash(title, body)

    conn.execute("INSERT INTO projects (name) VALUES (%s) ON CONFLICT DO NOTHING", (project,))

    row = conn.execute(
        """
        INSERT INTO sources (kind, uri, project, content_hash, trust)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (kind, uri, content_hash) DO NOTHING
        RETURNING id
        """,
        (kind, uri, project, chash, trust),
    ).fetchone()
    if row is None:
        return None  # DATA-1: same content re-ingested is a no-op
    source_id = row[0]

    if raw_content is not None and kind in ("file", "session", "git"):
        raw_path = archive_raw(project, raw_content)
        conn.execute("UPDATE sources SET raw_path = %s WHERE id = %s", (raw_path, source_id))

    full_meta = dict(meta or {})
    if redactions:
        full_meta["redactions"] = redactions
    if code_ref is not None:
        ref = code_ref if isinstance(code_ref, CodeRef) else CodeRef(**code_ref)
        full_meta["code_ref"] = ref.model_dump(exclude_none=True)

    item_id = conn.execute(
        """
        INSERT INTO items (source_id, project, type, title, body, tags, meta)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (source_id, project, type, title, body, tags or [], Jsonb(full_meta)),
    ).fetchone()[0]

    # The source splitter is only for actual raw source files (inbox drops,
    # Phase 3); a markdown-bodied snippet must keep §5.2 fence atomicity, so
    # routing is by the caller's is_source flag, never by item type.
    chunks = chunk_mod.chunk_text(title, body, is_source=is_source)
    if chunks:
        _insert_chunks(conn, item_id, chunks)

    # Derive entities from the (verbatim) tags + code_ref — GPU-free, so it runs
    # synchronously in-transaction without touching the 14B extractor (Phase 5,
    # Invariant 8). Same connection ⇒ commits atomically with the item.
    entities_mod.extract_entities_in(conn, item_id, project, tags or [], code_ref)
    return item_id


def find_duplicate(title: str, body: str, *, kind: str = "chat", uri: str = "") -> int | None:
    """Item id whose source matches this content's dedupe key, or None.

    Lets callers (MCP kb_save) point at the existing item when ingest reports
    a duplicate, instead of a dead-end 'nothing ingested'.
    """
    from kbcore import scope
    from kbcore.db import connect

    title_n, _ = scrub(normalize_title(title))
    body_n, _ = scrub(normalize(body))
    chash = content_hash(title_n, body_n)
    # Public-only: never hand back a non-public item's id — kb_save's dup path
    # would otherwise turn a content match into an id/existence oracle for
    # private items (review finding). A non-public dup → return None.
    gate = (
        "AND i.project IN (SELECT name FROM projects WHERE public IS TRUE)"
        if scope.public_only()
        else ""
    )
    with connect() as conn:
        row = conn.execute(
            f"""
            SELECT i.id FROM items i
            JOIN sources s ON s.id = i.source_id
            WHERE s.kind = %s AND s.uri = %s AND s.content_hash = %s {gate}
            ORDER BY i.id LIMIT 1
            """,  # noqa: S608 — gate is a constant fragment, no user data
            (kind, uri, chash),
        ).fetchone()
    return row[0] if row else None


def _insert_chunks(conn: psycopg.Connection, item_id: int, chunks: list[str]) -> None:
    """Embed and insert chunks, reusing embeddings for identical text (DATA-1)."""
    hashes = [hashlib.sha256(c.encode()).hexdigest() for c in chunks]
    known: dict[str, str] = {}
    rows = conn.execute(
        """
        SELECT DISTINCT ON (text_hash) text_hash, embedding::text FROM chunks
        WHERE text_hash = ANY(%s) AND embed_model = %s AND embedding IS NOT NULL
        """,
        (hashes, settings.kb_embed_model),
    ).fetchall()
    known = {h: e for h, e in rows}

    # Dedupe by hash before embedding: identical chunk texts embed once.
    missing: dict[str, str] = {}
    for c, h in zip(chunks, hashes, strict=True):
        if h not in known and h not in missing:
            missing[h] = c
    if missing:
        vectors = embed_mod.embed_texts(list(missing.values()))
        for h, vec in zip(missing.keys(), vectors, strict=True):
            known[h] = json.dumps(vec, separators=(",", ":"))

    conn.cursor().executemany(
        """
        INSERT INTO chunks (item_id, ord, text, text_hash, embedding, embed_model)
        VALUES (%s, %s, %s, %s, %s::vector, %s)
        """,
        [
            (item_id, ord_, text, h, known[h], settings.kb_embed_model)
            for ord_, (text, h) in enumerate(zip(chunks, hashes, strict=True))
        ],
    )


def archive_raw(project: str, raw: bytes) -> str:
    """Store the original bytes zstd-compressed under KB_RAW (OPS-4). Idempotent.

    Keyed by the hash of the ORIGINAL bytes, not the scrubbed content hash —
    two originals whose scrubbed bodies coincide must not overwrite each
    other's archive (review finding). Public so the inbox watcher can preserve
    a distinct original even when the item itself dedupes away (a re-drop whose
    raw bytes differ but scrub to an already-stored body).
    """
    directory = settings.raw / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{hashlib.sha256(raw).hexdigest()[:16]}.zst"
    if not path.exists():
        path.write_bytes(zstandard.ZstdCompressor(level=9).compress(raw))
    return str(path)

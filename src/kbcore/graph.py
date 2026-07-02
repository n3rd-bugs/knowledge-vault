"""kbcore.graph — relations, trace (BFS) and similar (nearest-neighbour) (§5.5).

Phase 1 shipped :func:`link` so real relations accumulate early (verdict V2);
Phase 5 adds :func:`trace` (BFS over the relation graph incl. supersession) and
:func:`similar` (lead-chunk cosine — the "have I hit this before?" primitive).
"""

from __future__ import annotations

from typing import Any

import psycopg

from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation, NotFound
from kbcore.models import RELATIONS, SimilarHit


def link(src_item_id: int, rel: str, dst_item_id: int, evidence_item: int | None = None) -> int:
    """Create ``src -rel-> dst`` between items. Returns the relation id (existing if dup)."""
    if rel not in RELATIONS:
        raise InvalidOperation(f"unknown relation {rel!r}; one of {', '.join(RELATIONS)}")
    with transaction() as conn:
        src = conn.execute("SELECT project FROM items WHERE id = %s", (src_item_id,)).fetchone()
        if src is None:
            raise NotFound(f"item {src_item_id} does not exist")
        dst = conn.execute("SELECT 1 FROM items WHERE id = %s", (dst_item_id,)).fetchone()
        if dst is None:
            raise NotFound(f"item {dst_item_id} does not exist")
        row = conn.execute(
            """
            INSERT INTO relations (project, src_kind, src_id, rel, dst_kind, dst_id, evidence_item)
            VALUES (%s, 'item', %s, %s, 'item', %s, %s)
            ON CONFLICT (src_kind, src_id, rel, dst_kind, dst_id) DO UPDATE
              SET evidence_item = COALESCE(relations.evidence_item, EXCLUDED.evidence_item)
            RETURNING id
            """,
            (src[0], src_item_id, rel, dst_item_id, evidence_item),
        ).fetchone()
    return row[0]


def _node_info(conn: psycopg.Connection, kind: str, node_id: int) -> dict[str, Any]:
    """Label fields for a trace node (id/kind/title/type/status/date)."""
    if kind == "item":
        row = conn.execute(
            "SELECT type, title, status, created_at FROM items WHERE id = %s", (node_id,)
        ).fetchone()
        if row is None:
            return {"id": node_id, "kind": "item", "title": "(missing)", "missing": True}
        return {
            "id": node_id,
            "kind": "item",
            "type": row[0],
            "title": row[1],
            "status": row[2],
            "date": row[3].date().isoformat(),
        }
    row = conn.execute(
        "SELECT name, kind, updated_at FROM entities WHERE id = %s", (node_id,)
    ).fetchone()
    if row is None:
        return {"id": node_id, "kind": "entity", "title": "(missing)", "missing": True}
    return {
        "id": node_id,
        "kind": "entity",
        "type": row[1],
        "title": row[0],
        "date": row[2].date().isoformat(),
    }


def _neighbors(
    conn: psycopg.Connection, kind: str, node_id: int, direction: str
) -> list[tuple[str, str, str, int]]:
    """(rel, direction, neighbor_kind, neighbor_id) edges of a node, ordered.

    Supersession is included automatically: supersede() records ``supersedes``
    edges in ``relations``, so walking relations covers the chain (§5.5).
    """
    edges: list[tuple[str, str, str, int]] = []
    if direction in ("both", "out"):
        for rel, dk, di in conn.execute(
            "SELECT rel, dst_kind, dst_id FROM relations "
            "WHERE src_kind = %s AND src_id = %s ORDER BY id",
            (kind, node_id),
        ).fetchall():
            edges.append((rel, "out", dk, di))
    if direction in ("both", "in"):
        for rel, sk, si in conn.execute(
            "SELECT rel, src_kind, src_id FROM relations "
            "WHERE dst_kind = %s AND dst_id = %s ORDER BY id",
            (kind, node_id),
        ).fetchall():
            edges.append((rel, "in", sk, si))
    return edges


def trace(node_kind: str, node_id: int, direction: str = "both", depth: int = 2) -> dict[str, Any]:
    """BFS the relation graph (item↔item and item↔entity) out to ``depth`` hops.

    Returns a nested tree ``{id, kind, type, title, status?, date?, edges:[{rel,
    direction, node}]}``. Cycles are cut with a visited set; a node reached again
    is shown without re-expanding its edges.
    """
    if node_kind not in ("item", "entity"):
        raise InvalidOperation(f"node_kind must be 'item' or 'entity', got {node_kind!r}")
    with connect() as conn:
        if _node_info(conn, node_kind, node_id).get("missing"):
            raise NotFound(f"{node_kind} {node_id} does not exist")
        visited: set[tuple[str, int]] = set()

        def build(kind: str, nid: int, remaining: int) -> dict[str, Any]:
            node = _node_info(conn, kind, nid)
            key = (kind, nid)
            if key in visited or remaining <= 0:
                node["edges"] = []
                return node
            visited.add(key)
            node["edges"] = [
                {"rel": rel, "direction": dirn, "node": build(nk, ni, remaining - 1)}
                for rel, dirn, nk, ni in _neighbors(conn, kind, nid, direction)
            ]
            return node

        return build(node_kind, node_id, depth)


def similar(item_id: int, k: int = 5) -> list[SimilarHit]:
    """Nearest ACTIVE items to ``item_id`` by lead-chunk cosine (current model).

    Excludes the item itself and any duplicate/superseded/archived item (only
    ``status='active'``). The "have I hit this bug before?" primitive — for a
    bug, its fix should surface near the top.
    """
    with connect() as conn:
        # Validate existence first so a stale/guessed id raises a clean NotFound
        # for every caller — otherwise the tool's later log_access() trips the
        # access_log FK with a raw psycopg error (review finding). An item that
        # exists but has no lead embedding legitimately returns [].
        if conn.execute("SELECT 1 FROM items WHERE id = %s", (item_id,)).fetchone() is None:
            raise NotFound(f"item {item_id} does not exist")
        base = conn.execute(
            "SELECT embedding::text FROM chunks "
            "WHERE item_id = %s AND ord = 0 AND embed_model = %s",
            (item_id, settings.kb_embed_model),
        ).fetchone()
        if base is None or base[0] is None:
            return []
        rows = conn.execute(
            """
            SELECT i.id, i.title, i.type, i.project, 1 - (c.embedding <=> %(v)s::vector) AS score
            FROM chunks c JOIN items i ON i.id = c.item_id
            WHERE c.ord = 0 AND c.embed_model = %(model)s AND c.embedding IS NOT NULL
              AND i.status = 'active' AND i.id <> %(self)s
            ORDER BY c.embedding <=> %(v)s::vector
            LIMIT %(k)s
            """,
            {"v": base[0], "model": settings.kb_embed_model, "self": item_id, "k": k},
        ).fetchall()
    return [
        SimilarHit(item_id=r[0], title=r[1], type=r[2], project=r[3], score=float(r[4]))
        for r in rows
    ]

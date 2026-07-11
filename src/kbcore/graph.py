"""kbcore.graph — relations, trace (BFS) and similar (nearest-neighbour) (§5.5).

Phase 1 shipped :func:`link` so real relations accumulate early (verdict V2);
Phase 5 adds :func:`trace` (BFS over the relation graph incl. supersession) and
:func:`similar` (lead-chunk cosine — the "have I hit this before?" primitive).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg

from kbcore import scope
from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation, NotFound
from kbcore.models import RELATIONS, SimilarHit

# Total distinct neighbors one search expansion may visit before BFS stops —
# bounds the N+1 traversal cost when a consolidation summary hub makes a whole
# component mutually reachable (review finding). hops is already capped at 3.
EXPAND_MAX_VISITED = 500


def link(src_item_id: int, rel: str, dst_item_id: int, evidence_item: int | None = None) -> int:
    """Create ``src -rel-> dst`` between items. Returns the relation id (existing if dup)."""
    if rel not in RELATIONS:
        raise InvalidOperation(f"unknown relation {rel!r}; one of {', '.join(RELATIONS)}")
    with transaction() as conn:
        src = conn.execute("SELECT project FROM items WHERE id = %s", (src_item_id,)).fetchone()
        if src is None:
            raise NotFound(f"item {src_item_id} does not exist")
        # Public-only instance: cannot write a relation from a non-public item.
        scope.assert_writable(src[0])
        dst = conn.execute("SELECT project FROM items WHERE id = %s", (dst_item_id,)).fetchone()
        # A dst the caller cannot read is indistinguishable from a missing one
        # (no id oracle) — walls off both public-only and grantee scope.
        if dst is None or not scope.project_readable(conn, dst[0]):
            raise NotFound(f"item {dst_item_id} does not exist")
        # Same gate for evidence_item — else its FK to items() is an existence
        # oracle for arbitrary (incl. withheld) ids (review finding).
        if evidence_item is not None:
            ev = conn.execute(
                "SELECT project FROM items WHERE id = %s", (evidence_item,)
            ).fetchone()
            if ev is None or not scope.project_readable(conn, ev[0]):
                raise NotFound(f"item {evidence_item} does not exist")
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
    """Label fields for a trace node (id/kind/title/type/status/date/project)."""
    if kind == "item":
        row = conn.execute(
            "SELECT type, title, status, created_at, project FROM items WHERE id = %s", (node_id,)
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
            "project": row[4],
        }
    row = conn.execute(
        "SELECT name, kind, updated_at, project FROM entities WHERE id = %s", (node_id,)
    ).fetchone()
    if row is None:
        return {"id": node_id, "kind": "entity", "title": "(missing)", "missing": True}
    return {
        "id": node_id,
        "kind": "entity",
        "type": row[1],
        "title": row[0],
        "date": row[2].date().isoformat(),
        "project": row[3],
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


def _neighbors_batch(
    conn: psycopg.Connection, kind: str, node_ids: list[int], direction: str
) -> dict[int, list[tuple[str, str, str, int]]]:
    """Level-batched :func:`_neighbors`: ONE query per direction over the whole
    frontier via ``= ANY(...)``, instead of two per node (finding — a BFS level of
    N nodes issued 2N sequential edge queries). Returns ``{node_id: edges}``.
    """
    out: dict[int, list[tuple[str, str, str, int]]] = {nid: [] for nid in node_ids}
    if not node_ids:
        return out
    if direction in ("both", "out"):
        for src_id, rel, dk, di in conn.execute(
            "SELECT src_id, rel, dst_kind, dst_id FROM relations "
            "WHERE src_kind = %s AND src_id = ANY(%s) ORDER BY src_id, id",
            (kind, node_ids),
        ).fetchall():
            out[src_id].append((rel, "out", dk, di))
    if direction in ("both", "in"):
        for dst_id, rel, sk, si in conn.execute(
            "SELECT dst_id, rel, src_kind, src_id FROM relations "
            "WHERE dst_kind = %s AND dst_id = ANY(%s) ORDER BY dst_id, id",
            (kind, node_ids),
        ).fetchall():
            out[dst_id].append((rel, "in", sk, si))
    return out


def expand_items(
    conn: psycopg.Connection, seed_ids: list[int], hops: int, direction: str = "both"
) -> dict[int, int]:
    """BFS the ITEM↔ITEM relation graph from ``seed_ids`` out to ``hops`` edges.

    Returns ``{neighbor_item_id: min_hop_distance}`` for every item reachable
    within ``hops`` that is NOT itself a seed. Entity nodes are ignored (item
    edges only — ``neighbor_kind == 'item'``). Cycle-safe via a visited set;
    level-by-level so the recorded distance is the minimum. WP2 graph-traversal
    retrieval expansion — the caller (search) re-checks scope/status/valid-time
    on the survivors before surfacing them.
    """
    if hops <= 0 or not seed_ids:
        return {}
    visited: set[int] = set(seed_ids)
    reached: dict[int, int] = {}
    frontier: set[int] = set(seed_ids)
    for dist in range(1, hops + 1):
        nxt: set[int] = set()
        for node_id in frontier:
            for _rel, _dirn, neighbor_kind, neighbor_id in _neighbors(
                conn, "item", node_id, direction
            ):
                if neighbor_kind != "item" or neighbor_id in visited:
                    continue
                visited.add(neighbor_id)  # first (=shortest) reach wins the distance
                reached[neighbor_id] = dist
                nxt.add(neighbor_id)
        if not nxt:
            break
        frontier = nxt
    return reached


def expand_items_scored(
    conn: psycopg.Connection,
    seeds: dict[int, float],
    hops: int,
    decay: float,
    scope_ok: Callable[[list[int]], set[int]],
    direction: str = "both",
    max_visited: int = EXPAND_MAX_VISITED,
) -> dict[int, tuple[float, int]]:
    """BFS the ITEM↔ITEM graph from scored ``seeds``; return per-neighbor
    ``(score, origin_seed_id)``.

    A neighbor's score is the MAX over the seeds reaching it of
    ``seed_score * decay**hop`` — so a neighbor is scored by the seed that actually
    vouches for it (not by the global best hit), and always ranks strictly below
    that seed since ``decay < 1``. ``origin_seed_id`` is that vouching seed, threaded
    out so the caller can re-impose the guarantee AFTER bonuses (finding). Seeds
    themselves are never returned.

    ``scope_ok(ids)`` returns the subset passing the search scope; it is applied to
    EACH frontier level so a withheld (non-public / out-of-scope / superseded /
    as_of-invalid) node is neither surfaced NOR used as a relay hop — parity with
    :func:`trace`'s walling (a private item can no longer bridge two public ones).
    Cycle-safe (a node re-expands only when a better vouch reaches it, and
    ``decay < 1`` makes that terminate); ``max_visited`` bounds total breadth. Each
    BFS level's edges are fetched in one batched query per direction (finding).
    """
    if hops <= 0 or not seeds:
        return {}
    seed_ids = set(seeds)
    # vouch: (best seed_score*decay**hop, vouching seed) reaching each non-seed node.
    vouch: dict[int, tuple[float, int]] = {}
    # node -> (vouch AT that node, origin seed); seeds vouch for themselves.
    frontier: dict[int, tuple[float, int]] = {sid: (score, sid) for sid, score in seeds.items()}
    scoped: dict[int, bool] = {}  # scope-check cache (never re-query a known id)
    for _dist in range(hops):
        cand: dict[int, tuple[float, int]] = {}
        batch = _neighbors_batch(conn, "item", list(frontier), direction)
        for node_id, (node_vouch, origin) in frontier.items():
            for _rel, _dirn, neighbor_kind, neighbor_id in batch.get(node_id, ()):
                if neighbor_kind != "item" or neighbor_id in seed_ids:
                    continue
                c = node_vouch * decay
                if c > cand.get(neighbor_id, (0.0, 0))[0]:
                    cand[neighbor_id] = (c, origin)  # inherit the vouching seed
        unknown = [nid for nid in cand if nid not in scoped]
        if unknown:
            ok = scope_ok(unknown)
            for nid in unknown:
                scoped[nid] = nid in ok
        next_frontier: dict[int, tuple[float, int]] = {}
        for nid, (c, origin) in cand.items():
            if not scoped.get(nid):  # withheld: never surfaced, never relayed
                continue
            if c > vouch.get(nid, (0.0, 0))[0]:
                vouch[nid] = (c, origin)
                next_frontier[nid] = (c, origin)
                if len(vouch) >= max_visited:
                    return vouch
        if not next_frontier:
            break
        frontier = next_frontier
    return vouch


def trace(node_kind: str, node_id: int, direction: str = "both", depth: int = 2) -> dict[str, Any]:
    """BFS the relation graph (item↔item and item↔entity) out to ``depth`` hops.

    Returns a nested tree ``{id, kind, type, title, status?, date?, edges:[{rel,
    direction, node}]}``. Cycles are cut with a visited set; a node reached again
    is shown without re-expanding its edges.
    """
    if node_kind not in ("item", "entity"):
        raise InvalidOperation(f"node_kind must be 'item' or 'entity', got {node_kind!r}")
    with connect() as conn:
        read_cache: dict[str, bool] = {}

        def is_readable(project: str | None) -> bool:
            if not project:
                return False
            if project not in read_cache:
                read_cache[project] = scope.project_readable(conn, project)
            return read_cache[project]

        root = _node_info(conn, node_kind, node_id)
        if root.get("missing") or not is_readable(root.get("project")):
            # Withheld (out-of-scope) roots are indistinguishable from missing.
            raise NotFound(f"{node_kind} {node_id} does not exist")
        visited: set[tuple[str, int]] = set()

        def build(node: dict[str, Any], remaining: int) -> dict[str, Any]:
            key = (node["kind"], node["id"])
            if key in visited or remaining <= 0:
                node["edges"] = []
                return node
            visited.add(key)
            edges = []
            for rel, dirn, nk, ni in _neighbors(conn, node["kind"], node["id"], direction):
                child = _node_info(conn, nk, ni)
                # Never traverse into or reveal an out-of-scope node — a withheld
                # item can bridge neither two public ones nor two granted ones.
                if child.get("missing") or not is_readable(child.get("project")):
                    continue
                edges.append({"rel": rel, "direction": dirn, "node": build(child, remaining - 1)})
            node["edges"] = edges
            return node

        return build(root, depth)


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
        anchor = conn.execute("SELECT project FROM items WHERE id = %s", (item_id,)).fetchone()
        if anchor is None:
            raise NotFound(f"item {item_id} does not exist")
        # Public-only: a withheld anchor is indistinguishable from missing.
        scope.assert_readable(anchor[0], f"item {item_id} does not exist")
        base = conn.execute(
            "SELECT embedding::text FROM chunks "
            "WHERE item_id = %s AND ord = 0 AND embed_model = %s",
            (item_id, settings.kb_embed_model),
        ).fetchone()
        if base is None or base[0] is None:
            return []
        # Visibility wall (public-only and/or grantee): results never surface an
        # item the caller cannot read. The value rides the `vis` named param.
        vis_frag, vis_named = scope.project_predicate_named("i.project")
        vis_where = f"AND {vis_frag}" if vis_frag else ""
        rows = conn.execute(
            f"""
            SELECT i.id, i.title, i.type, i.project, 1 - (c.embedding <=> %(v)s::vector) AS score
            FROM chunks c JOIN items i ON i.id = c.item_id
            WHERE c.ord = 0 AND c.embed_model = %(model)s AND c.embedding IS NOT NULL
              AND i.status = 'active' AND i.id <> %(self)s {vis_where}
            ORDER BY c.embedding <=> %(v)s::vector
            LIMIT %(k)s
            """,  # noqa: S608 — vis_where is a constant fragment; values ride params
            {"v": base[0], "model": settings.kb_embed_model, "self": item_id, "k": k, **vis_named},
        ).fetchall()
    return [
        SimilarHit(item_id=r[0], title=r[1], type=r[2], project=r[3], score=float(r[4]))
        for r in rows
    ]

"""kbcore.browse — READ-ONLY browse queries for the web UI (and future CLI use).

Everything here is public-scope-gated (Phase 8 allow-list): under a public-only
instance, withheld rows are simply absent from results — never an error, never
an id echo — so these functions leak no existence signal. No writes live here.
"""

from __future__ import annotations

from collections.abc import Sequence

from kbcore import scope
from kbcore.db import connect
from kbcore.models import ItemRef


def _gate(column: str) -> str:
    """AND-prefixed public gate for embedding in a WHERE clause, or ''."""
    pred = scope.project_predicate(column)
    return f"AND {pred}" if pred else ""


def item_refs(ids: Sequence[int]) -> dict[int, ItemRef]:
    """Light refs (title/type/status) for a set of item ids, in one query.

    Withheld (non-public) or missing ids are absent from the dict — callers
    render a neutral fallback ("id N") without learning which case it was.
    """
    unique = sorted({int(i) for i in ids})
    if not unique:
        return {}
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, title, type, status FROM items
            WHERE id = ANY(%s) {_gate("project")}
            """,  # noqa: S608 — gate is a constant fragment, no user data
            (unique,),
        ).fetchall()
    return {r[0]: ItemRef(item_id=r[0], title=r[1], type=r[2], status=r[3]) for r in rows}


def list_tags(project: str | None = None, limit: int = 100) -> list[tuple[str, int]]:
    """Most-used tags over ACTIVE items, optionally within one project."""
    limit = max(1, min(int(limit), 500))
    where = "WHERE status = 'active'"
    params: list[object] = []
    if project:
        where += " AND project = %s"
        params.append(project)
    gate = _gate("project")
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT tag, count(*) AS n
            FROM items, unnest(tags) AS tag
            {where} {gate}
            GROUP BY tag ORDER BY n DESC, tag LIMIT %s
            """,  # noqa: S608 — where/gate are constant fragments
            (*params, limit),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def items_with_tag(tag: str, project: str | None = None, limit: int = 200) -> list[ItemRef]:
    """Items carrying ``tag`` (active first, newest first), public-scope-gated."""
    limit = max(1, min(int(limit), 500))
    where = "WHERE %s = ANY(tags)"
    params: list[object] = [tag]
    if project:
        where += " AND project = %s"
        params.append(project)
    gate = _gate("project")
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, title, type, status FROM items
            {where} {gate}
            ORDER BY (status = 'active') DESC, created_at DESC
            LIMIT %s
            """,  # noqa: S608 — where/gate are constant fragments
            (*params, limit),
        ).fetchall()
    return [ItemRef(item_id=r[0], title=r[1], type=r[2], status=r[3]) for r in rows]

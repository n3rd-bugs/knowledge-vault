"""kbcore.items — read-side item/project queries (kb_get / kb_projects backing)."""

from __future__ import annotations

from kbcore.db import connect, transaction
from kbcore.errors import NotFound
from kbcore.models import CodeRef, ItemDetail, ProjectInfo, Relation


def get_item(item_id: int, with_relations: bool = True) -> ItemDetail:
    """Full item body + relations + supersession pointer (PLAN.md §6 kb_get)."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT i.id, i.project, i.type, i.title, i.body, i.tags, i.status,
                   s.trust, i.superseded_by, i.supersede_reason, i.meta, s.uri, i.created_at
            FROM items i LEFT JOIN sources s ON s.id = i.source_id
            WHERE i.id = %s
            """,
            (item_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"item {item_id} does not exist")
        (
            id_,
            project,
            type_,
            title,
            body,
            tags,
            status,
            trust,
            sup_by,
            sup_reason,
            meta,
            uri,
            created,
        ) = row
        relations: list[Relation] = []
        if with_relations:
            rel_rows = conn.execute(
                """
                SELECT src_id, rel, dst_id,
                       CASE WHEN src_id = %(id)s AND src_kind = 'item' THEN 'out' ELSE 'in' END
                FROM relations
                WHERE (src_kind = 'item' AND src_id = %(id)s)
                   OR (dst_kind = 'item' AND dst_id = %(id)s)
                ORDER BY id
                """,
                {"id": item_id},
            ).fetchall()
            relations = [
                Relation(src_id=s, rel=r, dst_id=d, direction=direc) for s, r, d, direc in rel_rows
            ]
    meta = meta if isinstance(meta, dict) else {}
    code_ref = CodeRef(**meta["code_ref"]) if meta.get("code_ref") else None
    return ItemDetail(
        item_id=id_,
        project=project,
        type=type_,
        title=title,
        body=body,
        tags=tags or [],
        status=status,
        trust=trust or "verified",
        superseded_by=sup_by,
        supersede_reason=sup_reason,
        code_ref=code_ref,
        meta=meta,
        source_uri=uri or None,
        created_at=created,
        relations=relations,
    )


def find_by_title(title: str) -> int | None:
    """Oldest item id with exactly this title, or None (seed relation wiring)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM items WHERE title = %s ORDER BY id LIMIT 1", (title,)
        ).fetchone()
    return row[0] if row else None


def list_projects() -> list[ProjectInfo]:
    """Project registry with item counts and last activity (kb_projects)."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT p.name, p.domain, p.repos, p.description, p.archived,
                   count(i.id), max(i.created_at)
            FROM projects p LEFT JOIN items i ON i.project = p.name
            GROUP BY p.name, p.domain, p.repos, p.description, p.archived
            ORDER BY max(i.created_at) DESC NULLS LAST
            """
        ).fetchall()
    return [
        ProjectInfo(
            name=n,
            domain=d,
            repos=list(r or []),
            description=desc,
            archived=a,
            item_count=c,
            last_activity=last,
        )
        for n, d, r, desc, a, c, last in rows
    ]


def set_project(
    name: str,
    domain: str | None = None,
    repos: list[str] | None = None,
    description: str | None = None,
) -> None:
    """Upsert registry metadata for a project (DATA-8)."""
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO projects (name, domain, repos, description)
            VALUES (%s, %s, COALESCE(%s::text[], '{}'), %s)
            ON CONFLICT (name) DO UPDATE SET
              domain = COALESCE(EXCLUDED.domain, projects.domain),
              repos = CASE WHEN %s THEN EXCLUDED.repos ELSE projects.repos END,
              description = COALESCE(EXCLUDED.description, projects.description)
            """,
            (name, domain, repos, description, repos is not None),
        )

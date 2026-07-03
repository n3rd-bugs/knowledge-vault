"""kbcore.items — read-side item/project queries (kb_get / kb_projects backing)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from kbcore import scope
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation, NotFound
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
        # Public-only instance: a withheld item is indistinguishable from missing.
        scope.assert_readable(row[1], f"item {item_id} does not exist")
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
            # Public-only: drop relations whose OTHER endpoint is an item in a
            # non-public project — otherwise a public item's relation list would
            # expose a private item's id (id oracle). Entity endpoints are kept.
            hide = (
                """
                AND NOT EXISTS (
                    SELECT 1 FROM items o WHERE o.id = CASE WHEN src_id = %(id)s THEN dst_id
                                                           ELSE src_id END
                      AND (CASE WHEN src_id = %(id)s THEN dst_kind ELSE src_kind END) = 'item'
                      AND o.project NOT IN (SELECT name FROM projects WHERE public IS TRUE)
                )
                """
                if scope.public_only()
                else ""
            )
            rel_rows = conn.execute(
                f"""
                SELECT src_id, rel, dst_id,
                       CASE WHEN src_id = %(id)s AND src_kind = 'item' THEN 'out' ELSE 'in' END
                FROM relations
                WHERE ((src_kind = 'item' AND src_id = %(id)s)
                    OR (dst_kind = 'item' AND dst_id = %(id)s)) {hide}
                ORDER BY id
                """,  # noqa: S608 — `hide` is a constant fragment, no user data
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


MAX_TIMELINE_LIMIT = 500


def _parse_day(value: str, param: str) -> date:
    """Strict YYYY-MM-DD validation so garbage input raises a domain error,
    not a raw psycopg error escaping to MCP clients (review finding)."""
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise InvalidOperation(f"{param} must be YYYY-MM-DD, got {value!r}") from e


def timeline(
    project: str,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 50,
) -> list[ItemDetail]:
    """Chronological items for a project, superseded/archived included (kb_timeline).

    Dates are whole days, both ends inclusive (to_date=today includes today —
    review finding: `<= 'YYYY-MM-DD'` casts to midnight and dropped the day).
    When more than `limit` items match, the MOST RECENT `limit` are returned,
    still in ascending order — recent evolution must not be invisible.
    """
    # Public-only instance: a non-public (or absent) project must look absent.
    scope.assert_readable(project, f"project {project!r} does not exist")
    limit = max(1, min(int(limit), MAX_TIMELINE_LIMIT))
    where = ["i.project = %s"]
    params: list = [project]
    if from_date:
        where.append("i.created_at >= %s")
        params.append(_parse_day(from_date, "from_date"))
    if to_date:
        where.append("i.created_at < %s")
        params.append(_parse_day(to_date, "to_date") + timedelta(days=1))
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM (
                SELECT i.id, i.project, i.type, i.title, i.body, i.tags, i.status,
                       s.trust, i.superseded_by, i.supersede_reason, i.meta,
                       s.uri, i.created_at
                FROM items i LEFT JOIN sources s ON s.id = i.source_id
                WHERE {" AND ".join(where)}
                ORDER BY i.created_at DESC, i.id DESC
                LIMIT %s
            ) recent ORDER BY created_at ASC, id ASC
            """,  # noqa: S608 — WHERE fragments above are constant literals
            params,
        ).fetchall()
    out = []
    for id_, proj, type_, title, body, tags, status, trust, sup, reason, meta, uri, created in rows:
        meta = meta if isinstance(meta, dict) else {}
        code_ref = CodeRef(**meta["code_ref"]) if meta.get("code_ref") else None
        out.append(
            ItemDetail(
                item_id=id_,
                project=proj,
                type=type_,
                title=title,
                body=body,
                tags=tags or [],
                status=status,
                trust=trust or "verified",
                superseded_by=sup,
                supersede_reason=reason,
                code_ref=code_ref,
                meta=meta,
                source_uri=uri or None,
                created_at=created,
            )
        )
    return out


def log_access(item_id: int, tool: str, query: str | None = None) -> None:
    """Record a direct item access (kb_get / kb get) for staleness scoring."""
    with transaction() as conn:
        conn.execute(
            "INSERT INTO access_log (item_id, tool, query) VALUES (%s, %s, %s)",
            (item_id, tool, query),
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
    where = "WHERE p.public IS TRUE" if scope.public_only() else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT p.name, p.domain, p.repos, p.description, p.archived, p.public,
                   count(i.id), max(i.created_at)
            FROM projects p LEFT JOIN items i ON i.project = p.name
            {where}
            GROUP BY p.name, p.domain, p.repos, p.description, p.archived, p.public
            ORDER BY max(i.created_at) DESC NULLS LAST
            """  # noqa: S608 — `where` is a constant literal, not user input
        ).fetchall()
    return [
        ProjectInfo(
            name=n,
            domain=d,
            repos=list(r or []),
            description=desc,
            archived=a,
            public=pub,
            item_count=c,
            last_activity=last,
        )
        for n, d, r, desc, a, pub, c, last in rows
    ]


def _canonical_repo(path: str | Path) -> str:
    """Normalize a repo path for comparison: strip trailing slash, expand ~, resolve."""
    p = Path(str(path).rstrip("/\\")).expanduser()
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


def project_for_repo(path: str | Path) -> str | None:
    """Registry project mapped to a local repo path via ``projects.repos`` (§7.5).

    Both sides are canonicalized (trailing slash / ``~`` / symlink alias) before
    comparison so a repo stored as ``~/work/x`` or ``x/`` still maps a resolved
    input path (review finding — one-sided normalization split a project).
    Returns None when no registry row claims the repo; the caller then falls
    back to the repo dir name.
    """
    target = _canonical_repo(path)
    with connect() as conn:
        rows = conn.execute(
            "SELECT name, repos FROM projects WHERE array_length(repos, 1) > 0 ORDER BY name"
        ).fetchall()
    for name, repos in rows:
        if any(_canonical_repo(stored) == target for stored in (repos or [])):
            return name
    return None


def set_project(
    name: str,
    domain: str | None = None,
    repos: list[str] | None = None,
    description: str | None = None,
    public: bool | None = None,
) -> None:
    """Upsert registry metadata for a project (DATA-8). ``public`` flips the
    public/cloud allow-list flag only when given (None leaves it unchanged)."""
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO projects (name, domain, repos, description, public)
            VALUES (%s, %s, COALESCE(%s::text[], '{}'), %s, COALESCE(%s, false))
            ON CONFLICT (name) DO UPDATE SET
              domain = COALESCE(EXCLUDED.domain, projects.domain),
              repos = CASE WHEN %s THEN EXCLUDED.repos ELSE projects.repos END,
              description = COALESCE(EXCLUDED.description, projects.description),
              public = CASE WHEN %s THEN EXCLUDED.public ELSE projects.public END
            """,
            (name, domain, repos, description, public, repos is not None, public is not None),
        )

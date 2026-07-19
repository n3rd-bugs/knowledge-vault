"""kbcore.items — read-side item/project queries (kb_get / kb_projects backing)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
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
                   s.trust, i.superseded_by, i.supersede_reason, i.meta, s.uri, i.created_at,
                   i.valid_from, i.valid_to
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
            valid_from,
            valid_to,
        ) = row
        relations: list[Relation] = []
        if with_relations:
            # Drop relations whose OTHER endpoint is an item in a project this
            # request cannot read (public-only and/or grantee) — otherwise a visible
            # item's relation list would expose a withheld item's id (id oracle).
            # Entity endpoints are kept. The value rides the `vis` named param.
            hidden_frag, hide_params = scope.hidden_predicate_named("o.project")
            hide = (
                f"""
                AND NOT EXISTS (
                    SELECT 1 FROM items o WHERE o.id = CASE WHEN src_id = %(id)s THEN dst_id
                                                           ELSE src_id END
                      AND (CASE WHEN src_id = %(id)s THEN dst_kind ELSE src_kind END) = 'item'
                      AND ({hidden_frag})
                )
                """
                if hidden_frag
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
                """,  # noqa: S608 — `hide` is built from constant fragments; values ride params
                {"id": item_id, **hide_params},
            ).fetchall()
            relations = [
                Relation(src_id=s, rel=r, dst_id=d, direction=direc) for s, r, d, direc in rel_rows
            ]
        # Denormalized supersession pointer: if the successor lives in a project this
        # request cannot read, NULL both the id AND the free-text reason — else the
        # scalar leaks a withheld item's existence + owner text past the relations wall
        # above (the reciprocal 'supersedes' edge is already hidden). Owner sees all.
        if sup_by is not None and not scope.readable_item_ids(conn, [sup_by]):
            sup_by = None
            sup_reason = None
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
        valid_from=valid_from,
        valid_to=valid_to,
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


def parse_instant(value: str, param: str) -> datetime:
    """Parse a full ISO-8601 timestamp OR a bare date (→ midnight) into a tz-aware
    UTC datetime — the valid-time as_of / valid_from / valid_to parser (WP1).

    A bare date maps to that day at 00:00 UTC; a naive timestamp is assumed UTC
    (Invariant 7). Garbage raises a domain error instead of letting a raw
    ValueError/psycopg error escape to MCP clients (mirrors _parse_day). Public
    because kbcli/kbmcp/kbrest all parse instants across the package boundary."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = datetime.combine(date.fromisoformat(value), datetime.min.time())
        except ValueError as e:
            raise InvalidOperation(
                f"{param} must be an ISO-8601 date or timestamp, got {value!r}"
            ) from e
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# Back-compat private alias (pre-WP1-patch callers/tests imported the underscore).
_parse_instant = parse_instant


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
                       s.uri, i.created_at, i.valid_from, i.valid_to
                FROM items i LEFT JOIN sources s ON s.id = i.source_id
                WHERE {" AND ".join(where)}
                ORDER BY i.created_at DESC, i.id DESC
                LIMIT %s
            ) recent ORDER BY created_at ASC, id ASC
            """,  # noqa: S608 — WHERE fragments above are constant literals
            params,
        ).fetchall()
        # Batch-mask superseded_by pointers into withheld projects (one scoped lookup
        # for the whole page) — same wall as get_item; owner sees everything.
        readable_sup = scope.readable_item_ids(conn, [r[8] for r in rows if r[8] is not None])
    out = []
    for (
        id_,
        proj,
        type_,
        title,
        body,
        tags,
        status,
        trust,
        sup,
        reason,
        meta,
        uri,
        created,
        valid_from,
        valid_to,
    ) in rows:
        meta = meta if isinstance(meta, dict) else {}
        code_ref = CodeRef(**meta["code_ref"]) if meta.get("code_ref") else None
        if sup is not None and sup not in readable_sup:
            sup = None  # withheld successor: byte-identical to a non-superseded row
            reason = None
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
                valid_from=valid_from,
                valid_to=valid_to,
            )
        )
    return out


def log_access(item_id: int, tool: str, query: str | None = None) -> None:
    """Record a direct item access (kb_get / kb get) for staleness scoring."""
    p = scope.current()
    pname = None if p.is_owner else p.name  # audit: grantee name; NULL for owner
    with transaction() as conn:
        conn.execute(
            "INSERT INTO access_log (item_id, tool, query, principal) VALUES (%s, %s, %s, %s)",
            (item_id, tool, query, pname),
        )


def archive_item(item_id: int, *, reason: str | None = None) -> bool:
    """Owner maintenance: flip an ACTIVE item to ``status='archived'`` (Invariant 5).

    A status flip, never a delete — the row, body, and any supersession trail
    are preserved and the flip is reversible. Idempotent: an already-non-active
    item returns False without churn. ``reason`` is recorded in ``meta`` for
    audit. NotFound if the id does not exist. Owner-only surfaces call this for
    one-off cleanup (retiring test fixtures, vendor-scan noise) that the
    date-based nightly ``archive_stale`` never reaches.
    """
    with transaction() as conn:
        row = conn.execute("SELECT status FROM items WHERE id = %s", (item_id,)).fetchone()
        if row is None:
            raise NotFound(f"item {item_id} does not exist")
        if row[0] != "active":
            return False
        conn.execute(
            """
            UPDATE items
            SET status = 'archived', updated_at = now(),
                meta = COALESCE(meta, '{}'::jsonb) || jsonb_build_object(
                    'archived', jsonb_build_object(
                        'at', %s::text, 'reason', %s::text))
            WHERE id = %s AND status = 'active'
            """,
            (datetime.now(UTC).isoformat(), reason, item_id),
        )
    return True


def find_by_title(title: str) -> int | None:
    """Oldest item id with exactly this title, or None (seed relation wiring)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM items WHERE title = %s ORDER BY id LIMIT 1", (title,)
        ).fetchone()
    return row[0] if row else None


def list_projects() -> list[ProjectInfo]:
    """Project registry with item counts and last activity (kb_projects).

    A grantee (and/or a public-only instance) enumerates ONLY the projects it may
    read — a withheld project is simply absent, never counted, no volume leak."""
    vis_frag, vis_params = scope.project_predicate("p.name")
    where = f"WHERE {vis_frag}" if vis_frag else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT p.name, p.domain, p.repos, p.description, p.archived, p.public,
                   count(i.id), max(i.created_at)
            FROM projects p LEFT JOIN items i ON i.project = p.name
            {where}
            GROUP BY p.name, p.domain, p.repos, p.description, p.archived, p.public
            ORDER BY max(i.created_at) DESC NULLS LAST
            """,  # noqa: S608 — `where` is a constant fragment; values ride params
            vis_params,
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

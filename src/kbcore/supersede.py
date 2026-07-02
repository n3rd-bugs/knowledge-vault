"""kbcore.supersede — supersession without loss (DATA-2, PLAN.md §5.5).

Old items are never edited or deleted: the old row flips to
``status='superseded'``, points at its replacement, and a ``supersedes``
relation records the edge (Invariant 1/5).
"""

from __future__ import annotations

from kbcore.db import transaction
from kbcore.errors import DuplicateContent, InvalidOperation, NotFound
from kbcore.ingest import ingest_in


def supersede(
    old_id: int,
    *,
    new_item_id: int | None = None,
    new_body: str | None = None,
    reason: str,
) -> int:
    """Supersede *old_id* with an existing item or a new body. Returns the new item id."""
    if (new_item_id is None) == (new_body is None):
        raise InvalidOperation("pass exactly one of new_item_id or new_body")

    with transaction() as conn:
        row = conn.execute(
            "SELECT project, type, title, tags, status, meta FROM items WHERE id = %s", (old_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"item {old_id} does not exist")
        project, type_, title, tags, status, old_meta = row
        if status == "superseded":
            raise InvalidOperation(f"item {old_id} is already superseded")

        if new_body is not None:
            # Carry knowledge anchors (code_ref etc.) onto the replacement;
            # redaction counts belong to the old body, not the new one.
            carried = {k: v for k, v in (old_meta or {}).items() if k != "redactions"}
            new_item_id = ingest_in(
                conn,
                project,
                type_,
                title,
                new_body,
                kind="manual",
                trust="verified",
                tags=tags,
                meta=carried or None,
            )
            if new_item_id is None:
                raise DuplicateContent(
                    "replacement body duplicates existing content; pass new_item_id instead"
                )
        else:
            repl = conn.execute("SELECT status FROM items WHERE id = %s", (new_item_id,)).fetchone()
            if repl is None:
                raise NotFound(f"replacement item {new_item_id} does not exist")
            if repl[0] != "active":
                # Guards against supersession cycles and chains that end in a
                # non-active successor (review finding).
                raise InvalidOperation(
                    f"replacement item {new_item_id} is {repl[0]}; must be active"
                )
        if new_item_id == old_id:
            raise InvalidOperation("an item cannot supersede itself")

        conn.execute(
            """
            UPDATE items
            SET status = 'superseded', superseded_by = %s, supersede_reason = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (new_item_id, reason, old_id),
        )
        conn.execute(
            """
            INSERT INTO relations (project, src_kind, src_id, rel, dst_kind, dst_id)
            VALUES (%s, 'item', %s, 'supersedes', 'item', %s)
            ON CONFLICT DO NOTHING
            """,
            (project, new_item_id, old_id),
        )
    return new_item_id

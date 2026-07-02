"""kbcore.graph — manual relations between items (PLAN.md §5.5, DATA-10).

Phase 1 ships :func:`link` so real relations (``fixes``, ``alternative_to``, …)
accumulate early (verdict V2). ``trace``/``similar`` arrive in Phase 5.
"""

from __future__ import annotations

from kbcore.db import transaction
from kbcore.errors import InvalidOperation, NotFound
from kbcore.models import RELATIONS


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

"""kbcore.reembed — re-embed chunks after an embedder swap (DATA-3, PLAN.md §7.4).

Chunks whose ``embed_model`` differs from the configured model are re-embedded
in batches. Bodies/text are untouched (Invariant 1) — only the vector and
model tag change.
"""

from __future__ import annotations

import json

from kbcore import embed as embed_mod
from kbcore.config import settings
from kbcore.db import transaction

BATCH = 64


def reembed() -> int:
    """Re-embed stale chunks. Returns how many chunks were updated."""
    updated = 0
    while True:
        with transaction() as conn:
            rows = conn.execute(
                """
                SELECT id, text FROM chunks
                WHERE embed_model IS DISTINCT FROM %s
                ORDER BY id LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (settings.kb_embed_model, BATCH),
            ).fetchall()
            if not rows:
                return updated
            vectors = embed_mod.embed_texts([t for _, t in rows])
            conn.cursor().executemany(
                "UPDATE chunks SET embedding = %s::vector, embed_model = %s WHERE id = %s",
                [
                    (json.dumps(v, separators=(",", ":")), settings.kb_embed_model, cid)
                    for (cid, _), v in zip(rows, vectors, strict=True)
                ],
            )
            updated += len(rows)

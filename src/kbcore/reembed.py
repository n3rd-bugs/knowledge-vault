"""kbcore.reembed — re-embed chunks after an embedder swap (DATA-3, PLAN.md §7.4).

Chunks whose ``embed_model`` differs from the configured model are re-embedded
in batches. Bodies/text are untouched (Invariant 1) — only the vector and
model tag change.

REVIEW #6: an embedder swap arms TWO landmines at once. This step used to be a
stop-less ``while True`` that would re-embed the WHOLE store through noon (and
on battery nights), and the same swap wholesale-invalidates the contradiction
pair-judgement memory (migration 017 keys on embed_model), so both detonate the
same night. The step is now (a) gated to unbounded days by its nightly caller
(``kbworker.tail_steps.reembed_step``), (b) stop-checked between batches, and
(c) capped per run by ``kb_reembed_night_cap``. An embedder swap is therefore a
WEEKEND project, scheduled deliberately — never a casual weekday .env edit — and
only now that the contradiction weekday day-cap bounds the re-judging wave that
follows it.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from kbcore import embed as embed_mod
from kbcore.config import settings
from kbcore.db import transaction

BATCH = 64

# "deadline" / "battery" / None — see ``kbworker.consolidate.should_stop``.
StopCheck = Callable[[], "str | None"]


def reembed(stop: StopCheck | None = None, max_chunks: int | None = None) -> int:
    """Re-embed stale chunks. Returns how many chunks were updated.

    ``stop`` (the run's stand-down guard) is asked between :data:`BATCH`-row
    batches, so the overshoot past a deadline/battery stop is at most one batch
    of embed calls. ``max_chunks`` caps ONE call's work — ``None`` reads
    ``kb_reembed_night_cap``, ``0`` means uncapped (an owner deliberately
    finishing a swap). Stopping or capping loses nothing: a chunk not reached is
    still stale and is re-selected by the next eligible run.
    """
    cap = int(settings.kb_reembed_night_cap) if max_chunks is None else int(max_chunks)
    updated = 0
    while True:
        if stop is not None and stop():
            return updated
        limit = BATCH if cap <= 0 else min(BATCH, cap - updated)
        if limit <= 0:
            return updated
        with transaction() as conn:
            rows = conn.execute(
                """
                SELECT id, text FROM chunks
                WHERE embed_model IS DISTINCT FROM %s
                ORDER BY id LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (settings.kb_embed_model, limit),
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

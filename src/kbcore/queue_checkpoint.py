"""kbcore.queue_checkpoint — PER-WINDOW extraction checkpoints for the drain.

A coalesced group is extracted one CONTEXT-SIZED WINDOW at a time
(``kbworker.prep._assemble_jobs``) and its queue rows flip only when the LAST
window lands. The all-or-nothing flip is settled design and is NOT changed here:
row flips stay group-atomic, so a session can never half-land in the queue's
status accounting. What it used to also mean is that the EXTRACTION CALLS had no
memory — a group interrupted at window 9 of 12 (a failing window, the tail
reserve, the deadline) re-ran windows 1-8 the next night, whose items were
already durably stored and deduped to nothing on ``content_hash``.

Measured 2026-08-10 on three giant sessions (aquamonix 269 rows/116 MB at
deferrals=6, ims 59/52 MB, weird-rtos 10/8 MB): ~8 GPU-hours a night re-mining
knowledge the KB already held, then the same failing window, then the whole group
deferred again. The night before this landed: 7.9 h of drain, 0 rows drained, 0
items saved. Owner decision 2026-08-10 — give the extraction calls a memory.

WHAT IS STORED, AND WHY IT IS NOT KNOWLEDGE. One additive JSONB key on the
group's FIRST row (``meta->'extract_checkpoint'``): the fingerprint, the window
indices that succeeded, the running saved/rejected totals, and a timestamp. No
migration, no new table, no content — it is bookkeeping ABOUT work that is
already stored, so losing it costs a re-extraction and nothing else.

THE FINGERPRINT IS THE SAFETY PROPERTY. ``fp`` is the sha256 of the ASSEMBLED
GROUP TEXT the windows were cut from, so a checkpoint is only ever trusted
against the exact text that produced it. Let a late chunk coalesce into the group
and the text changes, the windows move, the fingerprint differs, and the
checkpoint is DISCARDED for a full re-extraction — 'window 3' no longer means
what it meant. Skipping is therefore never a guess.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from kbcore.db import transaction

# The single JSONB key this feature owns. Everything else already riding on a
# row's meta (repo/branch/commit/redactions/source_uri) must survive untouched,
# which is why every write below is a MERGE and never an overwrite.
CHECKPOINT_KEY = "extract_checkpoint"

# The jsonb slice of the EXISTING row that enqueue's ON CONFLICT re-arm merges
# over the fresh EXCLUDED.meta (review #4 P2). ``meta = EXCLUDED.meta`` wiped a
# banked checkpoint on IDENTICAL resubmission — the one case (same bytes, so
# likely the same assembled text) where the fingerprint is MOST likely to still
# match and spare the GPU the done windows. jsonb_strip_nulls collapses the
# no-checkpoint case to '{}', a no-op merge, so fresh rows are untouched; a
# text that DID drift is discarded by the fingerprint as always.
# ``queue_repair.requeue_failed`` preserves this key for the same reason;
# ``requeue_extracted`` deliberately clears it (re-mining is the point there).
CHECKPOINT_PRESERVE_SQL = (
    f"jsonb_strip_nulls(jsonb_build_object('{CHECKPOINT_KEY}', "
    f"ingest_queue.meta -> '{CHECKPOINT_KEY}'))"
)


def window_fingerprint(windows: list[str]) -> str:
    """sha256 over the WINDOWS — content AND geometry — that a checkpoint is valid for.

    Length-prefixed per window, so it pins the text, the boundaries, and the
    count all at once. Hashing the pre-split text was the reviewed-out CRITICAL
    (wfulfrf33 follow-up): the split derives from settings.kb_llm_num_ctx, so a
    num_ctx change between two nights — the exact knob an owner tunes when
    giants keep failing — kept the fingerprint while MOVING every boundary, and
    stale done-indices then skipped content that had never been extracted on any
    night. Any change to the text, the knobs, the tokenizer, or the splitter
    itself now yields a different fingerprint and a discarded checkpoint —
    re-extraction is idempotent and cheap; silent skipping is knowledge loss.
    """
    h = hashlib.sha256()
    for w in windows:
        b = w.encode("utf-8")
        h.update(str(len(b)).encode("ascii"))
        h.update(b"\x1f")
        h.update(b)
    return h.hexdigest()


def save_window_checkpoint(
    queue_id: int, *, fp: str, done: list[int], saved: int, rejected: int
) -> None:
    """Record which windows of a multi-window group are already extracted.

    Written on the group's FIRST row after EACH successful window — one cheap
    UPDATE against a multi-minute extraction call — because the thing it protects
    against is precisely a run that ENDS part-way through a group. Batching it to
    the end would checkpoint only groups that never needed one.

    ``saved``/``rejected`` are the group's RUNNING totals, so a group finished
    across two nights still books truthful numbers in
    :func:`kbcore.ingest_queue.mark_group_extracted` instead of reporting only
    the last night's share.

    Merged with ``||``: the drain does not own this row's meta, and a checkpoint
    that clobbered ``source_uri`` would break the grouping key it depends on.
    """
    payload = {
        "fp": fp,
        "done": sorted({int(i) for i in done}),
        "saved": int(saved),
        "rejected": int(rejected),
        "at": datetime.now(UTC).isoformat(),
    }
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET meta = coalesce(meta, '{}'::jsonb) || %(patch)s "
            "WHERE id = %(id)s",
            {"patch": Jsonb({CHECKPOINT_KEY: payload}), "id": queue_id},
        )


def window_checkpoint(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """The checkpoint riding on a group's first-row ``meta``, or ``None``.

    A READ, not a query: ``ingest_queue.pending_groups`` already hands the drain
    every row's ``meta``, so resuming costs no extra round trip. Anything that is
    not a dict is treated as absent — a hand-edited or half-written value must
    degrade to a full re-extraction, never to an exception inside the drain.
    """
    cp = (meta or {}).get(CHECKPOINT_KEY)
    return cp if isinstance(cp, dict) else None


def resume_windows(
    meta: dict[str, Any] | None, fp: str, n_windows: int
) -> tuple[set[int], int, int]:
    """``(windows already extracted, saved so far, rejected so far)`` for a group.

    Empty — meaning "extract everything" — unless the checkpoint's fingerprint
    matches this exact assembled text. A returned index is a window whose
    ``extract_and_ingest`` RETURNED: its items are durably stored, so re-running
    it is pure waste (it would dedupe to nothing on ``content_hash``, Invariant 3).

    EVERY window done is treated as NO checkpoint. That state means a crash
    between the last checkpoint write and ``mark_group_extracted``; honouring it
    would emit a group with zero jobs, whose rows would then never flip — the
    group would sit 'pending' forever. Re-extracting is idempotent and rare.

    Defensive throughout, because a checkpoint is DATA: a malformed value
    degrades to a full re-extraction rather than raising inside the drain, where
    the group's exception handler would mark perfectly good content 'failed'.
    """
    cp = window_checkpoint(meta)
    if not cp or cp.get("fp") != fp or not isinstance(cp.get("done"), list):
        return set(), 0, 0
    # STRICT: one invalid entry discards the WHOLE checkpoint. Filtering bad
    # indices was the reviewed-out dodge — a stale-geometry checkpoint could
    # slip its in-range subset past the guard and skip never-extracted content.
    raw = cp["done"]
    if not all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < n_windows for i in raw):
        return set(), 0, 0
    done = set(raw)
    if not done or len(done) >= n_windows or len(done) != len(raw):
        return set(), 0, 0
    saved = cp["saved"] if isinstance(cp.get("saved"), int) else 0
    rejected = cp["rejected"] if isinstance(cp.get("rejected"), int) else 0
    return done, saved, rejected

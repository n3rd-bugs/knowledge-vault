"""kbcore.ingest_queue — durable queue for remote submissions (Invariant 8).

Remote agents submit conversation transcripts or memory dumps over the public
MCP/REST endpoint. We SCRUB and store immediately (fast, no GPU); the grounded
14B extraction runs later in the nightly consolidation drain. The raw-with-secrets
text is never persisted — only the scrubbed content is stored. All SQL for these
rows lives here and in the split-out queue modules re-imported below.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation

# THE ESTABLISHED SEAM. The modules below were split OUT of this one (the
# per-window checkpoint 2026-08-10, the owner requeue repairs with it, the
# read-side grouping 2026-08-21, the submit side 2026-09-11) and are re-imported
# HERE because this module owns these rows: every caller, CLI and test reaches
# them as ``ingest_queue.<name>``, and the split must not be visible to any of
# them. Same pattern as kbworker.consolidate's re-exports.
from kbcore.queue_checkpoint import (  # noqa: F401
    CHECKPOINT_KEY,
    CHECKPOINT_PRESERVE_SQL,
    resume_windows,
    save_window_checkpoint,
    window_checkpoint,
    window_fingerprint,
)
from kbcore.queue_enqueue import EnqueueResult, enqueue  # noqa: F401
from kbcore.queue_groups import (  # noqa: F401
    _DEGENERATE_URI_SQL,
    _DRAINABLE,
    COALESCE_QUIET_MINUTES,
    group_key,
    normalise_source_uri,
    pending,
    pending_groups,
)
from kbcore.queue_repair import (  # noqa: F401
    drain_day_start,
    near_deferral_cap_groups,
    requeue_extracted,
    requeue_failed,
)

logger = logging.getLogger("kbcore.ingest_queue")

QUEUE_KINDS = ("session", "memory")
MAX_CONTENT_BYTES = 1_000_000  # ~1 MB cap on a single submission
MAX_PENDING = 5_000  # aggregate cap: refuse new submissions when the backlog is this deep
# A failed row is re-drained until it has been tried this many times, then left
# permanently 'failed'. Self-heals a transient DB/OOM failure without any
# operator requeue tool; a genuinely-bad row stops after a bounded few attempts.
MAX_ATTEMPTS = 3
# A row DEFERRED for a "transient" extractor/embedder outage keeps attempts
# untouched (an outage must not burn the content-failure budget). But a
# payload-DETERMINISTIC 5xx / mid-response crash is misclassified as transient and
# would re-drain every night forever. After this many consecutive deferrals — set
# WELL ABOVE MAX_ATTEMPTS so a genuine multi-night outage still recovers — the row
# is escalated to 'failed' so the attempts cap can retire it (review finding).
DEFERRAL_CAP = 10
# Times a TERMINALLY-failed row may be re-armed to 'pending' by an identical
# resubmission before it stays permanently failed. Bounds a recurring poison
# payload that would otherwise reset attempts=0 forever (review finding).
MAX_REQUEUES = 3
# Projects a public submission may never target (system/curated). Review findings:
# a public caller must not inject items into kb-system or burn the nightly run on it.
RESERVED_PROJECTS = frozenset({"kb-system"})


class QueueFull(InvalidOperation):
    """The pending backlog is at :data:`MAX_PENDING`: refused, retry after the drain.

    A subclass so every existing InvalidOperation path (MCP tool error, CLI exit)
    is unchanged; the REST facade maps it to 429 so the hook's retry set can tell
    "wait" from "bad request" (wave S item S7, V4-M5).
    """


def mark_extracted(queue_id: int, saved: int, rejected: int) -> None:
    """Record a successful drain — terminal, whether it took a retry or not."""
    with transaction() as conn:
        conn.execute(
            """
            UPDATE ingest_queue
            SET status = 'extracted', saved_count = %s, rejected_count = %s, processed_at = now()
            WHERE id = %s
            """,
            (saved, rejected, queue_id),
        )


def mark_failed(queue_id: int, error: str) -> bool:
    """Record a failed drain (kept 'failed', not deleted — inspectable + retried).

    Increments ``attempts``; once it reaches :data:`MAX_ATTEMPTS` the row is no
    longer selected by :func:`pending`, so a genuinely-bad row stops looping.
    Returns True when THIS call took the row terminal (attempts hit the cap), so
    the drain can count the retirement into the run report and digest instead of
    leaving it journald-only (<36h retention — review #4 P0 rider).
    """
    with transaction() as conn:
        row = conn.execute(
            "UPDATE ingest_queue SET status = 'failed', attempts = attempts + 1, "
            "error = %s, processed_at = now() WHERE id = %s RETURNING attempts",
            (error[:2000], queue_id),
        ).fetchone()
    return row is not None and int(row[0]) >= MAX_ATTEMPTS


def mark_deferred(queue_id: int, error: str) -> bool:
    """Record a TRANSIENT defer (extractor/embedder unavailable): bump ``deferrals``,
    leaving the row 'pending' with ``attempts`` UNCHANGED so an outage never burns
    the content-failure budget. Once deferrals reach :data:`DEFERRAL_CAP` a
    "transient" that never heals is treated as content-deterministic in practice and
    ESCALATED to 'failed' (attempts pinned at the cap) so the attempts logic retires
    it — a payload-deterministic 5xx / mid-response crash can no longer re-drain
    every night forever (review finding). Returns True when the row was escalated.

    THE BURN IS BOUNDED PER LOCAL DAY (review #5 pipe-2, symmetric with the
    per-day ATTEMPT budget). :data:`DEFERRAL_CAP` is reasoned in NIGHTS ("a
    genuine multi-night outage still recovers"), but this used to burn per RUN,
    and multi-run local days are real (kb-consolidate-boot.timer fires a catch-up
    10 min after boot; 08-15 ran twice) — a flapping-power outage, the exact
    scenario the cap must survive, could escalate transient groups to terminal in
    3-4 days. A second same-day defer still records the latest ``error`` and
    leaves the row 'pending' (same-day retry ELIGIBILITY is untouched); only the
    budget burn is skipped. The day stamp is additive JSONB meta (the
    extract_checkpoint pattern, no migration); owner requeues reset ``deferrals``
    so the repair path is untouched.
    """
    day = drain_day_start().date().isoformat()
    with transaction() as conn:
        row = conn.execute(
            """
            UPDATE ingest_queue
               SET deferrals = CASE WHEN meta->>'last_deferred_day' = %(day)s
                                    THEN deferrals ELSE deferrals + 1 END,
                   meta = coalesce(meta, '{}'::jsonb)
                          || jsonb_build_object('last_deferred_day', %(day)s::text),
                   error = %(error)s
             WHERE id = %(id)s
             RETURNING deferrals
            """,
            {"day": day, "error": error[:2000], "id": queue_id},
        ).fetchone()
        deferrals = int(row[0]) if row else 0
        if deferrals >= DEFERRAL_CAP:
            conn.execute(
                "UPDATE ingest_queue SET status = 'failed', attempts = %s, "
                "processed_at = now() WHERE id = %s",
                (MAX_ATTEMPTS, queue_id),
            )
            return True
    return False


def escalate_rows(ids: list[int], error: str) -> None:
    """Terminally fail ``ids`` with attempts PINNED at the cap — the group-atomic
    half of a deferral-cap escalation (review #6).

    Per-row deferral counters can diverge inside one coalesced group (rows join a
    session on different nights), so :func:`mark_deferred` may escalate SOME rows
    of a group while leaving siblings 'pending' — and a split group is extracted
    in pieces on the next run, the exact thing coalescing exists to prevent. The
    drain calls this on the REMAINING rows so the group retires as a unit: the
    same status flip mark_deferred's escalation performs (Invariant 5 — never a
    delete), attempts pinned to :data:`MAX_ATTEMPTS` so the attempts logic keeps
    it retired, and the owner's ``requeue_failed`` reverses it group-wide.
    """
    if not ids:
        return
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET status = 'failed', attempts = %s, error = %s, "
            "processed_at = now() WHERE id = ANY(%s)",
            (MAX_ATTEMPTS, error[:2000], list(ids)),
        )


def queue_depth(before: datetime | None = None) -> int:
    """Count of pending submissions — only those created before ``before`` when given (the
    run start: a row enqueued mid-run is not 'deferred'; H6). Resilient to migration 002."""
    with connect() as conn:
        if conn.execute("SELECT to_regclass('ingest_queue')").fetchone()[0] is None:
            return 0
        sql = "SELECT count(*) FROM ingest_queue WHERE status = 'pending' AND created_at < %s"
        row = conn.execute(sql, (before or datetime.max.replace(tzinfo=UTC),)).fetchone()
    return int(row[0])


def mark_group_extracted(ids: list[int], saved: int, rejected: int) -> None:
    """Flip a whole coalesced group to 'extracted' in ONE transaction, booking the
    group's totals on ONE row. All-or-nothing: every id flips together, so a group
    extracted once cannot leave rows pending for a partial re-extraction.

    THE TOTALS BELONG TO THE GROUP, NOT EACH ROW. Writing ``saved`` / ``rejected``
    to EVERY row multiplied ``sum(saved_count)`` by the coalesce factor (a 648-item
    session reported 26,972 across ~42 rows). They are booked on the FIRST id and 0
    on the rest, so the column sums to the real total; that zero on a productive
    group's other rows is told apart from a genuinely zero-yield row by
    :func:`requeue_extracted`, whose predicate is group-aware for exactly this.

    THE PER-WINDOW CHECKPOINT DIES WITH THE GROUP IT WAS RESUMING (2026-08-10),
    in this same transaction as the flip: a completed group leaves no residue, so
    the key can never be read back against a re-opened row (and a stale one could
    only ever be read against a matching fingerprint anyway). See
    :mod:`kbcore.queue_checkpoint`.
    """
    if not ids:
        return
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET status='extracted', "
            "saved_count = CASE WHEN id = %(first)s THEN %(saved)s ELSE 0 END, "
            "rejected_count = CASE WHEN id = %(first)s THEN %(rejected)s ELSE 0 END, "
            "meta = coalesce(meta, '{}'::jsonb) - %(cpkey)s::text, "
            "processed_at = now() WHERE id = ANY(%(ids)s)",
            {
                "first": ids[0],
                "saved": saved,
                "rejected": rejected,
                "ids": list(ids),
                "cpkey": CHECKPOINT_KEY,
            },
        )

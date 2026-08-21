"""kbcore.ingest_queue — durable queue for remote submissions (Invariant 8).

Remote agents submit conversation transcripts or memory dumps over the public
MCP/REST endpoint. We SCRUB and store immediately (fast, no GPU); the grounded
14B extraction runs later in the nightly consolidation drain. The raw-with-secrets
text is never persisted — only the scrubbed content is stored. All SQL for these
rows lives here and in the split-out queue modules re-imported below.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, NamedTuple

from psycopg.types.json import Jsonb

from kbcore import scope
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation

# THE ESTABLISHED SEAM. The modules below were split OUT of this one (the
# per-window checkpoint 2026-08-10, the owner requeue repairs with it, the
# read-side grouping 2026-08-21) and are re-imported HERE because this module
# owns these rows: every caller, CLI and test reaches them as
# ``ingest_queue.<name>``, and the split must not be visible to any of them.
# Same pattern as kbworker.consolidate's re-exports.
from kbcore.queue_checkpoint import (  # noqa: F401
    CHECKPOINT_KEY,
    CHECKPOINT_PRESERVE_SQL,
    resume_windows,
    save_window_checkpoint,
    window_checkpoint,
    window_fingerprint,
)
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
from kbcore.scrub import scrub

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


class EnqueueResult(NamedTuple):
    """Outcome of an enqueue: ``id`` is the drainable row (None ⇒ live duplicate);
    ``requeued`` is True when a TERMINALLY-FAILED row was reset to 'pending' so
    callers can distinguish "re-queued after failure" from "already queued";
    ``permanently_failed`` is True when the row is terminally failed AND has
    exhausted its re-arm budget (:data:`MAX_REQUEUES`) — the id is returned but the
    row was NOT re-opened, so callers report it as needing manual review."""

    id: int | None
    requeued: bool
    permanently_failed: bool = False


def enqueue(
    project: str,
    content: str,
    kind: str = "session",
    repo: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
    source_uri: str | None = None,
    allow_reserved: bool = False,
) -> EnqueueResult:
    """Scrub and enqueue a submission. Returns an :class:`EnqueueResult`.

    Rejects unknown kinds, empty content, and oversized content (domain errors).
    The content is SCRUBBED before storage and the dedupe hash is over the
    scrubbed text, so re-submitting identical content is a no-op. ``allow_reserved``
    is set only by trusted LOCAL callers (the CLI session flush) so a PreCompact
    hook may file into kb-system; the public REST/MCP path always leaves it false.

    Resubmitting content whose only existing row is TERMINALLY FAILED (status
    'failed', attempts ≥ MAX_ATTEMPTS) resets that row to 'pending'/attempts=0 and
    returns its id with ``requeued=True`` — a status flip (Invariant 5), so a bug
    fixed after the cap was hit no longer strands the content behind the dedupe
    UNIQUE. A row that is still pending/in-flight/extracted/under-cap stays a no-op
    (id=None) so a live submission is never disturbed. Re-arming is BOUNDED at
    :data:`MAX_REQUEUES`: past that a recurring poison payload is left permanently
    failed and its id is returned with ``permanently_failed=True`` (finding).
    The re-arm keeps the FRESH submission's meta but CARRIES the old row's
    per-window checkpoint forward (review #4 P2): a byte-identical resubmission
    is the case most likely to still fingerprint-match (CHECKPOINT_PRESERVE_SQL).
    """
    if kind not in QUEUE_KINDS:
        raise InvalidOperation(f"unknown ingest kind {kind!r}; one of {', '.join(QUEUE_KINDS)}")
    if not project or not project.strip():
        raise InvalidOperation("project is required")
    if not allow_reserved and project.strip() in RESERVED_PROJECTS:
        raise InvalidOperation(f"project {project!r} is reserved and cannot be targeted remotely")
    # Public-only instance: only public projects accept submissions.
    scope.assert_writable(project)
    if not content or not content.strip():
        raise InvalidOperation("content is empty")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise InvalidOperation(f"content exceeds the {MAX_CONTENT_BYTES}-byte limit")
    if queue_depth() >= MAX_PENDING:
        raise InvalidOperation("ingest queue is full; try again after the nightly drain")

    clean, redactions = scrub(content)
    content_hash = hashlib.sha256(clean.encode("utf-8")).hexdigest()
    meta: dict[str, Any] = {}
    # A DEGENERATE session id is dropped at the door rather than stored: it is a
    # grouping key, and an empty one groups everything (see normalise_source_uri).
    source_uri = normalise_source_uri(source_uri)
    # Scrub the freeform meta strings too — the durable queue must hold no
    # raw secret, and these values flow into item metadata (review finding).
    for key, value in (
        ("repo", repo),
        ("branch", branch),
        ("commit", commit),
        ("source_uri", source_uri),
    ):
        if value:
            meta[key] = scrub(value)[0]
    # Box identity (KB 19663): AUTHENTICATED submitter via kbcore.scope (rule 12).
    meta["submitted_by"] = scope.current().name
    if redactions:
        meta["redactions"] = redactions

    with transaction() as conn:
        # ON CONFLICT DO UPDATE fires for ANY terminally-failed row; other statuses
        # fail the WHERE so RETURNING yields no row (id=None → live duplicate). For a
        # terminal row we RE-OPEN it (status→'pending', attempts→0) only while it is
        # still under the re-arm budget; once requeue_count hits MAX_REQUEUES we bump
        # nothing and leave it 'failed', so a recurring poison payload can't reset
        # attempts forever. xmax is 0 on a fresh INSERT and non-zero on an UPDATE, and
        # the returned status tells a re-open apart from a permanent-failure touch.
        row = conn.execute(
            f"""
            INSERT INTO ingest_queue (project, kind, content, content_hash, meta)
            VALUES (%(project)s, %(kind)s, %(content)s, %(hash)s, %(meta)s)
            ON CONFLICT (kind, project, content_hash) DO UPDATE
              SET requeue_count = CASE WHEN ingest_queue.requeue_count < %(cap)s
                                       THEN ingest_queue.requeue_count + 1
                                       ELSE ingest_queue.requeue_count END,
                  status = CASE WHEN ingest_queue.requeue_count < %(cap)s
                                THEN 'pending' ELSE 'failed' END,
                  attempts = CASE WHEN ingest_queue.requeue_count < %(cap)s
                                  THEN 0 ELSE ingest_queue.attempts END,
                  error = CASE WHEN ingest_queue.requeue_count < %(cap)s
                               THEN NULL ELSE ingest_queue.error END,
                  deferrals = CASE WHEN ingest_queue.requeue_count < %(cap)s
                                   THEN 0 ELSE ingest_queue.deferrals END,
                  meta = EXCLUDED.meta || {CHECKPOINT_PRESERVE_SQL},
                  processed_at = CASE WHEN ingest_queue.requeue_count < %(cap)s
                                      THEN NULL ELSE ingest_queue.processed_at END
              WHERE ingest_queue.status = 'failed' AND ingest_queue.attempts >= %(maxa)s
            RETURNING id, (xmax <> 0), (status = 'pending')
            """,  # noqa: S608 — CHECKPOINT_PRESERVE_SQL is a constant fragment
            {
                "project": project,
                "kind": kind,
                "content": clean,
                "hash": content_hash,
                "meta": Jsonb(meta),
                "cap": MAX_REQUEUES,
                "maxa": MAX_ATTEMPTS,
            },
        ).fetchone()
    if row is None:
        return EnqueueResult(None, requeued=False)
    id_, was_update, reopened = int(row[0]), bool(row[1]), bool(row[2])
    if was_update and not reopened:
        # A terminal row over the re-arm budget: id returned, but NOT re-opened.
        return EnqueueResult(id_, requeued=False, permanently_failed=True)
    return EnqueueResult(id_, requeued=(was_update and reopened))


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


def queue_depth() -> int:
    """Count of pending submissions. Resilient to migration 002 not yet applied."""
    with connect() as conn:
        exists = conn.execute("SELECT to_regclass('ingest_queue')").fetchone()[0]
        if exists is None:
            return 0
        row = conn.execute("SELECT count(*) FROM ingest_queue WHERE status = 'pending'").fetchone()
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

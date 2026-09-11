"""kbcore.queue_enqueue — the submit side of the ingest queue: scrub, guard, store.

Split from ``kbcore.ingest_queue`` (2026-09-11, wave S item S2): that file sat at
its ratchet ceiling and the resend guard needed room. FOURTH application of the
documented seam (queue_checkpoint, queue_repair, queue_groups before it):
``enqueue`` and ``EnqueueResult`` are re-imported by ``ingest_queue``, so every
caller keeps resolving ``ingest_queue.enqueue``. The queue's constants
(``MAX_PENDING``, ``MAX_ATTEMPTS``, ...) are read THROUGH ``ingest_queue`` at
call time — a lazy import, the queue_groups pattern — because tests patch them
there (``monkeypatch.setattr(ingest_queue, "MAX_PENDING", 1)``) and the seam must
stay invisible. Same logger name as the parent on purpose (journald greps).

THE RESEND GUARD (proposal_v2 Appendix F, KB 33783/33784). A session resumed
after a clean SessionEnd re-sent its whole transcript from byte 0 under whatever
project the firing-time cwd resolved to, and the only guard was UNIQUE (kind,
project, content_hash) — every chunk became a new row and a full re-extraction.
A transcript chunk (``kind='session'``, uri ``session:<sid>``) is now refused
when its ``(scrubbed uri, content_hash)`` is already queued under ANOTHER
project, when any row of that group is already mined, or when the same bytes
arrived from the same submitter under a different session uri. A refusal is
answered exactly like today's same-project duplicate (no-oracle, rules 10/12)
and recorded ONLY in the submitter's ledger row — never on the canonical.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, NamedTuple

import psycopg
from psycopg.types.json import Jsonb

from kbcore import queue_guard, scope
from kbcore.config import settings
from kbcore.db import transaction
from kbcore.errors import InvalidOperation
from kbcore.models import FlushStamp
from kbcore.queue_checkpoint import CHECKPOINT_PRESERVE_SQL
from kbcore.queue_groups import normalise_source_uri
from kbcore.scrub import scrub

logger = logging.getLogger("kbcore.ingest_queue")


class EnqueueResult(NamedTuple):
    """Outcome of an enqueue: ``id`` is the drainable row (None ⇒ live duplicate);
    ``requeued`` is True when a TERMINALLY-FAILED row was reset to 'pending' so
    callers can distinguish "re-queued after failure" from "already queued";
    ``permanently_failed`` is True when the row is terminally failed AND has
    exhausted its re-arm budget (:data:`MAX_REQUEUES`) — the id is returned but the
    row was NOT re-opened, so callers report it as needing manual review.
    ``resend_of`` names the canonical row a guarded resend was refused against —
    for TESTS only: the REST route builds its dict explicitly and MUST NOT add it,
    and the MCP tool says "duplicate — already queued" (no-oracle, rules 10/12)."""

    id: int | None
    requeued: bool
    permanently_failed: bool = False
    resend_of: int | None = None


def _refuse(
    conn: psycopg.Connection,
    prior: queue_guard.PriorRow,
    *,
    kind: str,
    uri: str,
    project: str,
    flush: FlushStamp | None,
    raw_len: int,
) -> EnqueueResult:
    """Record a guarded refusal in the submitter's ledger row and answer no-oracle."""
    from kbcore import ingest_queue

    if prior.status == "failed" and prior.attempts >= ingest_queue.MAX_ATTEMPTS:
        # Recovery is the OWNER's requeue_failed on the canonical's project; a
        # cross-project re-arm would let this bearer mutate another project's row.
        logger.warning(
            "resend of terminally failed canonical %s refused (from project %s, kind %s)",
            prior.id,
            project,
            kind,
        )
    queue_guard.record_refusal(
        conn,
        principal=scope.current().name,
        uri=uri,
        kind=kind,
        project=project,
        box=flush.box if flush is not None else None,
        canonical_id=prior.id,
        raw_bytes=raw_len,
    )
    return EnqueueResult(None, requeued=False, resend_of=prior.id)


def _probe(
    conn: psycopg.Connection, uri: str, content_hash: str, *, project: str, principal: str
) -> tuple[queue_guard.PriorRow, str] | None:
    """Appendix F step 2 in order: (a) cross-project prior, (b) mined twin anywhere,
    (c) same-principal hash-xsession (gated). The hit and its ledger kind, or None."""
    prior = queue_guard.find_prior(conn, uri, content_hash)
    if prior is not None and prior.project != project:
        return prior, queue_guard.KIND_HASH
    twin = queue_guard.extracted_twin(conn, uri, content_hash)
    if twin is not None:
        return twin, queue_guard.KIND_EXTRACTED
    if settings.kb_ingest_xsession_guard:
        twin = queue_guard.xsession_twin(conn, uri, content_hash, principal)
        if twin is not None:
            return twin, queue_guard.KIND_XSESSION
    return None


def enqueue(
    project: str,
    content: str,
    kind: str = "session",
    repo: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
    source_uri: str | None = None,
    allow_reserved: bool = False,
    hook_version: str | None = None,
    flush: FlushStamp | None = None,
) -> EnqueueResult:
    """Scrub and enqueue a submission. Returns an :class:`EnqueueResult`.

    Rejects unknown kinds, empty content, and oversized content (domain errors);
    a full backlog raises :class:`kbcore.ingest_queue.QueueFull` (REST 429).
    The content is SCRUBBED before storage and the dedupe hash is over the
    scrubbed text, so re-submitting identical content is a no-op. ``allow_reserved``
    is set only by trusted LOCAL callers (the CLI flush); REST/MCP leave it false.

    Resubmitting content whose only existing row is TERMINALLY FAILED (status
    'failed', attempts ≥ MAX_ATTEMPTS) resets that row to 'pending'/attempts=0 and
    returns its id with ``requeued=True`` — a status flip (Invariant 5), so a bug
    fixed after the cap was hit no longer strands the content behind the dedupe
    UNIQUE. A row that is still pending/in-flight/extracted/under-cap stays a no-op
    (id=None) so a live submission is never disturbed. Re-arming is BOUNDED at
    :data:`MAX_REQUEUES`: past that a recurring poison payload is left permanently
    failed and its id is returned with ``permanently_failed=True`` (finding).
    The re-arm keeps the FRESH submission's meta but CARRIES the old row's
    per-window checkpoint and flush telemetry forward (review #4 P2, wave S):
    a byte-identical resubmission is the case most likely to still
    fingerprint-match (CHECKPOINT_PRESERVE_SQL).

    TRANSCRIPT CHUNKS are additionally guarded project-agnostically (module
    docstring; ``KB_INGEST_RESEND_GUARD`` is the kill switch, ``KB_INGEST_XSESSION_GUARD``
    gates the same-principal cross-session probe). ``flush`` stamps are stored as
    ``meta.flush`` telemetry when consistent with the content, else counted and dropped.
    """
    from kbcore import ingest_queue

    if kind not in ingest_queue.QUEUE_KINDS:
        raise InvalidOperation(
            f"unknown ingest kind {kind!r}; one of {', '.join(ingest_queue.QUEUE_KINDS)}"
        )
    if not project or not project.strip():
        raise InvalidOperation("project is required")
    if not allow_reserved and project.strip() in ingest_queue.RESERVED_PROJECTS:
        raise InvalidOperation(f"project {project!r} is reserved and cannot be targeted remotely")
    # Public-only instance: only public projects accept submissions.
    scope.assert_writable(project)
    if not content or not content.strip():
        raise InvalidOperation("content is empty")
    raw_len = len(content.encode("utf-8"))
    if raw_len > ingest_queue.MAX_CONTENT_BYTES:
        raise InvalidOperation(f"content exceeds the {ingest_queue.MAX_CONTENT_BYTES}-byte limit")
    if ingest_queue.queue_depth() >= ingest_queue.MAX_PENDING:
        raise ingest_queue.QueueFull("ingest queue is full; try again after the nightly drain")
    clean, redactions = scrub(content)
    content_hash = hashlib.sha256(clean.encode("utf-8")).hexdigest()
    meta: dict[str, Any] = {}
    # A DEGENERATE session id is dropped at the door rather than stored: it is a
    # grouping key, and an empty one groups everything (see normalise_source_uri).
    # Scrubbed ONCE here: the guard's probe, the chunk key and meta.source_uri
    # must all see the same string (V2-m4).
    uri = normalise_source_uri(source_uri)
    if uri is not None:
        uri = scrub(uri)[0]
    # Scrub the freeform meta strings too — the durable queue must hold no
    # raw secret, and these values flow into item metadata (review finding).
    for key, value in (
        ("repo", repo),
        ("branch", branch),
        ("commit", commit),
        ("hook_version", hook_version),  # review #8 H15: fleet version column
    ):
        if value:
            meta[key] = scrub(value)[0]
    if uri:
        meta["source_uri"] = uri
    # Box identity (KB 19663): AUTHENTICATED submitter via kbcore.scope (rule 12).
    principal = scope.current().name
    meta["submitted_by"] = principal
    if redactions:
        meta["redactions"] = redactions
    # Narrowed ONCE: ``guarded_uri`` is the str the probes, the ledger and the
    # chunk key all see, or None when the guard does not apply to this row.
    guarded_uri: str | None = (
        uri
        if bool(settings.kb_ingest_resend_guard) and queue_guard.is_transcript_chunk(kind, uri)
        else None
    )
    refuse_kw: dict[str, Any] = {"project": project, "flush": flush, "raw_len": raw_len}

    with transaction() as conn:
        if guarded_uri is not None:
            hit = _probe(conn, guarded_uri, content_hash, project=project, principal=principal)
            if hit is not None:
                return _refuse(conn, hit[0], kind=hit[1], uri=guarded_uri, **refuse_kw)
            # A same-project prior falls through to today's ON CONFLICT: live
            # duplicate / bounded re-arm semantics byte for byte.
        queue_guard.apply_stamp(
            conn, meta, flush, uri=uri, raw_len=raw_len, principal=principal, chash=content_hash
        )
        ckey = None if guarded_uri is None else queue_guard.chunk_key(guarded_uri, content_hash)
        try:
            row = _insert(conn, project, kind, clean, content_hash, meta, ckey)
        except psycopg.errors.UniqueViolation as e:
            # Lost the chunk_key arbiter to a concurrent insert of the same chunk
            # under another project. The SAVEPOINT in _insert rolled back only the
            # INSERT, so the ledger write below still lands in this transaction.
            unrelated = e.diag.constraint_name != queue_guard.CHUNK_KEY_UIDX
            if unrelated or ckey is None or guarded_uri is None:  # ckey ⇒ guarded_uri
                raise
            prior = queue_guard.prior_by_chunk_key(conn, ckey)
            if prior is None:
                raise InvalidOperation("submission could not be queued; retry") from e
            return _refuse(conn, prior, kind=queue_guard.KIND_RACE, uri=guarded_uri, **refuse_kw)
    if row is None:
        return EnqueueResult(None, requeued=False)
    id_, was_update, reopened = int(row[0]), bool(row[1]), bool(row[2])
    if was_update and not reopened:
        # A terminal row over the re-arm budget: id returned, but NOT re-opened.
        return EnqueueResult(id_, requeued=False, permanently_failed=True)
    return EnqueueResult(id_, requeued=(was_update and reopened))


def _insert(
    conn: psycopg.Connection,
    project: str,
    kind: str,
    clean: str,
    content_hash: str,
    meta: dict[str, Any],
    ckey: str | None,
) -> tuple[Any, ...] | None:
    """Today's INSERT ... ON CONFLICT (kind, project, content_hash), inside a SAVEPOINT.

    ON CONFLICT DO UPDATE fires for ANY terminally-failed row; other statuses
    fail the WHERE so RETURNING yields no row (id=None → live duplicate). For a
    terminal row we RE-OPEN it (status→'pending', attempts→0) only while it is
    still under the re-arm budget; once requeue_count hits MAX_REQUEUES we bump
    nothing and leave it 'failed', so a recurring poison payload can't reset
    attempts forever. xmax is 0 on a fresh INSERT and non-zero on an UPDATE, and
    the returned status tells a re-open apart from a permanent-failure touch.
    The nested ``conn.transaction()`` is a psycopg3 SAVEPOINT: a UniqueViolation
    on the 020 chunk_key arbiter rolls back only this statement.
    """
    from kbcore import ingest_queue

    with conn.transaction():
        return conn.execute(
            f"""
            INSERT INTO ingest_queue (project, kind, content, content_hash, meta, chunk_key)
            VALUES (%(project)s, %(kind)s, %(content)s, %(hash)s, %(meta)s, %(ckey)s)
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
                "ckey": ckey,
                "cap": ingest_queue.MAX_REQUEUES,
                "maxa": ingest_queue.MAX_ATTEMPTS,
            },
        ).fetchone()

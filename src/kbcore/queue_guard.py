"""kbcore.queue_guard — the server-side resend guard: pure helpers + DB probes (wave S).

The scope predicate and the chunk key are fixed HERE first so the enqueue guard
(:mod:`kbcore.queue_enqueue`), the owner repair (:mod:`kbcore.resend_repair`) and
the fleet telemetry (:mod:`kbcore.fleet_flush`) agree byte for byte. The DB
probes beside them (``find_prior``, the extracted-anywhere and same-principal
hash-xsession checks, the refused-resend ledger UPSERT) are the guard's eyes and
its ONLY write: a refusal is recorded as the SUBMITTER in the bounded
``ingest_resend_events`` ledger and never touches the canonical row, which may
belong to another project's bearer (proposal_v2 §7, amendment 1). Root cause
KB 33783; design KB 33784 (proposal_v2 Appendix F).

Every probe takes the caller's connection so the whole enqueue is ONE
transaction (CLAUDE.md Inv 2); nothing here opens its own.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, NamedTuple

import psycopg

from kbcore.models import FlushStamp
from kbcore.scrub import scrub

logger = logging.getLogger("kbcore.ingest_queue")

# Bump ONLY with a repair that re-keys every stored row: the key is compared, never parsed.
CHUNK_KEY_VERSION = "session-chunk/1"

# The name the 020 arbiter raises under; the enqueue race path matches on it.
CHUNK_KEY_UIDX = "ingest_queue_chunk_key_uidx"

# Refusal kinds recorded in the ledger's ``last_kind`` (string labels, never parsed).
KIND_HASH = "hash"  # same (uri, hash) already queued under ANOTHER project
KIND_EXTRACTED = "extracted"  # a mined twin exists — no re-arm behind it, any project
KIND_XSESSION = "hash-xsession"  # same bytes, same submitter, a DIFFERENT session uri
KIND_RACE = "race"  # lost the chunk_key arbiter to a concurrent insert

# Process-local telemetry counters (WARNING-logged too). Tests read and reset them.
COUNTERS: dict[str, int] = {"flush_stamp_inconsistent": 0}


class PriorRow(NamedTuple):
    """The canonical row a guarded submission collides with."""

    id: int
    project: str
    status: str
    attempts: int
    chunk_key: str | None


def is_transcript_chunk(kind: str, uri: str | None) -> bool:
    """The ONLY rows the guard touches: session chunks keyed ``session:<sid>``.

    ``uri`` is the value AFTER :func:`kbcore.queue_groups.normalise_source_uri`
    (a degenerate ``'session:'`` is already ``None`` -> False). ``doc://`` uris
    (project-qualified by construction), the local flush's ``<path>:<off>-<new>``
    uris and ``kind='memory'`` are excluded by this predicate, never by a list.
    """
    return kind == "session" and uri is not None and uri.startswith("session:")


def chunk_key(uri: str, content_hash: str) -> str:
    """Deterministic identity of ONE transcript chunk across projects and hook vintages.

    ``content_hash`` is the sha256 of the SCRUBBED text (ingest_queue keeps hashing
    that way), so two boxes redacting the same secret still agree on the key.
    """
    return hashlib.sha256(f"{CHUNK_KEY_VERSION}|{uri}|{content_hash}".encode()).hexdigest()


_PRIOR_COLS = "id, project, status, attempts, chunk_key"


def _prior(row: tuple[Any, ...] | None) -> PriorRow | None:
    if row is None:
        return None
    return PriorRow(int(row[0]), str(row[1]), str(row[2]), int(row[3]), row[4])


def find_prior(conn: psycopg.Connection, uri: str, content_hash: str) -> PriorRow | None:
    """The oldest row holding these bytes under this session uri, ANY project.

    Matches on the stored (scrubbed) ``meta.source_uri`` — served by the 020
    ``ingest_queue_session_uri_idx`` — not on ``chunk_key``, so rows written
    before 020 dedupe from the minute the migration lands.
    """
    row = conn.execute(
        f"SELECT {_PRIOR_COLS} FROM ingest_queue "  # noqa: S608 — constant column list
        "WHERE content_hash = %s AND kind = 'session' AND btrim(meta->>'source_uri') = %s "
        "ORDER BY id LIMIT 1",
        (content_hash, uri),
    ).fetchone()
    return _prior(row)


def extracted_twin(conn: psycopg.Connection, uri: str, content_hash: str) -> PriorRow | None:
    """A row of the ``(uri, hash)`` group that is already ``'extracted'`` (any project).

    A resubmission may never re-arm a failed twin behind a mined canonical: the
    knowledge is in the KB, and a re-arm would re-mine it under another project.
    """
    row = conn.execute(
        f"SELECT {_PRIOR_COLS} FROM ingest_queue "  # noqa: S608 — constant column list
        "WHERE content_hash = %s AND kind = 'session' AND btrim(meta->>'source_uri') = %s "
        "AND status = 'extracted' ORDER BY id LIMIT 1",
        (content_hash, uri),
    ).fetchone()
    return _prior(row)


def xsession_twin(
    conn: psycopg.Connection, uri: str, content_hash: str, principal: str
) -> PriorRow | None:
    """The same bytes from the SAME submitter under a DIFFERENT ``session:`` uri.

    A ``/branch`` or ``--fork-session`` copies the transcript into a new session
    id, so the copy resends every byte under a uri :func:`find_prior` cannot
    match. Restricted to one principal on purpose: a project-agnostic probe
    would refuse legitimate fixture reuse across boxes.
    """
    row = conn.execute(
        f"SELECT {_PRIOR_COLS} FROM ingest_queue "  # noqa: S608 — constant column list
        "WHERE content_hash = %s AND kind = 'session' AND meta->>'submitted_by' = %s "
        "AND left(btrim(meta->>'source_uri'), 8) = 'session:' "
        "AND btrim(meta->>'source_uri') <> %s ORDER BY id LIMIT 1",
        (content_hash, principal, uri),
    ).fetchone()
    return _prior(row)


def prior_by_chunk_key(conn: psycopg.Connection, key: str) -> PriorRow | None:
    """The row holding ``chunk_key`` — the race path's lookup after losing the arbiter."""
    row = conn.execute(
        f"SELECT {_PRIOR_COLS} FROM ingest_queue WHERE chunk_key = %s",  # noqa: S608
        (key,),
    ).fetchone()
    return _prior(row)


def record_refusal(
    conn: psycopg.Connection,
    *,
    principal: str,
    uri: str,
    kind: str,
    project: str,
    box: str | None,
    canonical_id: int,
    raw_bytes: int,
) -> None:
    """UPSERT the refused-resend ledger row for ``(principal, uri)`` — as the SUBMITTER.

    One row per (principal, session uri), so the ledger is bounded by sessions x
    boxes rather than by refused chunks; ``refused_bytes`` sums the RAW submission
    length (what the wire carried). This is the guard's only write. It never
    stamps the canonical (no ``chunk_key``, no ``meta.resend``): that row may
    belong to another project's bearer (proposal_v2 §7). ``box`` is SCRUBBED here,
    at the single ledger writer: the digest prints ``last_box``, and a token-shaped
    label fits the REST pattern (rule 12 — no issued secret survives ingest).
    """
    if box:
        box = scrub(box)[0]
    conn.execute(
        """
        INSERT INTO ingest_resend_events
            (principal, uri, first_at, last_at, refused_rows, refused_bytes,
             last_kind, last_project, last_box, last_canonical)
        VALUES (%(principal)s, %(uri)s, now(), now(), 1, %(bytes)s,
                %(kind)s, %(project)s, %(box)s, %(canonical)s)
        ON CONFLICT (principal, uri) DO UPDATE
          SET last_at = now(),
              refused_rows = ingest_resend_events.refused_rows + 1,
              refused_bytes = ingest_resend_events.refused_bytes + EXCLUDED.refused_bytes,
              last_kind = EXCLUDED.last_kind,
              last_project = EXCLUDED.last_project,
              last_box = EXCLUDED.last_box,
              last_canonical = EXCLUDED.last_canonical
        """,
        {
            "principal": principal,
            "uri": uri,
            "bytes": int(raw_bytes),
            "kind": kind,
            "project": project,
            "box": box,
            "canonical": canonical_id,
        },
    )


def apply_stamp(
    conn: psycopg.Connection,
    meta: dict[str, Any],
    flush: FlushStamp | None,
    *,
    uri: str | None,
    raw_len: int,
    principal: str,
    chash: str | None = None,
) -> None:
    """Put ``meta.flush`` (+ ``meta.overlap`` against this principal's stamped rows).

    Telemetry only. A stamp inconsistent with its own content (``offset + bytes``
    past ``size``, or ``bytes`` off the raw length) is counted, WARNING-logged and
    dropped; the row is still stored. ``box``/``branch_of`` are scrubbed like every
    other freeform meta string. ``chash`` names the chunk's OWN content_hash group so a
    same-project re-arm of a stamped row is not measured against itself.
    """
    if flush is None:
        return
    if not flush.consistent(raw_len):
        COUNTERS["flush_stamp_inconsistent"] += 1
        logger.warning(
            "flush stamp inconsistent (offset=%s bytes=%s size=%s raw=%s); row stored unstamped",
            flush.offset,
            flush.bytes,
            flush.size,
            raw_len,
        )
        return
    stamp = flush.to_meta()
    for key in ("box", "branch_of"):
        if key in stamp:
            stamp[key] = scrub(stamp[key])[0]
    meta["flush"] = stamp
    if uri is None:
        return
    cov = coverage_end(conn, uri, principal, exclude_hash=chash)
    if cov is not None and cov > flush.offset:
        end = min(cov, flush.offset + flush.bytes)
        meta["overlap"] = {"covered_end": cov, "bytes": end - flush.offset}


def coverage_end(
    conn: psycopg.Connection, uri: str, principal: str, *, exclude_hash: str | None = None
) -> int | None:
    """``max(flush.offset + flush.bytes)`` over THIS principal's STAMPED rows of ``uri``.

    Telemetry for ``meta.overlap`` only (stamps never refuse, trim or range-check
    anything — proposal_v2 amendment 3). ``None`` when no stamped row exists.
    ``exclude_hash`` leaves out the submitted chunk's own ``content_hash`` group, so
    a re-armed row never reports a 100% overlap with itself (review finding).
    """
    row = conn.execute(
        """
        SELECT max((meta->'flush'->>'offset')::bigint + (meta->'flush'->>'bytes')::bigint)
          FROM ingest_queue
         WHERE kind = 'session' AND btrim(meta->>'source_uri') = %s
           AND meta->>'submitted_by' = %s AND meta ? 'flush'
           AND (%s::text IS NULL OR content_hash <> %s)
        """,
        (uri, principal, exclude_hash, exclude_hash),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])

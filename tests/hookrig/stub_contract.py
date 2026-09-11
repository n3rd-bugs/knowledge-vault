#!/usr/bin/env python3
"""The KB submission contract, mirrored for the hook rig (stdlib only).

Everything the real server would decide about a submission lives here, copied
from the sources named against each constant so a drift between this stub and
kbcore is a visible, reviewable diff rather than a silently permissive test:

    ValueError -> 422 (shape: FastAPI/pydantic would refuse to build the model)
    Invalid    -> 400 (domain: kbcore.errors.InvalidOperation)
    otherwise  -> 202 {queue_id, duplicate, requeued, permanently_failed}

DEDUPE KEY (wave S, KB 33783/34161; proposal_v2 Appendix F). A transcript chunk
-- kind 'session' whose session_uri survives normalise_source_uri and starts with
'session:' (src/kbcore/queue_guard.py:56-64) -- is identified by
(session_uri, content_hash) across EVERY project (queue_guard.py:67-73 chunk_key;
queue_enqueue.py:98-113 _probe + queue_guard.py:85-98 find_prior). A resubmission
of that pair under another project is REFUSED and answered exactly like a
same-project live duplicate: queue_id null, duplicate true, no oracle
(queue_enqueue.py:63-95). Anything else -- no session_uri, a degenerate one
('session:' -> None, src/kbcore/queue_groups.py:55-83), kind 'memory' -- falls
back to the legacy arbiter UNIQUE (kind, project, content_hash)
(queue_enqueue.py:268-270). Before this re-key the stub deduped on sha256(text)
alone, LOOSER than the server, so "a second sweep is a no-op" passed for the
wrong reason (hybrid_v1 s2.7). NOT mirrored: the same-principal cross-session
probe (queue_guard.py:116-133, gated by KB_INGEST_XSESSION_GUARD) -- the rig
reuses one fixture across session ids inside a case on purpose.

FLUSH STAMPS (wave S item S3): the eight optional flat flush_* fields of
src/kbrest/schemas.py:80-87 are shape-checked the way pydantic would (422) and
otherwise recorded VERBATIM by the server plumbing; consistency
(src/kbcore/models.py:268-274) is recorded, never enforced -- stamps are
telemetry (proposal_v2 amendment 3).

The HTTP plumbing and the request recording live in stub_server.py.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import threading
import zlib
from typing import Any

# Contract constants, mirrored from the real server (kept in sync by name so a
# drift is obvious): src/kbcore/ingest_queue.py:22, src/kbcore/compress.py:21-22.
MAX_CONTENT_BYTES = 1_000_000
MAX_COMPRESSED_BYTES = 2_000_000
MAX_DECOMPRESSED_BYTES = 1_100_000
QUEUE_KINDS = ("session", "memory")
RESERVED_PROJECTS = ("kb-system",)
# src/kbcore/queue_guard.py:31 -- the chunk-key version prefix (compared, never parsed).
CHUNK_KEY_VERSION = "session-chunk/1"
# src/kbcore/models.py:288-290 -- a stamp whose bytes differ from the raw length
# by more than this is inconsistent (the row is stored, the stamp is not).
FLUSH_RAW_LEN_SLACK = 8
# src/kbrest/schemas.py:80-87 -- the closed stamp shapes, field by field:
# ints >= 0 (an upper cap where the schema has one), strings by pattern + length.
FLUSH_INT_FIELDS: dict[str, int | None] = {
    "flush_offset": None,
    "flush_bytes": 2 * MAX_CONTENT_BYTES,
    "flush_size": None,
    "flush_mtime": None,
}
FLUSH_STR_FIELDS: dict[str, tuple[int, str]] = {
    "flush_event": (16, r"^[A-Za-z]+$"),
    "flush_box": (64, r"^[A-Za-z0-9._-]+$"),
    "flush_project_by": (16, r"^[a-z-]+$"),
    "flush_branch_of": (64, r"^[A-Za-z0-9._-]+$"),
}
FLUSH_FIELDS = tuple(FLUSH_INT_FIELDS) + tuple(FLUSH_STR_FIELDS)


class Invalid(Exception):
    """Domain rejection -> 400 (REST) / isError (MCP), like kbcore.InvalidOperation."""


def decode_transcript_gz(blob: str) -> str:
    """base64(gzip(text)) -> text, with the real guards (kbcore.compress)."""
    if len(blob) > MAX_COMPRESSED_BYTES * 4 // 3 + 8:
        raise Invalid("compressed transcript exceeds the size limit")
    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError) as e:
        raise Invalid(f"transcript_gz is not valid base64: {e}") from e
    if len(raw) > MAX_COMPRESSED_BYTES:
        raise Invalid("compressed transcript exceeds the size limit")
    d = zlib.decompressobj(wbits=47)
    try:
        out = d.decompress(raw, MAX_DECOMPRESSED_BYTES + 1)
        out += d.flush()
    except zlib.error as e:
        raise Invalid(f"transcript_gz is not valid gzip: {e}") from e
    if len(out) > MAX_DECOMPRESSED_BYTES or d.unconsumed_tail:
        raise Invalid(
            f"decompressed transcript exceeds the {MAX_DECOMPRESSED_BYTES}-byte row limit"
        )
    if not d.eof or d.unused_data:
        raise Invalid("transcript_gz is truncated or has trailing data")
    return out.decode("utf-8", errors="replace")


def resolve_transcript(transcript: str | None, transcript_gz: str | None) -> str:
    if transcript_gz:
        if transcript:
            raise Invalid("pass transcript OR transcript_gz, not both")
        return decode_transcript_gz(transcript_gz)
    if not transcript:
        raise Invalid("a transcript (or transcript_gz) is required")
    return transcript


def validate_submission(payload: dict[str, Any]) -> tuple[str, str]:
    """Shape check (422 -> ValueError) then domain check (400 -> Invalid).

    Returns (project, text).
    """
    project = payload.get("project")
    if not isinstance(project, str):
        raise ValueError("project must be a string")
    for key in ("transcript", "transcript_gz", "kind", "repo", "branch", "commit", "session_uri"):
        if payload.get(key) is not None and not isinstance(payload[key], str):
            raise ValueError(f"{key} must be a string")
    kind = payload.get("kind") or "session"
    text = resolve_transcript(payload.get("transcript"), payload.get("transcript_gz"))
    if kind not in QUEUE_KINDS:
        raise Invalid(f"unknown ingest kind {kind!r}")
    if not project.strip():
        raise Invalid("project is required")
    if project.strip() in RESERVED_PROJECTS:
        raise Invalid(f"project {project!r} is reserved and cannot be targeted remotely")
    if not text.strip():
        raise Invalid("content is empty")
    if len(text.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise Invalid(f"content exceeds the {MAX_CONTENT_BYTES}-byte limit")
    return project, text


def _is_int(v: Any) -> bool:
    # An int or a digit string. STRICTER than pydantic lax on purpose: pydantic
    # also coerces an int-valued float (12.0 -> 12) and a bool (True -> 1); the
    # rig refuses both so a hook that ever emitted them would show up here,
    # never at the server. Nothing kb_json_build's `n` triples produce is affected.
    return (isinstance(v, int) and not isinstance(v, bool)) or (
        isinstance(v, str) and v.strip().isdigit()
    )


def validate_flush(payload: dict[str, Any]) -> dict[str, Any]:
    """Shape-check the flush_* stamp fields (422 -> ValueError, like pydantic).

    Mirrors src/kbrest/schemas.py:80-87 field by field: ints >= 0 (flush_bytes
    <= 2 * MAX_CONTENT_BYTES), strings against their closed pattern + max length
    (fullmatch: a trailing newline is refused exactly as the server's anchored
    `pattern=` refuses it), None allowed (= absent). One deliberate difference:
    the int check is STRICTER than pydantic lax (see _is_int) -- the stub never
    accepts what the server refuses, only refuses a little more. Returns the
    PRESENT fields VERBATIM -- a digit string
    stays a string in the record, so a case can see exactly what the hook put on
    the wire (the wave-C brief wants numbers sent as `n`, never as `s`).
    """
    out: dict[str, Any] = {}
    for key, cap in FLUSH_INT_FIELDS.items():
        v = payload.get(key)
        if v is None:
            continue
        if not _is_int(v) or int(v) < 0 or (cap is not None and int(v) > cap):
            raise ValueError(f"{key} must be an integer >= 0" + (f" and <= {cap}" if cap else ""))
        out[key] = v
    for key, (maxlen, pat) in FLUSH_STR_FIELDS.items():
        v = payload.get(key)
        if v is None:
            continue
        if not isinstance(v, str) or len(v) > maxlen or not re.fullmatch(pat, v):
            raise ValueError(f"{key} must match {pat} (max {maxlen} chars)")
        out[key] = v
    return out


def flush_consistent(stamp: dict[str, Any], raw_len: int) -> bool | None:
    """src/kbcore/models.py:268-274, assembled all-or-nothing like schemas.py:89-106.

    None when the stamp is partial (offset/bytes/size not all present -> the
    server places nothing); else whether offset + bytes fits size AND bytes is
    within FLUSH_RAW_LEN_SLACK of the raw content length. Recorded, never enforced.
    """
    if any(stamp.get(k) is None for k in ("flush_offset", "flush_bytes", "flush_size")):
        return None
    off, nbytes, size = (int(stamp[k]) for k in ("flush_offset", "flush_bytes", "flush_size"))
    return off + nbytes <= size and abs(nbytes - int(raw_len)) <= FLUSH_RAW_LEN_SLACK


def normalise_source_uri(value: str | None) -> str | None:
    """src/kbcore/queue_groups.py:55-83 verbatim: a degenerate identity is None.

    Degenerate = empty/whitespace, or a bare scheme with nothing after it
    ('session:', 'session://', 'file:/'). 'session:' is the wildcard grouping
    key that once coalesced six projects into one extraction.
    """
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    _scheme, sep, tail = v.partition(":")
    if sep and not tail.strip("/ \t"):
        return None
    return v


def is_transcript_chunk(kind: str, uri: str | None) -> bool:
    """src/kbcore/queue_guard.py:56-64: the ONLY rows the session key applies to."""
    return kind == "session" and uri is not None and uri.startswith("session:")


def chunk_key(uri: str, content_hash: str) -> str:
    """src/kbcore/queue_guard.py:67-73: the cross-project identity of ONE chunk."""
    return hashlib.sha256(f"{CHUNK_KEY_VERSION}|{uri}|{content_hash}".encode()).hexdigest()


class Queue:
    """The stub's stand-in for the durable queue: dedupe keys + queue ids.

    Two keyspaces, exactly like ingest_queue after migration 020:
      * ``chunks`` -- chunk_key(uri, hash) -> project of the canonical row (the
        020 ``ingest_queue_chunk_key_uidx`` arbiter, queue_guard.py:34);
      * ``legacy`` -- (kind, project, hash), the pre-020 UNIQUE that is still the
        arbiter for everything that is not a session chunk (queue_enqueue.py:270).
    """

    lock = threading.Lock()
    next_id = 0
    chunks: dict[str, str] = {}
    legacy: set[tuple[str, str, str]] = set()

    @classmethod
    def reset(cls) -> None:
        with cls.lock:
            cls.next_id = 0
            cls.chunks = {}
            cls.legacy = set()

    @classmethod
    def take(
        cls, *, kind: str, project: str, uri: str | None, content_hash: str
    ) -> tuple[int | None, str, str | None]:
        """(queue_id, dedupe_key, duplicate_kind).

        queue_id is None for a live duplicate; dedupe_key is 'session' (keyed on
        (uri, hash)) or 'legacy' ((kind, project, hash)); duplicate_kind is None,
        'same-project' or 'cross-project' -- for the HARNESS record only: the
        answer on the wire never distinguishes them (rules 10/12).
        """
        with cls.lock:
            if is_transcript_chunk(kind, uri):
                key = chunk_key(uri or "", content_hash)
                prior = cls.chunks.get(key)
                if prior is not None:
                    return None, "session", "same-project" if prior == project else "cross-project"
                cls.chunks[key] = project
                cls.next_id += 1
                return cls.next_id, "session", None
            lkey = (kind, project, content_hash)
            if lkey in cls.legacy:
                return None, "legacy", "same-project"
            cls.legacy.add(lkey)
            cls.next_id += 1
            return cls.next_id, "legacy", None


def enqueue_result(
    text: str, *, project: str, kind: str | None = None, session_uri: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the 202 body, harness-only dedupe facts for the record).

    The body is TODAY's four keys and nothing else -- the real server reports a
    live duplicate (or a guarded refusal) as queue_id=None, duplicate=true. The
    second dict carries ``dedupe_key`` / ``duplicate_kind`` /
    ``session_uri_normalised`` so a case can tell a cross-project refusal from a
    same-project repeat without the answer ever saying so (queue_enqueue.py:53-55:
    the REST route MUST NOT add resend_of).
    """
    uri = normalise_source_uri(session_uri)
    qid, key_kind, dup_kind = Queue.take(
        kind=kind or "session",
        project=project.strip(),
        uri=uri,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    body = {
        "queue_id": qid,
        "duplicate": qid is None,
        "requeued": False,
        "permanently_failed": False,
    }
    facts = {"dedupe_key": key_kind, "duplicate_kind": dup_kind, "session_uri_normalised": uri}
    return body, facts

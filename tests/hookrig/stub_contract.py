#!/usr/bin/env python3
"""The KB submission contract, mirrored for the hook rig (stdlib only).

Everything the real server would decide about a submission lives here, copied
from the sources named against each constant so a drift between this stub and
kbcore is a visible, reviewable diff rather than a silently permissive test:

    ValueError -> 422 (shape: FastAPI/pydantic would refuse to build the model)
    Invalid    -> 400 (domain: kbcore.errors.InvalidOperation)
    otherwise  -> 202 {queue_id, duplicate, requeued, permanently_failed}

The HTTP plumbing and the request recording live in stub_server.py.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
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
    for key in ("transcript", "transcript_gz", "kind", "repo", "branch", "commit"):
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


class Queue:
    """The stub's stand-in for the durable queue: dedupe hashes + queue ids."""

    lock = threading.Lock()
    next_id = 0
    hashes: set[str] = set()

    @classmethod
    def reset(cls) -> None:
        with cls.lock:
            cls.next_id = 0
            cls.hashes = set()

    @classmethod
    def take(cls, content_hash: str) -> int | None:
        """None if this content was already submitted (a live duplicate)."""
        with cls.lock:
            if content_hash in cls.hashes:
                return None
            cls.hashes.add(content_hash)
            cls.next_id += 1
            return cls.next_id


def enqueue_result(text: str) -> dict[str, Any]:
    qid = Queue.take(hashlib.sha256(text.encode("utf-8")).hexdigest())
    dup = qid is None
    # The real server reports a live duplicate as queue_id=None, duplicate=true.
    return {
        "queue_id": qid,
        "duplicate": dup,
        "requeued": False,
        "permanently_failed": False,
    }

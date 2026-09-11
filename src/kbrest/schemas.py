"""kbrest.schemas — request bodies for the REST write endpoints.

Read responses reuse the existing kbcore.models shapes directly; only the write
inputs need their own request models.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from kbcore.ingest_queue import MAX_CONTENT_BYTES
from kbcore.models import CodeRef, FlushStamp


class ItemCreate(BaseModel):
    project: str
    type: str
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)
    code_ref: CodeRef | None = None
    # Optional valid-time interval (WP1). ISO-8601 strings are coerced by
    # pydantic; ingest() normalizes naive values to UTC (Invariant 7).
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class SupersedeRequest(BaseModel):
    reason: str
    new_body: str | None = None
    new_item_id: int | None = None
    # Optional valid-time OVERRIDE for the replacement (new_body path only); when
    # omitted the replacement inherits the old item's interval (WP1). ISO-8601.
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class LinkRequest(BaseModel):
    src_item_id: int
    rel: str
    dst_item_id: int
    evidence_item_id: int | None = None


class ReviewResolveRequest(BaseModel):
    action: str  # promote | reject | supersede
    new_item_id: int | None = None


class IngestSessionRequest(BaseModel):
    project: str
    transcript: str | None = None
    # base64(gzip(transcript)) — for large-context submissions (~10x smaller on
    # the wire); mutually exclusive with transcript. Decoded with zip-bomb
    # guards in kbcore.compress.
    transcript_gz: str | None = None
    kind: str = "session"  # session | memory
    # Identity shared by every chunk of ONE session, so the drain can coalesce
    # them into a single extraction instead of one per 900 KB window.
    session_uri: str | None = None
    repo: str | None = None
    branch: str | None = None
    commit: str | None = None
    # The installed hook version (~/.claude/hooks/.setup-version, a manifest
    # digest), sent ONLY over REST by flush-session-to-kb.sh (review #8 H15).
    # Threaded into ingest_queue.meta['hook_version'] so fleet_by_box can say
    # WHICH boxes carry a given fix (the IPv4 pin, the H11 envelope gate) — the
    # only server-side way to know. Optional: pre-stamp hooks and MCP-transport
    # boxes send nothing, and that absence is itself the signal (fleet_health).
    # Closed shape (a digest is 12 hex); never trusted as anything but a label.
    hook_version: str | None = Field(default=None, max_length=64)
    # Flush TELEMETRY (wave S item S3, hybrid_v1 §1.3): eight optional, closed-shape,
    # FLAT fields — flat because the hook's JSON builder is flat and each value is a
    # number or a slug the rig can assert. Stored under ingest_queue.meta['flush']
    # by kbcore when consistent; NEVER a refusal, trim or range check (stamps are
    # telemetry only, proposal_v2 amendment 3). Pre-stamp hooks send nothing; an
    # older server drops them (plain BaseModel, extra='ignore').
    flush_offset: int | None = Field(default=None, ge=0)  # byte offset in the transcript
    flush_bytes: int | None = Field(default=None, ge=0, le=2 * MAX_CONTENT_BYTES)  # RAW length
    flush_size: int | None = Field(default=None, ge=0)  # transcript size at firing
    flush_event: str | None = Field(default=None, max_length=16, pattern=r"^[A-Za-z]+$")
    flush_mtime: int | None = Field(default=None, ge=0)  # epoch seconds; stored ISO UTC
    flush_box: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    flush_project_by: str | None = Field(default=None, max_length=16, pattern=r"^[a-z-]+$")
    flush_branch_of: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")

    def flush_stamp(self) -> FlushStamp | None:
        """The stamp, ALL-OR-NOTHING: offset + bytes + size present, else ``None``.

        A partial stamp is not an error (a hook mid-upgrade may send a subset); it
        is simply not telemetry the server can place, so it is dropped whole.
        """
        if self.flush_offset is None or self.flush_bytes is None or self.flush_size is None:
            return None
        return FlushStamp(
            offset=self.flush_offset,
            bytes=self.flush_bytes,
            size=self.flush_size,
            event=self.flush_event,
            mtime=self.flush_mtime,
            box=self.flush_box,
            project_by=self.flush_project_by,
            branch_of=self.flush_branch_of,
        )


class EnrolRequest(BaseModel):
    """Redeem a one-time enrolment code for this machine's own credential.

    The CODE is not in the body — it rides the Authorization header, so it is
    handled by exactly the same bearer path as every other credential and never
    lands in a request-body log. `machine` is a human label for the audit trail
    and for naming the per-machine principal.
    """

    machine: str | None = None

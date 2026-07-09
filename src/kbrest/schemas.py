"""kbrest.schemas — request bodies for the REST write endpoints.

Read responses reuse the existing kbcore.models shapes directly; only the write
inputs need their own request models.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from kbcore.models import CodeRef


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
    repo: str | None = None
    branch: str | None = None
    commit: str | None = None

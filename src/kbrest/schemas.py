"""kbrest.schemas — request bodies for the REST write endpoints.

Read responses reuse the existing kbcore.models shapes directly; only the write
inputs need their own request models.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from kbcore.models import CodeRef


class ItemCreate(BaseModel):
    project: str
    type: str
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)
    code_ref: CodeRef | None = None


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

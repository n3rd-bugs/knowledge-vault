"""kbworker.sessions — grounded session-transcript extraction, checkpointed (§7.2).

``ingest_session`` reads new bytes of a Claude Code ``.jsonl`` transcript from
the last checkpoint, assembles the new user/assistant text, runs the extractor
(kbcore.llm), and ingests only items whose ``quote`` is a verbatim substring of
that text (SEC-3/ARCH-2 — everything else is dropped and counted). Survivors are
``trust='unverified'`` and land in the review queue (Verdict V4). All SQL is in
kbcore; this module only orchestrates (Invariant 2).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from kbcore import llm
from kbcore.checkpoints import get_checkpoint, set_checkpoint
from kbcore.config import settings
from kbcore.db import transaction
from kbcore.ingest import ingest_in
from kbcore.locks import session_lock
from kbcore.models import ITEM_TYPES, SessionSummary
from kbcore.review import queue_review_in
from kbcore.scrub import scrub

logger = logging.getLogger("kbworker.sessions")

# Transcript roles we keep; tool machinery and system noise are skipped.
_KEEP_ROLES = frozenset({"user", "assistant"})


def _message_text(obj: dict[str, Any]) -> str:
    """Pull plain text out of one transcript JSON object, skipping tool noise."""
    role = obj.get("type") or obj.get("role")
    if role not in _KEEP_ROLES:
        return ""
    message = obj.get("message")
    message = message if isinstance(message, dict) else obj
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        # Keep prose text blocks; drop tool_use / tool_result and images.
        if block.get("type") in (None, "text") and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(p for p in parts if p.strip()).strip()


def assemble_transcript(data: bytes) -> str:
    """Assemble user/assistant text from a slice of a ``.jsonl`` transcript.

    Unparseable lines (e.g. a partially written trailing line) are skipped, so
    a mid-write read simply yields less text rather than crashing.
    """
    out: list[str] = []
    for line in data.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        text = _message_text(obj)
        if text:
            out.append(text)
    return "\n\n".join(out)


def _code_ref(project: str, repo: str | None, commit: str | None) -> dict[str, str] | None:
    """Stamp a code anchor from the session's git context (§7.2, DATA-9).

    ``commit`` + ``repo`` are what matter; path is ``.`` because a session spans
    a working tree, not one file.
    """
    if not repo and not commit:
        return None
    name = os.path.basename(repo.rstrip("/")) if repo else project
    ref = {"repo": name, "path": "."}
    if commit:
        ref["commit"] = commit
    return ref


def _ingest_and_queue(
    *,
    project: str,
    type_: str,
    title: str,
    body: str,
    uri: str,
    tags: list[str],
    meta: dict[str, Any],
    code_ref: dict[str, str] | None,
    raw: bytes,
    detail: dict[str, Any],
) -> int | None:
    """Insert the unverified item AND its review-queue row in ONE transaction.

    A crash between the two inserts must never leave an unverified extraction
    that is invisible to review (review finding).
    """
    with transaction() as conn:
        item_id = ingest_in(
            conn,
            project,
            type_,
            title,
            body,
            kind="session",
            uri=uri,
            trust="unverified",
            tags=tags,
            meta=meta,
            code_ref=code_ref,
            raw_content=raw,
        )
        if item_id is not None:
            queue_review_in(conn, item_id, "unverified_extraction", detail)
    return item_id


def ingest_session(
    path: str | Path,
    project: str,
    repo: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
) -> SessionSummary:
    """Extract and ingest new knowledge from a transcript window. Idempotent.

    Serialized per transcript across processes, reads from the checkpointed byte
    offset up to the last COMPLETE line, validates quote-grounding, ingests
    survivors as unverified session items (each atomically queued for review),
    and advances the checkpoint only after successful processing.
    """
    abs_path = Path(path).expanduser().resolve()
    key = str(abs_path)

    if not abs_path.exists():
        return SessionSummary(path=str(path), project=project, offset=get_checkpoint(key))

    # Hold the cross-process lock for the whole read→extract→ingest→checkpoint.
    with session_lock(key):
        offset = get_checkpoint(key)
        size = abs_path.stat().st_size
        if size < offset:
            # Transcript rotated/truncated below the checkpoint — reprocess from
            # the start rather than wedging forever on an empty read.
            logger.warning(
                "transcript %s shrank (%d < %d); reprocessing from start", abs_path, size, offset
            )
            offset = 0

        with abs_path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()

        # Consume only up to the last COMPLETE line so a half-written trailing
        # line is re-read (never lost) once the writer finishes it.
        consumed = data.rfind(b"\n") + 1
        if consumed == 0:
            return SessionSummary(path=str(path), project=project, offset=offset)
        chunk = data[:consumed]
        new_offset = offset + consumed

        transcript_text = assemble_transcript(chunk)
        if not transcript_text.strip():
            set_checkpoint(key, new_offset)
            return SessionSummary(path=str(path), project=project, offset=new_offset)

        raw_items = llm.extract_items(transcript_text)

        survivors: list[dict[str, Any]] = []
        rejected = 0
        for it in raw_items:
            quote = it.get("quote")
            if isinstance(quote, str) and quote and quote in transcript_text:
                survivors.append(it)
            else:
                rejected += 1  # SEC-3: ungrounded item dropped

        code_ref = _code_ref(project, repo, commit)
        # uri keyed by the canonical abs_path + consumed range so a relative and
        # an absolute invocation (or a retry) produce identical dedupe keys.
        uri = f"{key}:{offset}-{new_offset}"
        detail = {"rejected_count": rejected, "offset_range": f"{offset}-{new_offset}"}
        saved = 0
        for it in survivors:
            type_ = it.get("type") if it.get("type") in ITEM_TYPES else "note"
            title = (it.get("title") or "").strip() or "session note"
            body = (it.get("body") or "").strip()
            if not body:
                continue
            tags = [t for t in (it.get("tags") or []) if isinstance(t, str)]
            # Scrub the quote before storing it in meta: a secret pasted in chat
            # must not become searchable via meta.quote (SEC-2). Grounding above
            # already validated the pre-scrub quote against the transcript.
            scrubbed_quote, _ = scrub(it["quote"])
            meta: dict[str, Any] = {
                "quote": scrubbed_quote,
                "extractor": settings.kb_extract_model,
            }
            if branch:
                meta["branch"] = branch
            item_id = _ingest_and_queue(
                project=project,
                type_=type_,
                title=title,
                body=body,
                uri=uri,
                tags=tags,
                meta=meta,
                code_ref=code_ref,
                raw=chunk,
                detail=detail,
            )
            if item_id is not None:
                saved += 1

        set_checkpoint(key, new_offset)
        return SessionSummary(
            path=str(path), project=project, saved=saved, rejected=rejected, offset=new_offset
        )

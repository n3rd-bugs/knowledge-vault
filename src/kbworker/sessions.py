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


def _mostly_jsonl(text: str) -> bool:
    """True if the majority of non-empty lines parse as JSON — i.e. a real
    transcript, not prose that merely contains a stray JSON line."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    parsed = 0
    for ln in lines:
        try:
            json.loads(ln)
            parsed += 1
        except (ValueError, TypeError):
            pass
    return parsed >= max(1, (len(lines) + 1) // 2)


def assemble_text(data: bytes | str, kind: str = "session") -> str:
    """Text to extract from, for either a JSONL transcript OR a plain blob.

    ``kind='memory'`` is always treated as plain text. ``kind='session'`` uses
    JSONL assembly ONLY when the blob is actually mostly JSON lines — otherwise a
    prose 'session' (or a memory dump misrouted here) that happens to contain one
    message-shaped JSON line would collapse to that single line, silently
    dropping the rest (review finding).
    """
    plain = (data if isinstance(data, str) else data.decode("utf-8", errors="replace")).strip()
    if kind == "memory" or not _mostly_jsonl(plain):
        return plain
    raw = data.encode("utf-8") if isinstance(data, str) else data
    assembled = assemble_transcript(raw)
    return assembled if assembled.strip() else plain


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


def extract_and_ingest(
    text: str,
    project: str,
    uri: str,
    code_ref: dict[str, str] | None = None,
    branch: str | None = None,
    extractor=None,
    extra_detail: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """Extract → quote-validate → ingest, the core shared by file + queue paths.

    Runs ``extractor`` (the 14B) on ``text``, keeps only items whose ``quote`` is
    a verbatim substring (SEC-3/ARCH-2), and ingests survivors as unverified
    ``session`` items with a scrubbed ``meta.quote`` and a review-queue row.
    Returns ``(saved, rejected)``. The extractor defaults to ``llm.extract_items``
    resolved at CALL time (so tests can monkeypatch it), and is a parameter so
    callers own when the GPU runs — enqueue never calls it, only the nightly drain.
    """
    if extractor is None:
        extractor = llm.extract_items
    raw_items = extractor(text)
    survivors: list[dict[str, Any]] = []
    rejected = 0
    for it in raw_items:
        quote = it.get("quote")
        if isinstance(quote, str) and quote and quote in text:
            survivors.append(it)
        else:
            rejected += 1  # SEC-3: ungrounded item dropped

    raw = text.encode("utf-8")
    detail = {"rejected_count": rejected, **(extra_detail or {})}
    saved = 0
    for it in survivors:
        type_ = it.get("type") if it.get("type") in ITEM_TYPES else "note"
        title = (it.get("title") or "").strip() or "session note"
        body = (it.get("body") or "").strip()
        if not body:
            continue
        tags = [t for t in (it.get("tags") or []) if isinstance(t, str)]
        # Scrub the quote before storing it in meta: a secret in the source must
        # not become searchable via meta.quote (SEC-2). Grounding above already
        # validated the pre-scrub quote against the text.
        scrubbed_quote, _ = scrub(it["quote"])
        meta: dict[str, Any] = {"quote": scrubbed_quote, "extractor": settings.kb_extract_model}
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
            raw=raw,
            detail=detail,
        )
        if item_id is not None:
            saved += 1
    return saved, rejected


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

        # uri keyed by the canonical abs_path + consumed range so a relative and
        # an absolute invocation (or a retry) produce identical dedupe keys.
        uri = f"{key}:{offset}-{new_offset}"
        saved, rejected = extract_and_ingest(
            transcript_text,
            project,
            uri,
            code_ref=_code_ref(project, repo, commit),
            branch=branch,
            extra_detail={"offset_range": f"{offset}-{new_offset}"},
        )
        set_checkpoint(key, new_offset)
        return SessionSummary(
            path=str(path), project=project, saved=saved, rejected=rejected, offset=new_offset
        )

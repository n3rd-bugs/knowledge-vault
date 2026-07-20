"""kbworker.sessions — grounded session-transcript extraction, checkpointed (§7.2).

``ingest_session`` reads new bytes of a Claude Code ``.jsonl`` transcript from
the last checkpoint, assembles the new user/assistant text, runs the extractor
(kbcore.llm), and ingests only items whose ``quote`` is a verbatim substring of
that text (SEC-3/ARCH-2 — near-miss quotes get one fuzzy-relocation attempt via
kbcore.quote_repair, everything else is dropped and counted). Survivors are
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
from kbcore.errors import InvalidOperation
from kbcore.ingest import ingest_in
from kbcore.locks import session_lock
from kbcore.models import ITEM_TYPES, SessionSummary
from kbcore.quote_repair import find_span
from kbcore.review import queue_review_in
from kbcore.scrub import scrub

logger = logging.getLogger("kbworker.sessions")

# Transcript roles we keep; tool machinery and system noise are skipped.
_KEEP_ROLES = frozenset({"user", "assistant"})

# Large-session flushing: per-queue-row window (under ingest_queue's 1 MB row
# cap) and a total-delta ceiling. 1 GB ≈ 1,200 windows — drained at
# QUEUE_BATCH/night by consolidation, a natural throttle on extraction cost.
QUEUE_WINDOW_BYTES = 900_000
MAX_DELTA_BYTES = 1_073_741_824  # 1 GB


def _text_windows(text: str, max_bytes: int) -> list[str]:
    """Split text into UTF-8 windows of at most ``max_bytes``, on line boundaries.

    A single line longer than the window is emitted alone (mid-line split —
    unavoidable, and the drain's line parser skips at most that one line).
    Raises when the total exceeds :data:`MAX_DELTA_BYTES`.
    """
    if len(text.encode("utf-8")) > MAX_DELTA_BYTES:
        raise InvalidOperation(f"session delta exceeds the {MAX_DELTA_BYTES}-byte (1 GB) limit")
    windows: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        n = len(line.encode("utf-8"))
        if n > max_bytes:
            # A single monster line: flush what we have, then hard-split it on
            # UTF-8-safe byte boundaries (a row must fit the queue's 1 MB cap).
            if current:
                windows.append("".join(current))
                current, size = [], 0
            windows.extend(_hard_split(line, max_bytes))
            continue
        if size + n > max_bytes and current:
            windows.append("".join(current))
            current, size = [], 0
        current.append(line)
        size += n
    if current:
        windows.append("".join(current))
    return windows or [""]


def _hard_split(line: str, max_bytes: int) -> list[str]:
    """Byte-cap a single oversized line, never cutting inside a UTF-8 sequence."""
    data = line.encode("utf-8")
    out: list[str] = []
    i = 0
    while i < len(data):
        j = min(i + max_bytes, len(data))
        while j > i:  # back off to a codepoint boundary (≤3 continuation bytes)
            try:
                out.append(data[i:j].decode("utf-8"))
                break
            except UnicodeDecodeError:
                j -= 1
        i = j
    return out


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
            # Decode to str before json.loads: handing raw bytes to json runs
            # json.detect_encoding() on the first 4 bytes, which mis-guesses
            # UTF-16/32 for a line with leading NULs (e.g. a corrupt/partial
            # write) and raises UnicodeDecodeError — NOT a JSONDecodeError, so it
            # would crash the whole ingest. Decoding as UTF-8 (the transcript's
            # real encoding) first sidesteps the sniffer; a garbage line then
            # fails as a normal JSONDecodeError and is skipped.
            obj = json.loads(line.decode("utf-8", "replace"))
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


def gate_quotes(
    raw_items: list[dict[str, Any]], text: str, min_ratio: float
) -> tuple[list[tuple[dict[str, Any], dict[str, Any] | None]], int]:
    """SEC-3 quote gate: exact substring → grounded; near-miss → ONE fuzzy
    relocation attempt (repaired: the LOCATED span replaces the model quote);
    anything else is dropped and counted.

    Returns ``(survivors, rejected)`` where each survivor is ``(item, repair)`` —
    ``repair`` is None for exact quotes, else the audit record ``{"ratio": r,
    "model_quote": <original, scrubbed at store time>}``. This is THE quote gate,
    shared by pipeline v1 and the WP-B2 ensemble (kbworker.ensemble) so the two
    paths can never drift.
    """
    survivors: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    rejected = 0
    for it in raw_items:
        quote = it.get("quote")
        if not isinstance(quote, str) or not quote:
            rejected += 1  # SEC-3: no grounding quote at all
            continue
        if quote in text:
            survivors.append((it, None))
            continue
        located = find_span(quote, text, min_ratio) if min_ratio > 0 else None
        if located is None:
            rejected += 1  # SEC-3: ungrounded and unrepairable — dropped
            continue
        span, ratio = located
        # The LOCATED span IS a verbatim substring of the transcript, so the item
        # proceeds without weakening SEC-3; the model's near-miss is kept only as
        # audit metadata (scrubbed below — SEC-2 applies to hallucinated text too).
        survivors.append(({**it, "quote": span}, {"ratio": round(ratio, 4), "model_quote": quote}))
    return survivors, rejected


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
    extra_meta: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """Extract → quote-validate/repair → ingest, the core shared by file + queue paths.

    ``extra_detail`` reaches only the review-queue row's detail; ``extra_meta``
    keys are additionally stamped into each saved ITEM's meta (setdefault —
    pipeline-owned keys always win) so provenance like ``reprocessed_from``
    (kbcore.reprocess) survives on the item after the review row resolves.

    Runs ``extractor`` (the 14B) on ``text`` and keeps items whose ``quote`` is a
    verbatim substring (SEC-3/ARCH-2). A quote that fails the exact check gets ONE
    fuzzy-relocation attempt (:func:`kbcore.quote_repair.find_span`, measured
    survival 46%→92%); on success the item proceeds with the LOCATED span — itself
    a verbatim substring, so SEC-3 holds by construction — and ``meta.quote_repair``
    records the ratio plus the scrubbed original model quote for audit. Kill
    switch: ``kb_quote_repair_min_ratio=0`` (unrepaired quotes reject as before).
    Survivors ingest as unverified ``session`` items with a scrubbed ``meta.quote``
    and a review-queue row. Returns ``(saved, rejected)``. The extractor defaults
    to ``llm.extract_items`` resolved at CALL time (so tests can monkeypatch it),
    and is a parameter so callers own when the GPU runs — enqueue never calls it,
    only the nightly drain.

    Pipeline v2 ensemble (WP-B2): with ``kb_extract_ensemble`` EMPTY (the
    default) this is pipeline v1 exactly as above — no ensemble code runs or is
    even imported. Non-empty + ``kb_ensemble_shadow`` (default true): v1 ingests
    as today, then the ensemble computes and LOGS its counts, writing nothing
    (any shadow failure is swallowed — it must never affect v1's outcome).
    Non-empty + shadow false: the ensemble REPLACES v1 — survivors ingest via
    :func:`_ingest_ensemble_survivors` and ``extractor`` is ignored (the
    ensemble owns model selection).
    """
    ensemble_on = bool(settings.extract_ensemble)
    if ensemble_on and not settings.kb_ensemble_shadow:
        from kbworker import ensemble  # lazy: the v1 path must not depend on it

        report = ensemble.run_ensemble(
            text, project, uri, code_ref=code_ref, branch=branch, extra_detail=extra_detail
        )
        return _ingest_ensemble_survivors(report, text, extra_meta=extra_meta)

    if extractor is None:
        extractor = llm.extract_items
    raw_items = extractor(text)
    # (item, repair) pairs: repair is None for exact quotes, else the audit record
    # {"ratio": r, "model_quote": <original, scrubbed at store time>}.
    survivors, rejected = gate_quotes(raw_items, text, settings.kb_quote_repair_min_ratio)

    raw = text.encode("utf-8")
    detail = {"rejected_count": rejected, **(extra_detail or {})}
    saved = 0
    for it, repair in survivors:
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
        if repair is not None:
            # Same scrub for the ORIGINAL model quote: a secret in a hallucinated
            # quote must not become searchable either (SEC-2).
            scrubbed_model_quote, _ = scrub(repair["model_quote"])
            meta["quote_repair"] = {
                "ratio": repair["ratio"],
                "model_quote": scrubbed_model_quote,
            }
        if branch:
            meta["branch"] = branch
        for k, v in (extra_meta or {}).items():
            meta.setdefault(k, v)  # provenance stamp (kb reprocess); pipeline keys win
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

    if ensemble_on:  # shadow mode: v1 stood above; compute + LOG only, write nothing
        from kbworker import ensemble

        try:
            report = ensemble.run_ensemble(
                text, project, uri, code_ref=code_ref, branch=branch, extra_detail=extra_detail
            )
            ensemble.record_shadow(report, v1_saved=saved)
        except Exception:  # noqa: BLE001 — shadow must never affect v1's outcome
            logger.exception("ensemble shadow run failed; v1 result stands")
    return saved, rejected


def _ingest_ensemble_survivors(
    report: dict[str, Any], text: str, extra_meta: dict[str, Any] | None = None
) -> tuple[int, int]:
    """Live-mode ensemble ingest: survivors flow through the SAME path as v1.

    Same :func:`_ingest_and_queue` transaction (trust='unverified' + review row),
    same scrub-before-store rules for the quote and the repaired model quote
    (SEC-2), plus ``meta.extraction_pipeline`` recording provenance (found_by /
    corroboration / quote_state / verified_by — WP-B2). ``rejected`` counts
    unanchored ensemble items (the SEC-3 quote gate runs before any policy) plus
    representatives that failed verification (judge-fail / judge-error).
    Idempotent like v1: same window re-run dedupes on (kind, uri, content_hash).
    """
    raw = text.encode("utf-8")
    rejected = report["unanchored"] + report["rejected_reps"]
    detail = {"rejected_count": rejected, **(report.get("extra_detail") or {})}
    branch = report.get("branch")
    saved = 0
    for surv in report["survivors"]:
        it = surv["item"]
        type_ = it.get("type") if it.get("type") in ITEM_TYPES else "note"
        title = (it.get("title") or "").strip() or "session note"
        body = (it.get("body") or "").strip()
        if not body:
            continue
        tags = [t for t in (it.get("tags") or []) if isinstance(t, str)]
        # Same SEC-2 rules as v1: scrub the stored quote (and the model's
        # near-miss original, below) so a secret never becomes searchable.
        scrubbed_quote, _ = scrub(it["quote"])
        meta: dict[str, Any] = {
            "quote": scrubbed_quote,
            "extractor": surv["model"],
            "extraction_pipeline": surv["extraction_pipeline"],
        }
        if surv["repair"] is not None:
            scrubbed_model_quote, _ = scrub(surv["repair"]["model_quote"])
            meta["quote_repair"] = {
                "ratio": surv["repair"]["ratio"],
                "model_quote": scrubbed_model_quote,
            }
        if branch:
            meta["branch"] = branch
        for k, v in (extra_meta or {}).items():
            meta.setdefault(k, v)  # provenance stamp (kb reprocess); pipeline keys win
        item_id = _ingest_and_queue(
            project=report["project"],
            type_=type_,
            title=title,
            body=body,
            uri=report["uri"],
            tags=tags,
            meta=meta,
            code_ref=report.get("code_ref"),
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
    queue: bool = False,
) -> SessionSummary:
    """Extract and ingest new knowledge from a transcript window. Idempotent.

    Serialized per transcript across processes, reads from the checkpointed byte
    offset up to the last COMPLETE line, validates quote-grounding, ingests
    survivors as unverified session items (each atomically queued for review),
    and advances the checkpoint only after successful processing.

    With ``queue=True`` the transcript window is instead ENQUEUED for the nightly
    drain (no GPU / 14B at call time) — the courtesy path for a PreCompact hook
    that must return instantly (Invariant 8). The checkpoint still advances, so
    each compaction enqueues only its new delta.
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

        # Courtesy path: enqueue the delta for the nightly drain instead of running
        # the 14B now. Large-session support (owner request, up to 1 GB): the delta
        # is split into per-row windows under the queue's 1 MB cap — each window is
        # its own queue row (extracted independently by the nightly drain), so big
        # sessions no longer wedge the checkpoint on the cap. Advance the
        # checkpoint only after ALL windows enqueued.
        if queue:
            from kbcore import ingest_queue

            windows = _text_windows(transcript_text, QUEUE_WINDOW_BYTES)
            saved = 0
            for i, win in enumerate(windows):
                if not win.strip():
                    continue  # a whitespace-only window carries no knowledge
                part = f"#p{i}" if len(windows) > 1 else ""
                res = ingest_queue.enqueue(
                    project,
                    win,
                    kind="session",
                    repo=repo,
                    branch=branch,
                    commit=commit,
                    source_uri=f"{uri}{part}",
                    allow_reserved=True,  # trusted local flush may target kb-system
                )
                if res.id is not None:
                    saved += 1
            set_checkpoint(key, new_offset)
            return SessionSummary(
                path=str(path),
                project=project,
                saved=saved,
                rejected=0,
                offset=new_offset,
            )

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

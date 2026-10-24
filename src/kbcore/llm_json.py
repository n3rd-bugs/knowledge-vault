"""kbcore.llm_json — parsing, salvage and retry of the extractor's JSON output.

Split from :mod:`kbcore.llm` (2026-09-12) after one ~900 KB window of a nightly
cohort came back as malformed JSON (an unescaped double quote inside a string
value), ``extract_items`` raised :class:`ExtractionFailed`, the per-job fallback
replanned from the SAME prefetch and failed identically, and 151 rows / 100 MB
flipped to ``failed`` — with the fixed ``EXTRACT_SEED`` guaranteeing the next two
nightlies would reproduce the byte-identical bad output and burn the attempts cap.

Layers, tried in order by :func:`recover_items`: strict :func:`parse_model_json`
(the documented seam, re-exported from ``kbcore.llm``) → ONE re-ask with seed+1
and a repair hint (``kb_extract_json_retry``) → :func:`salvage_items` (keep every
``"items"`` element that parses on its own, drop the one(s) that do not).

Every unparseable output is dumped under ``$KB_STATE_DIR/extract-failures``
(0700/0600, rotated to ``kb_extract_failure_dumps`` newest) — journald cannot
carry a 900 KB body. Every window of raw output that reaches a log line or an
exception message is scrubbed first: a quote may echo a credential (rule 12).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kbcore.config import settings
from kbcore.errors import ExtractionFailed
from kbcore.scrub import scrub
from kbcore.unicode_safe import clean_obj, clean_result

logger = logging.getLogger(__name__)

# Process-local outcome counters, copied into the drain report. Keys are only ADDED.
JSON_COUNTERS: dict[str, int] = {"retry_ok": 0, "salvaged": 0, "failed": 0}

# Reasoning models (deepseek-r1, qwen3 with thinking on, ...) may wrap or prefix
# the answer with a <think>...</think> block, and some models fence the JSON in
# markdown despite format="json". Strip/recover instead of failing the batch.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


@clean_result  # a lone "\ud834" escape must not reach a UTF-8 encoder (2026-10-18)
def parse_model_json(content: str) -> Any:
    """Parse model output into JSON, tolerating reasoning-model wrappers.

    Order: strip <think> blocks → direct parse → fenced ```json``` block →
    outermost {...} span. Raises ``ValueError`` if no JSON object can be
    recovered (callers map that to their own failure mode).
    """
    text = _THINK_RE.sub("", content).strip()
    # An UNCLOSED <think> means the model hit its token budget mid-reasoning;
    # anything after it is reasoning, not an answer — do not mine JSON from it.
    if text.lstrip().lower().startswith("<think>"):
        raise ValueError("extractor output is an unterminated <think> block")
    try:
        return json.loads(text)
    except ValueError:
        pass
    fence = _FENCE_RE.search(text)
    if fence:
        try:
            return json.loads(fence.group(1))
        except ValueError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])  # ValueError propagates
    raise ValueError("no JSON object found in extractor output")


def items_of(data: Any) -> list[dict[str, Any]]:
    """The extractor schema's item dicts out of a parsed reply. ``{"items": null}``
    / a bare non-object → ``[]`` (never a TypeError); non-dict entries dropped."""
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict)]


# --- Salvage --------------------------------------------------------------------

_ITEMS_ARRAY_RE = re.compile(r'"items"\s*:\s*\[')
# Where the NEXT item may start after a broken one: a `{` that follows a comma
# (`}, {` between items, `foo, {` after a stray token). A `{` after a colon is a
# nested object inside an item, never an item boundary.
_RESUME_RE = re.compile(r",\s*(\{)")
_ITEM_KEYS = ("title", "body", "quote")


def _looks_like_item(obj: Any) -> bool:
    """At least one schema field — a `{"b": 2}` fished out of a body is not an item."""
    return isinstance(obj, dict) and any(k in obj for k in _ITEM_KEYS)


def salvage_items(content: str) -> dict[str, Any] | None:
    """LAST resort: keep every ``"items"`` element that parses on its own.

    Locates the extractor schema's ``"items"`` array and walks it with
    ``raw_decode`` from each ``{`` at array depth 1. An element that fails to
    decode (an unescaped inner quote, a truncated last item) is skipped to the
    next ``, {`` boundary; a stray token between items is skipped the same way.
    Returns ``{"items": [...]}`` when at least one item survived, else ``None``
    (the caller then fails exactly as before).
    """
    text = _THINK_RE.sub("", content)
    if text.lstrip().lower().startswith("<think>"):
        return None  # reasoning, not an answer (same rule as parse_model_json)
    m = _ITEMS_ARRAY_RE.search(text)
    if not m:
        return None
    decoder = json.JSONDecoder()
    items: list[dict[str, Any]] = []
    i, n = m.end(), len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        # The array ends only at a `]` followed by `}` or nothing; a `]` inside the
        # garbage of a broken item (a body quoting a JSON array) falls through.
        if text[i] == "]" and text[i + 1 :].lstrip()[:1] in ("", "}"):
            break
        if text[i] == "{":
            try:
                obj, end = decoder.raw_decode(text, i)
            except ValueError:
                pass
            else:
                if _looks_like_item(obj):
                    items.append(clean_obj(obj))  # same rule as parse_model_json
                i = end
                continue
        nxt = _RESUME_RE.search(text, i + 1)
        if not nxt:
            break
        i = nxt.start(1)
    return {"items": items} if items else None


# --- Diagnostics ----------------------------------------------------------------

_CONTEXT_CHARS = 200
_ERR_CHARS = 120


def _span(content: str) -> str:
    """Approximate the text ``parse_model_json`` last handed to ``json.loads``,
    so a ``JSONDecodeError.pos`` lands on the right region of ``content``."""
    text = _THINK_RE.sub("", content).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def describe_json_error(content: Any, err: BaseException) -> str:
    """One SCRUBBED line, <= 200 chars, around the offending position (or the tail
    when the error is at the very end — a truncated reply), newlines collapsed.
    Safe for journald and for ``ingest_queue.error``; tolerates a non-str reply."""
    if not isinstance(content, str):
        content = repr(content)
    text = _span(content)
    pos = getattr(err, "pos", None)
    half = (_CONTEXT_CHARS - 2) // 2
    if not isinstance(pos, int) or pos >= len(text) - 1:
        # Truncation / no position: the RAW tail is what the model last emitted —
        # the {...} span would hide everything after its last closing brace.
        raw = _THINK_RE.sub("", content).strip()
        window, prefix, suffix = raw[-(2 * half) :], "…" if len(raw) > 2 * half else "", ""
    else:
        lo, hi = max(0, pos - half), min(len(text), pos + half)
        window, prefix, suffix = text[lo:hi], "…" if lo else "", "…" if hi < len(text) else ""
    clean, _ = scrub(" ".join(window.split()))
    return (prefix + clean + suffix)[:_CONTEXT_CHARS]


def _short(err: BaseException) -> str:
    return f"{err.__class__.__name__}: {' '.join(str(err).split())}"[:_ERR_CHARS]


def repair_hint(err: BaseException) -> str:
    """System-prompt suffix for the re-ask, naming what went wrong last time."""
    return (
        f"\n\nYour previous answer was not valid JSON ({_short(err)}). Return STRICT "
        'JSON only, with every double quote inside a string escaped as \\".'
    )


def record_unparseable(model: str, content: str, err: BaseException) -> Path | None:
    """Dump the raw output + a header for post-mortem; best-effort, never raises.

    ``<kb_state_dir>/extract-failures/<utcstamp>-<model-slug>-<sha8>.txt``, dir
    0700 / file 0600, keeping ``kb_extract_failure_dumps`` newest (0 disables).
    Debug artefacts, not knowledge (outside rule 5); body scrubbed like ingest.
    """
    keep = settings.kb_extract_failure_dumps
    if keep <= 0:
        return None
    try:
        root = Path(settings.kb_state_dir).expanduser() / "extract-failures"
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)  # mkdir's mode is umask-subject; our own dir, so narrow it
        now = datetime.now(UTC)
        slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-") or "model"
        sha8 = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:8]
        path = root / f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{slug}-{sha8}.txt"
        body, _ = scrub(content)
        header = (
            f"# utc: {now.isoformat()}\n# model: {model}\n"
            f"# error: {_short(err)}\n# len: {len(content)}\n\n"
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(header + body)
        for old in sorted(root.glob("*.txt"), key=lambda p: p.name)[:-keep]:
            old.unlink(missing_ok=True)
        return path
    except Exception:  # noqa: BLE001 — a debug dump must never fail an extraction
        logger.debug("extract-failure dump for %s failed", model, exc_info=True)
        return None


# --- Orchestration ----------------------------------------------------------------


def _try_parse(content: str) -> tuple[list[dict[str, Any]] | None, BaseException | None]:
    try:
        return items_of(parse_model_json(content)), None
    except (ValueError, TypeError) as e:
        return None, e


def _re_ask(model: str, ask_again: Callable[[str], str], err: BaseException) -> str | None:
    """The ONE re-ask. A content-class failure of the re-ask itself (ReadTimeout,
    no ``message.content``) yields ``None`` so the first reply still gets salvaged
    instead of burning the attempt; :class:`ExtractorUnavailable` propagates."""
    try:
        return ask_again(repair_hint(err))
    except ExtractionFailed as e2:
        logger.warning("extractor %s: JSON re-ask failed (%s); salvaging first reply", model, e2)
        return None


def _salvage(model: str, content: str, origin: str) -> list[dict[str, Any]] | None:
    salvaged = salvage_items(content)
    if salvaged is None:
        return None
    JSON_COUNTERS["salvaged"] += 1
    got = items_of(salvaged)
    logger.info("extractor %s: salvaged %d item(s) from %s", model, len(got), origin)
    return got


def recover_items(
    model: str, first_content: str, ask_again: Callable[[str], str]
) -> list[dict[str, Any]]:
    """Items out of an extractor reply, recovering from malformed JSON.

    Strict parse → (if ``kb_extract_json_retry`` > 0) ONE re-ask with
    :func:`repair_hint`, parsed + salvaged → salvage of the FIRST reply → raise
    :class:`ExtractionFailed` with the scrubbed context. A re-ask that parses
    cleanly but yields NO items is inconclusive (the first reply may still salvage)
    and wins only when nothing salvages. Counted in :data:`JSON_COUNTERS`.
    """
    items, err = _try_parse(first_content)
    if err is None:
        return items or []
    ctx = describe_json_error(first_content, err)
    logger.warning("extractor %s returned unparseable output (%s): %s", model, _short(err), ctx)
    record_unparseable(model, first_content, err)
    clean_empty = False
    if settings.kb_extract_json_retry > 0:
        second = _re_ask(model, ask_again, err)
        if second is not None:
            items, err2 = _try_parse(second)
            if err2 is None and items:
                JSON_COUNTERS["retry_ok"] += 1
                logger.info("extractor %s: re-ask parsed cleanly, %d item(s)", model, len(items))
                return items
            clean_empty = err2 is None
            if err2 is not None:
                record_unparseable(model, second, err2)
                got = _salvage(model, second, "the re-ask")
                if got is not None:
                    return got
    got = _salvage(model, first_content, "the first reply")
    if got is not None:
        return got
    if clean_empty:
        JSON_COUNTERS["retry_ok"] += 1
        logger.info("extractor %s: JSON re-ask parsed cleanly with no items", model)
        return []
    JSON_COUNTERS["failed"] += 1
    raise ExtractionFailed(f"extractor returned unparseable output: {_short(err)}: {ctx}") from err

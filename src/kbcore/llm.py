"""kbcore.llm — Ollama /api/chat extractor client (PLAN.md §7.2, Invariant 8).

The nightly 14B extractor turns a session transcript into a list of grounded
knowledge items. Every call sets ``keep_alive=0`` so the model unloads
immediately after use — GPU is a guest and the extractor must not sit resident
(Invariant 8 / OPS-1). Network/format failures become an
:class:`ExtractorUnavailable` domain error; raw traces stay in the logs.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from kbcore.config import settings
from kbcore.errors import ExtractorUnavailable

# Reasoning models (deepseek-r1, qwen3 with thinking on, ...) may wrap or prefix
# the answer with a <think>...</think> block, and some models fence the JSON in
# markdown despite format="json". Strip/recover instead of failing the batch.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


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


# The 14B can take a while on first-token; generous read timeout, tight connect.
_TIMEOUT = httpx.Timeout(600.0, connect=10.0)
# Invariant 8: unload the extractor the instant the call returns.
KEEP_ALIVE = 0

# The prompt demands the grounded schema from §7.2. Every item MUST carry a
# ``quote`` that is a verbatim substring of the transcript — the caller
# re-validates and drops anything that is not (SEC-3 / ARCH-2). We only *ask*
# here; grounding is enforced downstream, never trusted from the model.
SYSTEM_PROMPT = (
    "You extract durable software-engineering knowledge from a chat transcript.\n"
    "Return STRICT JSON only, shaped exactly like:\n"
    '{"items": [{"type": "...", "title": "...", "body": "...", '
    '"quote": "<verbatim substring of the transcript>", "tags": ["lang:...", "hw:..."]}]}\n\n'
    "Rules:\n"
    "- type is one of: decision, note, qa, snippet, gotcha, bug, spec, pattern, "
    "runbook, config.\n"
    "- Capture decisions made, gotchas hit, reusable snippets, and bug/fix pairs. "
    "Skip greetings, tool output, and chit-chat.\n"
    "- quote MUST be copied character-for-character from the transcript (a real "
    "substring). If you cannot ground an item in an exact quote, omit that item.\n"
    "- title is one line; body is self-contained prose.\n"
    "- tags are lowercase, hyphenated, namespaced (lang:, hw:, protocol:, "
    "framework:, tool:, os:).\n"
    '- Return {"items": []} if there is nothing worth keeping.'
)


def extract_items(transcript_text: str) -> list[dict[str, Any]]:
    """Call the extractor on a transcript and return the raw item dicts.

    The returned dicts are UNVALIDATED — the caller must verify each ``quote``
    is a verbatim substring of ``transcript_text`` before trusting the item.
    """
    if not transcript_text.strip():
        return []
    payload: dict[str, Any] = {
        "model": settings.kb_extract_model,
        "format": "json",
        "keep_alive": KEEP_ALIVE,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": transcript_text},
        ],
    }
    try:
        resp = httpx.post(f"{settings.ollama_url}/api/chat", json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise ExtractorUnavailable(
            f"extraction call to {settings.ollama_url} failed: {e.__class__.__name__}: {e}"
        ) from e

    try:
        content = resp.json()["message"]["content"]
        data = parse_model_json(content)
    except (KeyError, ValueError, TypeError) as e:
        raise ExtractorUnavailable(
            f"extractor returned unparseable output: {e.__class__.__name__}: {e}"
        ) from e

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        # {"items": null} / {"items": "x"} / bare non-object → nothing usable.
        # Coerce to empty rather than letting a TypeError escape (review finding).
        return []
    return [it for it in items if isinstance(it, dict)]


def _chat(messages: list[dict[str, str]], *, json_format: bool) -> str:
    """POST /api/chat with keep_alive=0 (Invariant 8); return the message content."""
    payload: dict[str, Any] = {
        "model": settings.kb_extract_model,
        "keep_alive": KEEP_ALIVE,
        "stream": False,
        "messages": messages,
    }
    if json_format:
        payload["format"] = "json"
    try:
        resp = httpx.post(f"{settings.ollama_url}/api/chat", json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()["message"]["content"]
    except httpx.HTTPError as e:
        raise ExtractorUnavailable(
            f"extraction call to {settings.ollama_url} failed: {e.__class__.__name__}: {e}"
        ) from e
    except (KeyError, ValueError, TypeError) as e:
        raise ExtractorUnavailable(
            f"extractor returned unusable output: {e.__class__.__name__}: {e}"
        ) from e


_SUMMARIZE_SYSTEM = (
    "You write a concise, factual summary of what a project knows about one topic, "
    "grounded ONLY in the items provided. Preserve specific identifiers, values, part "
    "numbers, and decisions verbatim; do not speculate or add outside facts. One or two "
    "short paragraphs, no preamble."
)


def summarize_items(topic: str, items: list[dict[str, Any]]) -> str:
    """Grounded synthesis of a topic's source items into a summary body (§7.3 step 2)."""
    if not items:
        return ""
    corpus = "\n\n".join(f"### {it.get('title', '')}\n{it.get('body', '')}".strip() for it in items)
    content = _chat(
        [
            {"role": "system", "content": _SUMMARIZE_SYSTEM},
            {
                "role": "user",
                "content": f"Topic: {topic}\n\nItems:\n{corpus}\n\nWrite the summary.",
            },
        ],
        json_format=False,
    )
    return content.strip()


_EVIDENCE_SYSTEM = (
    "Two knowledge items (A and B) contradict each other. You are given a third "
    "item as evidence. Decide which side the evidence supports. Answer STRICT "
    'JSON only: {"side": "a"} or {"side": "b"} or {"side": "neither"}. '
    'If the evidence is off-topic, ambiguous, or supports both, answer "neither".'
)


def evidence_side(
    a_title: str, a_body: str, b_title: str, b_body: str, c_title: str, c_body: str
) -> str:
    """Which side of a contradiction the candidate item supports: 'a'|'b'|'neither'.

    Mirrors :func:`items_contradict`: an unparseable or unexpected answer degrades
    to 'neither' (never raises on ambiguity); genuine network failures surface as
    :class:`ExtractorUnavailable` so the nightly step can log and continue.
    """
    content = _chat(
        [
            {"role": "system", "content": _EVIDENCE_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Item A: {a_title}\n{a_body}\n\n"
                    f"Item B: {b_title}\n{b_body}\n\n"
                    f"Evidence: {c_title}\n{c_body}"
                ),
            },
        ],
        json_format=True,
    )
    try:
        data = parse_model_json(content)
    except (ValueError, TypeError):
        return "neither"
    if not isinstance(data, dict):
        return "neither"
    side = str(data.get("side", "")).strip().lower()
    return side if side in ("a", "b") else "neither"


_CONTRADICT_SYSTEM = (
    "You compare two knowledge items and decide whether they factually CONTRADICT — "
    "state incompatible facts or decisions about the same thing (not merely different "
    'topics). Answer STRICT JSON only: {"contradict": true} or {"contradict": false}. '
    "If you are unsure, answer false."
)


def items_contradict(a_title: str, a_body: str, b_title: str, b_body: str) -> bool:
    """True iff the extractor judges the two items to conflict (§7.3 step 3).

    Never raises on an ambiguous/unparseable answer — defaults to False. Genuine
    network failures still surface as :class:`ExtractorUnavailable` (via ``_chat``)
    so the consolidation step logs and continues.
    """
    content = _chat(
        [
            {"role": "system", "content": _CONTRADICT_SYSTEM},
            {
                "role": "user",
                "content": f"Item A: {a_title}\n{a_body}\n\nItem B: {b_title}\n{b_body}",
            },
        ],
        json_format=True,
    )
    try:
        data = parse_model_json(content)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    val = data.get("contradict")
    # Some models emit a stringified bool under format=json; bool("false") is
    # True, so coerce strings explicitly instead of trusting bool() on them.
    if isinstance(val, str):
        return val.strip().lower() in ("true", "yes", "1")
    return bool(val)

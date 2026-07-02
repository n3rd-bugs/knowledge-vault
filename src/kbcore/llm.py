"""kbcore.llm — Ollama /api/chat extractor client (PLAN.md §7.2, Invariant 8).

The nightly 14B extractor turns a session transcript into a list of grounded
knowledge items. Every call sets ``keep_alive=0`` so the model unloads
immediately after use — GPU is a guest and the extractor must not sit resident
(Invariant 8 / OPS-1). Network/format failures become an
:class:`ExtractorUnavailable` domain error; raw traces stay in the logs.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from kbcore.config import settings
from kbcore.errors import ExtractorUnavailable

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
        data = json.loads(content)
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

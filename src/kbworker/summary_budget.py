"""Fit a topic's sources into ONE summarizer call before provenance is written.

KB 55031 (found 2026-10-26): ``llm.summarize_items`` sent a tag's WHOLE active corpus
(up to 590 items / 57k tokens) as one message. Past Ollama's prompt ceiling (num_ctx/2,
8,194 tokens at 16384) Ollama keeps the first 4 tokens and the LAST ~8,190, so the model
never saw ``_SUMMARIZE_SYSTEM`` or the older items, while ``create_summary`` still linked
``derived_from`` to every source — the failure the drain fixed for transcripts in July
(:mod:`kbworker.prep`). The fitter applies the drain's ceiling to the summary prompt, so a
summary is written from, and credits, exactly the sources its model saw.
"""

from __future__ import annotations

import logging
from typing import Any

from kbcore import llm
from kbcore.chunk import count_tokens
from kbworker import prep

logger = logging.getLogger("kbworker.consolidate")


def fit_summary_sources(
    topic: str, sources: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Keep newest sources that fit, returning original objects in ascending id order.

    The call-time ceiling shares the drain's tokenizer margin. Measure the exact
    system and user text, including wrappers and separators: counting blocks in
    isolation can miss token merges at their boundaries.
    """
    ceiling = prep._token_ceiling()

    def tokens(items: list[dict[str, Any]]) -> int:
        corpus = "\n\n".join(
            f"### {it.get('title', '')}\n{it.get('body', '')}".strip() for it in items
        )
        user = f"Topic: {topic}\n\nItems:\n{corpus}\n\nWrite the summary."
        return count_tokens(llm._SUMMARIZE_SYSTEM) + count_tokens(user)

    kept: list[dict[str, Any]] = []
    for source in sorted(sources, key=lambda s: s["id"], reverse=True):
        if tokens([source]) > ceiling:
            logger.info(
                "summary source %s exceeds %d-token ceiling; skipped", source["id"], ceiling
            )
            continue
        candidate = [source, *kept]
        if tokens(candidate) <= ceiling:
            kept = candidate
    return kept, len(sources) - len(kept)

"""parse_model_json — reasoning-model ("think") output wrapper for the extractor.

Reasoning models (deepseek-r1, qwen3 with thinking enabled) can prefix the
answer with <think>...</think> or fence the JSON in markdown; the wrapper
recovers the JSON instead of failing the nightly batch. No network: httpx is
monkeypatched for the extract_items integration case.
"""

from __future__ import annotations

import json

import pytest

from kbcore import llm
from kbcore.errors import ExtractionFailed
from kbcore.llm import parse_model_json

_ITEMS = {"items": [{"type": "gotcha", "title": "t", "body": "b", "quote": "q", "tags": []}]}


def test_plain_json_passthrough():
    assert parse_model_json(json.dumps(_ITEMS)) == _ITEMS


def test_think_block_stripped():
    content = "<think>\nlet me reason about the transcript...\n</think>\n" + json.dumps(_ITEMS)
    assert parse_model_json(content) == _ITEMS


def test_think_block_case_and_multiple():
    content = "<THINK>first pass</THINK>ignore me<think>second\npass</think>" + json.dumps(_ITEMS)
    # leading non-JSON prose survives think-stripping; outermost-span fallback recovers
    assert parse_model_json(content) == _ITEMS


def test_markdown_fenced_json():
    content = "Here is the result:\n```json\n" + json.dumps(_ITEMS) + "\n```\nDone."
    assert parse_model_json(content) == _ITEMS


def test_think_plus_fence():
    content = "<think>hmm</think>\n```json\n" + json.dumps(_ITEMS) + "\n```"
    assert parse_model_json(content) == _ITEMS


def test_unterminated_think_rejected():
    # Model ran out of tokens mid-reasoning: any {...} inside is reasoning, not answer.
    with pytest.raises(ValueError):
        parse_model_json('<think>I will output {"items": []} because')


def test_no_json_rejected():
    with pytest.raises(ValueError):
        parse_model_json("<think>done</think>Sorry, I cannot help with that.")


def test_extract_items_with_think_content(monkeypatch):
    """End-to-end through extract_items: a think-wrapped Ollama reply parses."""

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "<think>reasoning</think>" + json.dumps(_ITEMS)}}

    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: _Resp())
    items = llm.extract_items("some transcript text")
    assert items == _ITEMS["items"]


def test_extract_items_unparseable_still_raises(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": "<think>never closed"}}

    monkeypatch.setattr(llm.httpx, "post", lambda *a, **k: _Resp())
    # Content-deterministic (unterminated <think>) → ExtractionFailed, so the queue
    # drain bounds it with the attempts cap instead of deferring forever.
    with pytest.raises(ExtractionFailed):
        llm.extract_items("some transcript text")

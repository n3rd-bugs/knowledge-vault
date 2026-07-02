"""Unit test for llm.items_contradict answer coercion (no DB, extractor stubbed)."""

from __future__ import annotations

import pytest

from kbcore import llm


@pytest.mark.parametrize(
    "payload,expected",
    [
        ('{"contradict": false}', False),
        ('{"contradict": true}', True),
        ('{"contradict": "false"}', False),  # stringified bool: bool("false") is True — must coerce
        ('{"contradict": "true"}', True),
        ("not json", False),
        ('{"other": 1}', False),
        ("[1,2,3]", False),
    ],
)
def test_items_contradict_coerces_answer(monkeypatch, payload, expected):
    monkeypatch.setattr(llm, "_chat", lambda *a, **k: payload)
    assert llm.items_contradict("a", "x", "b", "y") is expected

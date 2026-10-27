"""Unit test for llm.items_contradict answer coercion (no DB, extractor stubbed).

Three-state since KB 55589: a reply with no usable verdict is None, never a silent
False — the nightly memorized blind replies as "no conflict" when it was False.
"""

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
        ('{"contradict": "no"}', False),
        ('{"contradict": 1}', True),
        ('{"contradict": 0}', False),
        ("not json", None),
        ("", None),  # a token-repeat abort reads as an empty reply
        ('{"other": 1}', None),
        ("[1,2,3]", None),
        ('{"contradict": null}', None),
        ('{"contradict": ["false"]}', None),  # the old lenient bool() read this as True
        ('{"contradict": 2}', None),
        ('{"contradict": "maybe"}', None),
    ],
)
def test_items_contradict_coerces_answer(monkeypatch, payload, expected):
    monkeypatch.setattr(llm, "_chat", lambda *a, **k: payload)
    assert llm.items_contradict("a", "x", "b", "y") is expected


def test_a_reply_without_a_verdict_is_asked_once_more_at_the_next_seed(monkeypatch):
    seen: list[int] = []
    replies = iter(["", '{"contradict": false}'])
    monkeypatch.setattr(llm, "_chat", lambda m, **k: seen.append(k["seed"]) or next(replies))
    assert llm.items_contradict("a", "x", "b", "y") is False
    base = llm._draw_seed("a", "x", "b", "y")
    assert seen == [base, base + 1]


def test_a_verdict_on_the_first_draw_is_never_re_asked(monkeypatch):
    seen: list[int] = []
    monkeypatch.setattr(llm, "_chat", lambda m, **k: seen.append(k["seed"]) or '{"contradict": 1}')
    assert llm.items_contradict("a", "x", "b", "y") is True
    assert len(seen) == 1


def test_two_draws_without_a_verdict_are_none_and_never_a_third(monkeypatch):
    seen: list[int] = []
    monkeypatch.setattr(llm, "_chat", lambda m, **k: seen.append(k["seed"]) or "no json")
    assert llm.items_contradict("a", "x", "b", "y") is None
    assert len(seen) == 2

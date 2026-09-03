"""Review #8 H3 — the inert ``think`` passthrough and the pair-judge seat knobs.

``llm._chat`` emits Ollama's ``think`` key ONLY when a caller passes a real bool, so
every existing caller's request is byte-identical to before; KB_PAIR_JUDGE_MODEL /
KB_PAIR_JUDGE_THINK are read by EXACTLY two consumers — ``llm.items_contradict`` and
``twin_judge.judge_same_fact`` — through ``llm._pair_judge_kwargs()``, and an EMPTY
model restores the incumbent model AND its default thinking in one line. No network:
httpx is monkeypatched to capture the payload (test_llm_num_ctx style); no DB:
``twin_judge.item_pair_texts`` is stubbed.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from kbcore import llm, twin_judge
from kbcore.config import settings
from kbcore.config_llm import LLMSettings

# One answer every parser in llm/twin_judge accepts, so any caller can be driven.
_UNIVERSAL_ANSWER = json.dumps(
    {
        "items": [],
        "supported": True,
        "reason": "r",
        "contradict": False,
        "side": "neither",
        "same_fact": False,
        "action": "keep",
        "dup_of": None,
        "rationale": "r",
    }
)


def _capture_post(store: dict, content: str = _UNIVERSAL_ANSWER):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": content}}

    def post(url, *, json=None, timeout=None):
        store["payload"] = json
        store.setdefault("payloads", []).append(json)
        return _Resp()

    return post


@pytest.fixture
def capture(monkeypatch) -> dict:
    store: dict = {}
    monkeypatch.setattr(llm.httpx, "post", _capture_post(store))
    return store


@pytest.fixture
def incumbent(monkeypatch):
    """The knob EMPTY — how H3 lands — whatever this box's .env says."""
    monkeypatch.setattr(settings, "kb_pair_judge_model", "")
    monkeypatch.setattr(settings, "kb_pair_judge_think", None)


@pytest.fixture
def candidate(monkeypatch):
    """The swap armed: granite4.2:8b with thinking OFF (the H3d .env lines)."""
    monkeypatch.setattr(settings, "kb_pair_judge_model", "granite4.2:8b")
    monkeypatch.setattr(settings, "kb_pair_judge_think", False)


@pytest.fixture
def pair_texts(monkeypatch):
    monkeypatch.setattr(
        twin_judge,
        "item_pair_texts",
        lambda a, b: (("A title", "A body"), ("B title", "B body")),
    )


# Every /api/chat caller in the tree, by name, so the "absent for every existing
# caller" pin is enumerated rather than assumed.
_CALLERS = {
    "extract_items": lambda: llm.extract_items("some transcript"),
    "chat": lambda: llm.chat([{"role": "user", "content": "hi"}]),
    "_chat": lambda: llm._chat([{"role": "user", "content": "hi"}], json_format=True, seed=3),
    "summarize_items": lambda: llm.summarize_items("t", [{"title": "a", "body": "b"}]),
    "evidence_side": lambda: llm.evidence_side("a", "x", "b", "y", "c", "z"),
    "items_contradict": lambda: llm.items_contradict("a", "x", "b", "y"),
    "verify_item": lambda: llm.verify_item("t", "b", "transcript"),
    "triage_extraction": lambda: llm.triage_extraction(
        {"title": "t", "body": "b", "tags": []}, [], 0
    ),
    "judge_same_fact": lambda: twin_judge.judge_same_fact(1, "p1", 2, "p2"),
}
_PAIR_JUDGES = {"items_contradict", "judge_same_fact"}


@pytest.mark.parametrize("caller", sorted(_CALLERS))
def test_think_key_is_absent_for_every_existing_caller(caller, capture, incumbent, pair_texts):
    _CALLERS[caller]()
    assert "think" not in capture["payload"], f"{caller} must not emit think when unset"
    assert capture["payload"]["model"] == settings.kb_extract_model


def test_think_is_emitted_only_when_a_real_bool_is_passed(capture):
    msgs = [{"role": "user", "content": "hi"}]
    llm._chat(msgs, json_format=False, think=False)
    assert capture["payload"]["think"] is False
    llm._chat(msgs, json_format=False, think=True)
    assert capture["payload"]["think"] is True
    llm._chat(msgs, json_format=False, think=None)
    assert "think" not in capture["payload"]
    # The public chat() and verify_item() forward it unchanged.
    llm.chat(msgs, think=False)
    assert capture["payload"]["think"] is False
    llm.verify_item("t", "b", "transcript", think=True)
    assert capture["payload"]["think"] is True
    llm.verify_item("t", "b", "transcript")
    assert "think" not in capture["payload"]


def test_chat_forwards_seed_and_leaves_sampling_unseeded_by_default(capture):
    msgs = [{"role": "user", "content": "hi"}]
    llm.chat(msgs, seed=11)
    assert capture["payload"]["options"]["seed"] == 11
    llm.chat(msgs)
    assert "seed" not in capture["payload"]["options"]
    assert capture["payload"]["options"]["num_ctx"] == settings.kb_llm_num_ctx


def test_items_contradict_request_is_byte_identical_with_the_knob_empty(capture, incumbent):
    """The pre-H3 payload, key for key and in the same insertion order."""
    llm.items_contradict("a", "x", "b", "y")
    expected = {
        "model": settings.kb_extract_model,
        "keep_alive": settings.kb_llm_keep_alive,
        "stream": False,
        "messages": [
            {"role": "system", "content": llm._CONTRADICT_SYSTEM},
            {"role": "user", "content": "Item A: a\nx\n\nItem B: b\ny"},
        ],
        "format": "json",
        "options": {"num_ctx": settings.kb_llm_num_ctx, "seed": llm._draw_seed("a", "x", "b", "y")},
    }
    assert json.dumps(capture["payload"]) == json.dumps(expected)


@pytest.mark.parametrize("caller", sorted(_CALLERS))
def test_pair_judge_knob_routes_exactly_the_two_pair_judges(caller, capture, candidate, pair_texts):
    _CALLERS[caller]()
    payload = capture["payload"]
    if caller in _PAIR_JUDGES:
        assert payload["model"] == "granite4.2:8b"
        assert payload["think"] is False
    else:
        # evidence_side, verify_item, triage, deep search, the extractor and the
        # summariser stay on their incumbents with no think key at all.
        assert payload["model"] == settings.kb_extract_model
        assert "think" not in payload


def test_an_empty_model_restores_the_incumbent_model_and_its_thinking(
    capture, monkeypatch, pair_texts
):
    """`KB_PAIR_JUDGE_MODEL=` alone is the rollback: think is read ONLY while a model is set."""
    monkeypatch.setattr(settings, "kb_pair_judge_model", "")
    monkeypatch.setattr(settings, "kb_pair_judge_think", False)  # a stale think line left behind
    assert settings.pair_judge_model == settings.kb_extract_model
    assert settings.pair_judge_think is None
    assert llm._pair_judge_kwargs() == {"model": settings.kb_extract_model, "think": None}
    llm.items_contradict("a", "x", "b", "y")
    assert capture["payload"]["model"] == settings.kb_extract_model
    assert "think" not in capture["payload"]
    twin_judge.judge_same_fact(1, "p1", 2, "p2")
    assert capture["payload"]["model"] == settings.kb_extract_model
    assert "think" not in capture["payload"]


def test_same_fact_judge_passes_the_per_content_seed(capture, incumbent, pair_texts):
    """H3c: the same pair reproduces the same draw every night — no one-way ratchet."""
    same, judged, _ = twin_judge.judge_same_fact(1, "p1", 2, "p2")
    opts = capture["payload"]["options"]
    assert opts["seed"] == llm._draw_seed("A title", "A body", "B title", "B body")
    assert opts["num_ctx"] == settings.kb_llm_num_ctx
    assert capture["payload"]["format"] == "json"
    assert (same, judged) == (False, True)
    # Different pair texts ⇒ a different seed (no shared-RNG bias).
    assert opts["seed"] != llm._draw_seed("A title", "A body", "B title", "other")


def test_same_fact_request_shape_is_unchanged_by_the_plumb(capture, incumbent, pair_texts):
    twin_judge.judge_same_fact(1, "unroll", 2, "muhammadusama")
    system, user = (m["content"] for m in capture["payload"]["messages"])
    assert system == twin_judge.SAME_FACT_SYSTEM
    assert "BEGIN ITEM_A" in user and "(project unroll)" in user
    assert "BEGIN ITEM_B" in user and "(project muhammadusama)" in user
    assert user.endswith("Do ITEM_A and ITEM_B record the same underlying fact?")


@pytest.mark.parametrize(
    "raw,expected",
    [("false", False), ("true", True), ("0", False), ("1", True), (None, None)],
)
def test_think_env_values_parse_as_bool_or_none(monkeypatch, raw, expected):
    monkeypatch.delenv("KB_PAIR_JUDGE_THINK", raising=False)
    if raw is not None:
        monkeypatch.setenv("KB_PAIR_JUDGE_THINK", raw)
    assert LLMSettings(_env_file=None).kb_pair_judge_think is expected


def test_a_blank_think_env_value_is_a_validation_error_not_a_silent_default(monkeypatch):
    """Why .env.example says true|false or commented — never `KB_PAIR_JUDGE_THINK=`."""
    monkeypatch.setenv("KB_PAIR_JUDGE_THINK", "")
    with pytest.raises(ValidationError):
        LLMSettings(_env_file=None)

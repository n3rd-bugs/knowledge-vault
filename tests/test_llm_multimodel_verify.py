"""Pipeline v2 WP-B1 llm plumbing — multi-model extract_items + factored verify_item.

No network: httpx is monkeypatched to capture the outgoing payload. extract_items
grows additive model/keep_alive overrides (zero behavior change when omitted);
verify_item is the factored judge — it sees ONLY transcript + title/body, parses
strict JSON, and NEVER coerces a non-bool verdict.
"""

from __future__ import annotations

import json

import pytest

from kbcore import llm
from kbcore.config import settings
from kbcore.errors import ExtractionFailed


def _capture_post(store: dict, content: str):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": content}}

    def post(url, *, json=None, timeout=None):
        store["payload"] = json
        return _Resp()

    return post


# --- extract_items: additive model/keep_alive params (WP-B2 ensemble plumbing) ---


def test_extract_items_defaults_are_settings_driven(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(llm.httpx, "post", _capture_post(store, json.dumps({"items": []})))
    llm.extract_items("some transcript text")
    payload = store["payload"]
    assert payload["model"] == settings.kb_extract_model
    assert payload["keep_alive"] == settings.kb_llm_keep_alive
    assert payload["options"]["num_ctx"] == settings.kb_llm_num_ctx
    assert payload["options"]["seed"] == llm.EXTRACT_SEED


def test_extract_items_overrides_land_in_payload(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(llm.httpx, "post", _capture_post(store, json.dumps({"items": []})))
    # keep_alive=0 is falsy on purpose: ensemble extras unload immediately so
    # only the primary stays resident (Invariant 8) — 0 must not fall back.
    llm.extract_items("some transcript text", model="granite4.1:8b", keep_alive=0)
    payload = store["payload"]
    assert payload["model"] == "granite4.1:8b"
    assert payload["keep_alive"] == 0
    assert payload["options"]["num_ctx"] == settings.kb_llm_num_ctx, "num_ctx still rides"


# --- verify_item: factored support verdicts ----------------------------------


def test_verify_item_parses_supported_true(monkeypatch):
    store: dict = {}
    content = '{"supported": true, "reason": "stated in the transcript"}'
    monkeypatch.setattr(llm.httpx, "post", _capture_post(store, content))
    out = llm.verify_item("title", "body", "transcript")
    assert out == {"supported": True, "reason": "stated in the transcript"}


def test_verify_item_parses_supported_false(monkeypatch):
    store: dict = {}
    content = '{"supported": false, "reason": "no evidence for the claim"}'
    monkeypatch.setattr(llm.httpx, "post", _capture_post(store, content))
    out = llm.verify_item("title", "body", "transcript")
    assert out == {"supported": False, "reason": "no evidence for the claim"}


@pytest.mark.parametrize(
    "content",
    [
        '{"reason": "verdict key missing"}',  # missing supported
        '{"supported": "true", "reason": "stringly typed"}',  # non-bool: never coerce
        '{"supported": 1, "reason": "int is not bool"}',
        "the transcript clearly supports it",  # not JSON at all
    ],
)
def test_verify_item_malformed_output_raises(monkeypatch, content):
    monkeypatch.setattr(llm.httpx, "post", _capture_post({}, content))
    with pytest.raises(ExtractionFailed):
        llm.verify_item("title", "body", "transcript")


def test_verify_item_factored_contract(monkeypatch):
    store: dict = {}
    content = '{"supported": false, "reason": "not present"}'
    monkeypatch.setattr(llm.httpx, "post", _capture_post(store, content))

    title = "SPI mode decision"
    body = "Use SPI mode 1 because mode 0 reads all zeros."
    transcript = "We chose SPI mode 1 after mode 0 read all zeros on the DRV8706."
    # A near-miss grounding span (NOT a transcript substring): the judge must
    # never see it — factored setups measured -77% hallucinated entities.
    item_quote = "mode 0 read all zeroes on DRV8706"
    assert item_quote not in transcript

    out = llm.verify_item(title, body, transcript, judge_model="qwen3:14b", keep_alive=0)
    assert out == {"supported": False, "reason": "not present"}

    payload = store["payload"]
    assert payload["model"] == "qwen3:14b"
    assert payload["keep_alive"] == 0
    assert payload["format"] == "json", "strict JSON via json_format=True"
    assert payload["options"]["num_ctx"] == settings.kb_llm_num_ctx, "num_ctx rides automatically"
    assert payload["options"]["seed"] == llm._draw_seed(title, body)

    joined = "\n".join(m["content"] for m in payload["messages"])
    assert transcript in joined
    assert title in joined
    assert body in joined
    # Factored: no grounding-span field, and the prompt never even mentions one.
    assert item_quote not in joined
    assert "quote" not in joined.lower()


def test_verify_item_defaults_are_settings_driven(monkeypatch):
    store: dict = {}
    content = '{"supported": true, "reason": "ok"}'
    monkeypatch.setattr(llm.httpx, "post", _capture_post(store, content))
    llm.verify_item("t", "b", "tr")
    payload = store["payload"]
    assert payload["model"] == settings.kb_extract_model
    assert payload["keep_alive"] == settings.kb_llm_keep_alive

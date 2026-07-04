"""Deep search (kbcore.deepsearch) + the llm chat plumbing it relies on.

The LLM is always mocked (plan/synthesize monkeypatched, or httpx faked);
retrieval runs against the real kb_test DB with conftest's fake embeddings —
the keyword leg carries relevance deterministically.
"""

from __future__ import annotations

from kbcore import llm
from kbcore.config import settings


class _FakeResp:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"message": {"content": self._content}}


def test_chat_keep_alive_and_model_passthrough(monkeypatch):
    captured: list[dict] = []

    def fake_post(url, json=None, timeout=None):
        captured.append(json)
        return _FakeResp('{"ok": true}')

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    # defaults: extract model, keep_alive 0 (Invariant 8)
    llm.chat([{"role": "user", "content": "x"}], json_format=True)
    assert captured[-1]["model"] == settings.kb_extract_model
    assert captured[-1]["keep_alive"] == 0
    assert captured[-1]["format"] == "json"
    # overrides propagate
    llm.chat([{"role": "user", "content": "x"}], model="other:1b", keep_alive=120)
    assert captured[-1]["model"] == "other:1b"
    assert captured[-1]["keep_alive"] == 120
    assert "format" not in captured[-1]


def test_deep_model_falls_back_to_extract_model(monkeypatch):
    monkeypatch.setattr(settings, "kb_deep_model", "")
    assert settings.deep_model == settings.kb_extract_model
    monkeypatch.setattr(settings, "kb_deep_model", "big:70b")
    assert settings.deep_model == "big:70b"

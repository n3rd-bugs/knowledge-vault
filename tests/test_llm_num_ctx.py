"""Every /api/chat call must carry an explicit options.num_ctx (bench 2026-07-18).

Without it Ollama runs its RUNTIME default context (4K), silently truncating
transcript input and cutting JSON output mid-object (done_reason=length): the
incumbent extractor's verbatim grounding measured 0% on >4K-token transcripts
until num_ctx was passed. No network — httpx is monkeypatched to capture the
payload.
"""

from __future__ import annotations

import json

from kbcore import llm
from kbcore.config import settings


def _capture_post(store: dict):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": json.dumps({"items": []})}}

    def post(url, *, json=None, timeout=None):
        store["payload"] = json
        return _Resp()

    return post


def test_extract_items_sends_configured_num_ctx(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(llm.httpx, "post", _capture_post(store))
    llm.extract_items("some transcript text")
    assert store["payload"]["options"]["num_ctx"] == settings.kb_llm_num_ctx


def test_chat_sends_configured_num_ctx_with_and_without_seed(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(llm.httpx, "post", _capture_post(store))
    llm.chat([{"role": "user", "content": "hi"}])
    assert store["payload"]["options"]["num_ctx"] == settings.kb_llm_num_ctx

    llm._chat([{"role": "user", "content": "hi"}], json_format=True, seed=7)
    opts = store["payload"]["options"]
    assert opts["num_ctx"] == settings.kb_llm_num_ctx
    assert opts["seed"] == 7

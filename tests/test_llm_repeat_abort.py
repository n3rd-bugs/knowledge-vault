"""Ollama >= 0.40's token-repeat abort (HTTP 500) is a CONTENT failure, never an outage.

0.31.1 cut a degenerate reply (a whitespace run inside format=json) at 31 identical
tokens and answered 200 with the truncated text, so every caller's unparseable-reply
path handled it: the extractor's ONE re-ask at seed+1, a judge's per-item failure, the
contradiction/evidence judges' "unparseable → False / neither". 0.40 keeps going to 101
and answers HTTP 500 "prediction aborted, token repeat limit reached" — which the old
classifier filed as an OUTAGE (5xx → ExtractorUnavailable): no re-ask, and a judge
group abandoned and DEFERRED whole, night after night for a seeded, deterministic
loop. These pin the abort to the empty-reply behaviour 0.31.1 effectively had. No
network: httpx.post is monkeypatched with scripted responses.
"""

from __future__ import annotations

import json

import httpx
import pytest

from kbcore import llm
from kbcore.config import settings
from kbcore.errors import ExtractionFailed, ExtractorUnavailable

_ABORT = '{"error":"prediction aborted, token repeat limit reached"}'
_ITEMS = json.dumps({"items": [{"type": "note", "title": "t", "body": "b", "quote": "q"}]})


class _Resp:
    def __init__(self, status: int, text: str):
        self.status_code = status
        self.text = text
        self._req = httpx.Request("POST", f"{settings.ollama_url}/api/chat")

    def raise_for_status(self):
        if self.status_code >= 400:
            resp = httpx.Response(self.status_code, text=self.text, request=self._req)
            raise httpx.HTTPStatusError("boom", request=self._req, response=resp)

    def json(self):
        return json.loads(self.text)


def _ok(content: str) -> _Resp:
    return _Resp(200, json.dumps({"message": {"content": content}}))


@pytest.fixture
def script(monkeypatch):
    """Queue responses; each httpx.post pops the next one and records the payload."""
    state: dict = {"queue": [], "payloads": []}

    def post(url, *, json=None, timeout=None):
        state["payloads"].append(json)
        return state["queue"].pop(0)

    monkeypatch.setattr(llm.httpx, "post", post)
    monkeypatch.setattr(settings, "kb_extract_json_retry", 1)
    return state


def test_extractor_abort_takes_the_one_reask_at_seed_plus_one(script):
    script["queue"] = [_Resp(500, _ABORT), _ok(_ITEMS)]
    items = llm.extract_items("some transcript")
    assert [it["title"] for it in items] == ["t"]
    first, second = script["payloads"]
    assert second["options"]["seed"] == first["options"]["seed"] + 1


def test_extractor_abort_twice_is_attempts_bounded_never_an_outage(script):
    script["queue"] = [_Resp(500, _ABORT), _Resp(500, _ABORT)]
    with pytest.raises(ExtractionFailed):
        llm.extract_items("some transcript")


def test_a_plain_500_is_still_an_outage(script):
    script["queue"] = [_Resp(500, '{"error":"llama runner process has terminated"}')]
    with pytest.raises(ExtractorUnavailable):
        llm.extract_items("some transcript")


def test_judge_abort_is_a_per_item_failure_not_a_dead_judge(script):
    # judging.judge_cohort abandons the whole GROUP (and defers it) on
    # ExtractorUnavailable; ExtractionFailed stays strictly per item.
    script["queue"] = [_Resp(500, _ABORT)]
    with pytest.raises(ExtractionFailed):
        llm.verify_item("title", "body", "transcript")


def test_contradiction_and_evidence_judges_degrade_like_an_unparseable_reply(script):
    script["queue"] = [_Resp(500, _ABORT), _Resp(500, _ABORT)]
    assert llm.items_contradict("a", "a body", "b", "b body") is False
    assert llm.evidence_side("a", "ab", "b", "bb", "c", "cb") == "neither"


def test_plain_500_on_chat_is_still_an_outage(script):
    script["queue"] = [_Resp(503, '{"error":"server busy"}')]
    with pytest.raises(ExtractorUnavailable):
        llm.items_contradict("a", "a body", "b", "b body")

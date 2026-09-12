"""extract_items recovers from a malformed-JSON reply instead of failing the row.

Incident 2026-09-12: the primary returned malformed JSON for ONE window; with the
fixed EXTRACT_SEED every nightly retry would reproduce the same bytes, so 151
rows burned their attempts on a deterministic parse error. The chain now is:
strict parse → ONE re-ask (seed+1, repair hint) → salvage of the re-ask →
salvage of the first reply → ExtractionFailed carrying the context.

No network: httpx.post is monkeypatched exactly the way test_llm_think_wrapper
does it, scripted to return one reply per call so the CALL COUNT is pinned.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from kbcore import llm
from kbcore.config import settings
from kbcore.errors import ExtractionFailed


def _item(n: int) -> dict[str, object]:
    return {"type": "gotcha", "title": f"t{n}", "body": f"b{n}", "quote": f"q{n}", "tags": []}


GOOD = json.dumps({"items": [_item(1), _item(2), _item(3)]})
# Item 2 carries an unescaped inner quote (the incident's shape) — salvageable.
SALVAGEABLE = GOOD.replace('"b2"', '"we said "hi""', 1)
GARBAGE = "<think>never closed"


def _script(store: dict, replies: list):
    """httpx.post stub: returns replies[i] on the i-th call, records payloads. A
    reply that is an exception is RAISED (transport failure); a non-str reply is
    handed back verbatim as message.content (a null/number/list from the model)."""
    store["payloads"] = []

    class _Resp:
        def __init__(self, content: str) -> None:
            self._content = content

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"message": {"content": self._content}}

    def post(url, *, json=None, timeout=None):
        store["payloads"].append(json)
        assert len(store["payloads"]) <= len(replies), "more chat calls than scripted"
        reply = replies[len(store["payloads"]) - 1]
        if isinstance(reply, BaseException):
            raise reply
        return _Resp(reply)

    return post


@pytest.fixture(autouse=True)
def _counters(monkeypatch):
    monkeypatch.setattr(settings, "kb_extract_json_retry", 1)
    monkeypatch.setattr(settings, "kb_extract_failure_dumps", 20)
    for k in llm.JSON_COUNTERS:
        llm.JSON_COUNTERS[k] = 0
    yield
    for k in llm.JSON_COUNTERS:
        llm.JSON_COUNTERS[k] = 0


def _dumps() -> list[Path]:
    return sorted((Path(settings.kb_state_dir).expanduser() / "extract-failures").glob("*.txt"))


def test_malformed_first_reply_is_re_asked_once_with_seed_plus_one(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(llm.httpx, "post", _script(store, [SALVAGEABLE, GOOD]))
    items = llm.extract_items("some transcript text", model="qwen3:14b", keep_alive=600)
    assert items == [_item(1), _item(2), _item(3)]
    assert len(store["payloads"]) == 2, "exactly one re-ask"
    first, retry = store["payloads"]
    assert first["options"] == {"seed": llm.EXTRACT_SEED, "num_ctx": settings.kb_llm_num_ctx}
    assert retry["options"] == {"seed": llm.EXTRACT_SEED + 1, "num_ctx": settings.kb_llm_num_ctx}
    assert retry["options"]["seed"] == 43
    assert retry["model"] == first["model"] == "qwen3:14b"
    assert retry["keep_alive"] == first["keep_alive"] == 600
    assert retry["format"] == "json"
    sys_prompt = retry["messages"][0]["content"]
    assert sys_prompt.startswith(llm.SYSTEM_PROMPT)
    assert "Your previous answer was not valid JSON (JSONDecodeError:" in sys_prompt
    assert 'escaped as \\"' in sys_prompt
    assert retry["messages"][1] == first["messages"][1], "same transcript"
    assert first["messages"][0]["content"] == llm.SYSTEM_PROMPT, "first ask is unchanged"
    assert llm.JSON_COUNTERS == {"retry_ok": 1, "salvaged": 0, "failed": 0}
    assert len(_dumps()) == 1, "the malformed reply was dumped for post-mortem"


def test_malformed_twice_but_salvageable_yields_the_salvaged_items(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(llm.httpx, "post", _script(store, [SALVAGEABLE, SALVAGEABLE]))
    items = llm.extract_items("some transcript text")
    assert [it["title"] for it in items] == ["t1", "t3"], "item 2 dropped, 1 and 3 kept"
    assert len(store["payloads"]) == 2
    assert llm.JSON_COUNTERS == {"retry_ok": 0, "salvaged": 1, "failed": 0}
    assert len(_dumps()) == 2, "both malformed replies dumped"


def test_garbage_re_ask_falls_back_to_salvaging_the_first_reply(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(llm.httpx, "post", _script(store, [SALVAGEABLE, GARBAGE]))
    items = llm.extract_items("some transcript text")
    assert [it["title"] for it in items] == ["t1", "t3"]
    assert llm.JSON_COUNTERS["salvaged"] == 1


TOKEN = "ghp_" + "0" * 36


def test_malformed_twice_unsalvageable_raises_with_context_after_two_calls(monkeypatch, caplog):
    store: dict = {}
    # Cut inside the string; the broken region echoes a credential (a quote may).
    broken = '{"items": [{"title": "t", "body": "token ' + TOKEN + " unterminated}"
    monkeypatch.setattr(llm.httpx, "post", _script(store, [broken, GARBAGE]))
    with (
        caplog.at_level(logging.WARNING, logger="kbcore.llm_json"),
        pytest.raises(ExtractionFailed) as ei,
    ):
        llm.extract_items("some transcript text")
    msg = str(ei.value)
    assert msg.startswith("extractor returned unparseable output: JSONDecodeError:")
    assert "unterminated" in msg, "the context window rides the error"
    assert TOKEN not in msg, "the window is scrubbed before it reaches ingest_queue.error"
    assert "unparseable output" in caplog.text and TOKEN not in caplog.text, "…and journald"
    assert len(store["payloads"]) == 2, "still only the one re-ask"
    assert llm.JSON_COUNTERS == {"retry_ok": 0, "salvaged": 0, "failed": 1}


def test_non_string_message_content_is_a_content_failure_not_a_type_error(monkeypatch):
    """Regression guard: the old code mapped a null/number/list message.content to
    ExtractionFailed; a raw TypeError would abort the whole cohort prefetch."""
    for bad in (None, 123, ["x"]):
        store: dict = {}
        monkeypatch.setattr(llm.httpx, "post", _script(store, [bad]))
        with pytest.raises(ExtractionFailed) as ei:
            llm.extract_items("some transcript text")
        assert "TypeError: message.content is" in str(ei.value)
        assert len(store["payloads"]) == 1, "no re-ask for a non-string reply"
    assert llm.JSON_COUNTERS == {"retry_ok": 0, "salvaged": 0, "failed": 0}
    assert _dumps() == []


def test_clean_but_empty_re_ask_is_inconclusive_and_salvages_the_first_reply(monkeypatch):
    """A model confused by the repair hint answers {"items": []}: the first reply
    still holds two grounded items and must win over the empty re-ask."""
    store: dict = {}
    monkeypatch.setattr(llm.httpx, "post", _script(store, [SALVAGEABLE, '{"items": []}']))
    items = llm.extract_items("some transcript text")
    assert [it["title"] for it in items] == ["t1", "t3"]
    assert len(store["payloads"]) == 2
    assert llm.JSON_COUNTERS == {"retry_ok": 0, "salvaged": 1, "failed": 0}


def test_clean_but_empty_re_ask_wins_only_when_the_first_reply_is_unsalvageable(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(llm.httpx, "post", _script(store, [GARBAGE, '{"items": null}']))
    assert llm.extract_items("some transcript text") == []
    assert llm.JSON_COUNTERS == {"retry_ok": 1, "salvaged": 0, "failed": 0}


def test_read_timeout_on_the_re_ask_still_salvages_the_first_reply(monkeypatch):
    """A content-class failure of the re-ask itself (ReadTimeout on the doubled
    budget) must not throw away a first reply that salvage can rescue."""
    store: dict = {}
    timeout = httpx.ReadTimeout("read timed out")
    monkeypatch.setattr(llm.httpx, "post", _script(store, [SALVAGEABLE, timeout]))
    items = llm.extract_items("some transcript text")
    assert [it["title"] for it in items] == ["t1", "t3"]
    assert len(store["payloads"]) == 2
    assert llm.JSON_COUNTERS == {"retry_ok": 0, "salvaged": 1, "failed": 0}


def test_connect_error_on_the_re_ask_propagates_as_unavailable(monkeypatch):
    """An OUTAGE on the re-ask must defer the row (ExtractorUnavailable), not be
    swallowed into a salvage that burns the attempt."""
    from kbcore.errors import ExtractorUnavailable

    store: dict = {}
    monkeypatch.setattr(
        llm.httpx, "post", _script(store, [SALVAGEABLE, httpx.ConnectError("refused")])
    )
    with pytest.raises(ExtractorUnavailable):
        llm.extract_items("some transcript text")
    assert llm.JSON_COUNTERS == {"retry_ok": 0, "salvaged": 0, "failed": 0}


def test_knob_zero_disables_the_re_ask_but_salvage_still_runs(monkeypatch):
    monkeypatch.setattr(settings, "kb_extract_json_retry", 0)
    store: dict = {}
    monkeypatch.setattr(llm.httpx, "post", _script(store, [SALVAGEABLE]))
    items = llm.extract_items("some transcript text")
    assert [it["title"] for it in items] == ["t1", "t3"]
    assert len(store["payloads"]) == 1, "no re-ask"
    assert llm.JSON_COUNTERS == {"retry_ok": 0, "salvaged": 1, "failed": 0}


def test_knob_zero_and_unsalvageable_fails_after_one_call(monkeypatch):
    monkeypatch.setattr(settings, "kb_extract_json_retry", 0)
    store: dict = {}
    monkeypatch.setattr(llm.httpx, "post", _script(store, [GARBAGE]))
    with pytest.raises(ExtractionFailed):
        llm.extract_items("some transcript text")
    assert len(store["payloads"]) == 1
    assert llm.JSON_COUNTERS["failed"] == 1


def test_clean_first_reply_is_one_call_and_no_dump(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(llm.httpx, "post", _script(store, [GOOD]))
    assert llm.extract_items("some transcript text") == [_item(1), _item(2), _item(3)]
    assert len(store["payloads"]) == 1
    assert llm.JSON_COUNTERS == {"retry_ok": 0, "salvaged": 0, "failed": 0}
    assert _dumps() == []


def test_drain_stats_seed_and_copy_the_json_counters(monkeypatch):
    """The drain report surfaces the counters (drainstats hook). Hermetic: the
    deferral-pressure probe is stubbed so no DB round-trip hides in a counter test."""
    from kbworker import drainstats

    monkeypatch.setattr(drainstats.ingest_queue, "near_deferral_cap_groups", lambda: 0)
    drainstats.reset_cohort_stats(4)
    assert drainstats._COHORT_STATS["extract_json_retry_ok"] == 0
    llm.JSON_COUNTERS["retry_ok"] += 2
    llm.JSON_COUNTERS["salvaged"] += 1
    drainstats.record_deferral_pressure()
    assert drainstats._COHORT_STATS["extract_json_retry_ok"] == 2
    assert drainstats._COHORT_STATS["extract_json_salvaged"] == 1
    assert drainstats._COHORT_STATS["extract_json_failed"] == 0
    drainstats.reset_cohort_stats(4)
    assert llm.JSON_COUNTERS == {"retry_ok": 0, "salvaged": 0, "failed": 0}

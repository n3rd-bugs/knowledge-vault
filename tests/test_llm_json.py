"""kbcore.llm_json — parse seam, item salvage, error context and failure dumps.

Incident 2026-09-12: ONE malformed ~900 KB extractor reply (an unescaped quote
inside a string) failed 151 rows because the whole ``"items"`` array was thrown
away on the first bad element. These tests pin the layers that stop that:
salvage keeps every element that parses on its own, the log line shows WHERE the
JSON broke, and the raw reply is dumped (rotated) for post-mortem.

Pure Python + tmp filesystem — no DB, no network, no Ollama.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kbcore import llm, llm_json
from kbcore.config import settings
from kbcore.llm_json import (
    describe_json_error,
    parse_model_json,
    record_unparseable,
    salvage_items,
)


def _item(n: int, **over: object) -> dict[str, object]:
    base = {
        "type": "gotcha",
        "title": f"title {n}",
        "body": f"body {n}",
        "quote": f"quote {n}",
        "tags": ["lang:python"],
    }
    base.update(over)
    return base


_THREE = {"items": [_item(1), _item(2), _item(3)]}


# --- the seam ------------------------------------------------------------------


def test_parse_model_json_is_the_same_object_through_kbcore_llm():
    """Every caller keeps resolving ``llm.parse_model_json`` (documented seam)."""
    assert llm.parse_model_json is parse_model_json
    assert llm.JSON_COUNTERS is llm_json.JSON_COUNTERS


def test_parse_valid_think_wrapped_and_fenced():
    raw = json.dumps(_THREE)
    assert parse_model_json(raw) == _THREE
    assert parse_model_json("<think>hmm\nmore</think>\n" + raw) == _THREE
    assert parse_model_json("Result:\n```json\n" + raw + "\n```\n") == _THREE


def test_parse_stays_strict_on_a_broken_item():
    """The strict seam does NOT salvage — that is recover_items' last resort, so
    the re-ask (a different draw) gets its chance first."""
    with pytest.raises(ValueError):
        parse_model_json(_broken_middle())


# --- salvage ---------------------------------------------------------------------


def _broken_middle() -> str:
    """Item 2 of 3 carries an unescaped inner quote (the incident's shape)."""
    good = json.dumps(_THREE, indent=2)
    return good.replace('"body 2"', '"we said "hi" here"', 1)


def test_salvage_drops_the_item_with_the_unescaped_quote_and_keeps_the_rest():
    text = _broken_middle()
    assert '"we said "hi" here"' in text, "sanity: the fixture is malformed"
    out = salvage_items(text)
    assert out is not None
    assert [it["title"] for it in out["items"]] == ["title 1", "title 3"]
    assert out["items"][0] == _item(1) and out["items"][1] == _item(3)


def test_salvage_keeps_the_complete_items_before_a_truncation():
    """Output cut mid-way through item 3 (done_reason=length): items 1-2 survive."""
    full = json.dumps(_THREE)
    cut = full[: full.index('"title 3"') + 6]  # inside item 3's title string
    out = salvage_items(cut)
    assert out is not None
    assert out["items"] == [_item(1), _item(2)]


def test_salvage_skips_a_stray_token_between_items():
    text = '{"items": [' + json.dumps(_item(1)) + ", oops, " + json.dumps(_item(2)) + "]}"
    out = salvage_items(text)
    assert out is not None
    assert out["items"] == [_item(1), _item(2)]


def test_salvage_ignores_non_item_dicts_fished_out_of_a_broken_body():
    """A `{"b": 2}` inside a broken body string is not an item."""
    bad = _item(1, body='code was {"a": 1}, {"b": 2} and it "broke')  # unescaped quote
    text = (
        '{"items": ['
        + json.dumps(bad).replace('\\"broke', '"broke')
        + ", "
        + json.dumps(_item(2))
        + "]}"
    )
    out = salvage_items(text)
    assert out is not None
    assert out["items"] == [_item(2)]


def test_salvage_survives_a_json_array_quoted_inside_a_broken_body():
    """Review finding: a body that quotes a JSON ARRAY of objects (the incident's most
    likely origin of the unescaped quote) puts a `]` inside the garbage; the walk
    must not mistake it for the end of the items array and lose every later item."""
    bad = json.dumps(_item(1, body='saw [{"a": 1}, {"b": 2}] and it "broke'))
    text = (
        '{"items": ['
        + bad.replace('\\"broke', '"broke')
        + ", "
        + json.dumps(_item(2))
        + ", "
        + json.dumps(_item(3))
        + "]}"
    )
    assert '"broke' in text and '\\"broke' not in text, "sanity: the fixture is malformed"
    out = salvage_items(text)
    assert out is not None
    assert [it["title"] for it in out["items"]] == ["title 2", "title 3"]


def test_salvage_survives_a_bracket_then_unescaped_quote_in_a_broken_body():
    bad = json.dumps(_item(1, body='tags: [a, {"k": 1}] and "broke'))
    text = '{"items": [' + bad.replace('\\"broke', '"broke') + ", " + json.dumps(_item(2)) + "]}"
    out = salvage_items(text)
    assert out is not None
    assert [it["title"] for it in out["items"]] == ["title 2"]


def test_salvage_still_stops_at_the_real_end_of_the_items_array():
    """Trailing prose after the object is not mined for more items."""
    text = json.dumps(_THREE) + '\n\nAlso: {"title": "ghost", "body": "not in the array"}'
    out = salvage_items(text)
    assert out is not None
    assert [it["title"] for it in out["items"]] == ["title 1", "title 2", "title 3"]


def test_salvage_returns_none_on_garbage_and_on_an_unterminated_think():
    assert salvage_items("Sorry, I cannot help with that.") is None
    assert salvage_items('{"items": [{"title": "x", "body": ') is None
    assert salvage_items('<think>I will say {"items": [{"title": "t", "body": "b"}]}') is None


def test_salvage_also_strips_a_closed_think_block():
    text = "<think>reasoning</think>" + _broken_middle()
    out = salvage_items(text)
    assert out is not None and len(out["items"]) == 2


# --- describe_json_error ---------------------------------------------------------


def test_describe_json_error_shows_the_offending_region_on_one_line():
    text = _broken_middle()
    with pytest.raises(json.JSONDecodeError) as ei:
        json.loads(text)
    ctx = describe_json_error(text, ei.value)
    assert "\n" not in ctx
    assert len(ctx) <= 200
    assert '"we said "hi" here"' in ctx, ctx
    assert "title 1" not in ctx or "title 3" not in ctx, "a window, not the whole body"


def test_describe_json_error_uses_the_tail_for_a_truncated_reply():
    full = json.dumps(_THREE)
    cut = full[: full.index('"title 3"') + 6]
    with pytest.raises(json.JSONDecodeError) as ei:
        json.loads(cut)
    ctx = describe_json_error(cut, ei.value)
    assert cut.endswith('"title'), "sanity: cut inside item 3's title string"
    assert ctx.endswith('"title') and len(ctx) <= 200, ctx
    assert ctx.startswith("…"), "a window, not the whole body"


def test_describe_json_error_without_a_position_still_returns_a_window():
    ctx = describe_json_error("x" * 1000, ValueError("no JSON object found"))
    assert 0 < len(ctx) <= 200


def test_describe_json_error_scrubs_a_credential_in_the_window():
    """The window reaches journald and ingest_queue.error — a quote that echoed a
    token must be redacted there exactly like the dump body (rule 12)."""
    token = "ghp_" + "0" * 36
    text = '{"items": [{"title": "t", "body": "token ' + token + ' and "broke"}]}'
    with pytest.raises(json.JSONDecodeError) as ei:
        json.loads(text)
    ctx = describe_json_error(text, ei.value)
    assert token not in ctx, ctx
    assert '"broke' in ctx, "the offending region is still shown"
    assert len(ctx) <= 200


def test_describe_json_error_tolerates_a_non_string_reply():
    """A null/number/list message.content must never turn into a raw TypeError."""
    for content in (None, 123, ["x"]):
        ctx = describe_json_error(content, TypeError("message.content is not a str"))
        assert isinstance(ctx, str) and 0 < len(ctx) <= 200


# --- record_unparseable ----------------------------------------------------------


def _dumps_dir() -> Path:
    return Path(settings.kb_state_dir).expanduser() / "extract-failures"


def test_record_unparseable_writes_a_scrubbed_dump_with_a_header(monkeypatch):
    monkeypatch.setattr(settings, "kb_extract_failure_dumps", 20)
    content = '{"items": [{"title": "t", "body": "token ghp_' + "0" * 36 + '"}, broken'
    path = record_unparseable("qwen3:14b", content, ValueError("Expecting ',' delimiter"))
    assert path is not None and path.exists()
    assert path.parent == _dumps_dir()
    assert path.name.endswith(".txt") and "-qwen3-14b-" in path.name
    text = path.read_text(encoding="utf-8")
    assert "# model: qwen3:14b" in text
    assert "# error: ValueError: Expecting ',' delimiter" in text
    assert f"# len: {len(content)}" in text
    assert "# utc: " in text and "+00:00" in text
    assert "ghp_" + "0" * 36 not in text, "the dump is scrubbed like every ingest path"
    assert "broken" in text, "the raw body is there for the post-mortem"
    assert oct(path.stat().st_mode & 0o777) == "0o600", "transcript-derived: owner-only"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"


def test_record_unparseable_keeps_only_the_newest_n(monkeypatch):
    monkeypatch.setattr(settings, "kb_extract_failure_dumps", 3)
    paths = [record_unparseable("m", f"bad {i}", ValueError("x")) for i in range(5)]
    assert all(p is not None for p in paths)
    left = sorted(p.name for p in _dumps_dir().glob("*.txt"))
    assert len(left) == 3
    assert left == sorted(p.name for p in paths[-3:]), "the three NEWEST survive"


def test_record_unparseable_zero_disables(monkeypatch):
    monkeypatch.setattr(settings, "kb_extract_failure_dumps", 0)
    assert record_unparseable("m", "bad", ValueError("x")) is None
    assert not _dumps_dir().exists()


def test_record_unparseable_never_raises_on_an_unwritable_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "kb_extract_failure_dumps", 5)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file, not a directory")
    monkeypatch.setattr(settings, "kb_state_dir", blocker)
    assert record_unparseable("m", "bad", ValueError("x")) is None


def test_record_unparseable_expands_tilde(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "kb_extract_failure_dumps", 5)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(settings, "kb_state_dir", Path("~/state"))
    path = record_unparseable("m", "bad", ValueError("x"))
    assert path is not None and path.is_relative_to(tmp_path / "state")

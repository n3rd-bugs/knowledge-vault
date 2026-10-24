"""kbcore.unicode_safe — lone surrogates never reach a strict UTF-8 encoder.

Incident 2026-10-18: 26 rows of repeater session 0854af52 failed all three
attempts with ``UnicodeEncodeError: 'utf-8' codec can't encode character
'\\ud834' ... surrogates not allowed``. The transcript was clean; an extractor's
JSON reply carried a ``"\\ud834"`` escape with no low half, ``json.loads`` made
it a lone surrogate, and httpx's strict ``json=`` encoder (the ensemble's
clustering embed) raised. The pinned seed reproduced the reply every attempt.

These pin the sanitiser and its three boundaries — the model-JSON parse (strict
parse AND salvage), the embed request, and transcript assembly. Pure Python, no
DB; HTTP is faked the way tests/test_embed.py and test_llm_extract_retry.py do,
except the fakes build a REAL ``httpx.Request(json=...)`` so httpx's own strict
encoder runs exactly as in production. The drain-level proof is in
tests/test_unicode_safe_drain.py.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from kbcore import embed, llm, llm_json
from kbcore.config import settings
from kbcore.embed import embed_texts as _REAL_EMBED_TEXTS  # before conftest's autouse fake
from kbcore.unicode_safe import REPLACEMENT, clean, clean_obj
from kbworker.sessions import assemble_text, assemble_transcript

HIGH = "\ud834"  # the incident's code point: a high surrogate with no low half
LOW = "\udd1e"
ROBOT = "\U0001f916"  # the session's one non-BMP char — a VALID astral code point
ESC_HIGH = "\\ud834"  # the 6-char JSON escape the extractor emitted


def _encodes(x: object) -> None:
    """The exact strict encoder that raised in production (httpx encode_json)."""
    json.dumps(x, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


# --- clean ---------------------------------------------------------------------


def test_the_incident_is_reproducible_without_the_sanitiser():
    """Guard the premise: json.loads really yields an unencodable str."""
    raw = json.loads('{"t": "x' + ESC_HIGH + 'y"}')["t"]
    assert raw == "x" + HIGH + "y"
    with pytest.raises(UnicodeEncodeError, match="surrogates not allowed"):
        _encodes(raw)


def test_clean_replaces_a_lone_high_and_a_lone_low_surrogate():
    assert clean("a" + HIGH + "b") == "a" + REPLACEMENT + "b"
    assert clean("a" + LOW + "b") == "a" + REPLACEMENT + "b"
    assert clean(HIGH + HIGH + LOW) == REPLACEMENT * 3
    ends = chr(0xD800) + chr(0xDFFF)  # two code points: a Python str never joins them
    assert clean(ends) == REPLACEMENT * 2, "the range ends are both covered"
    _encodes(clean("x" + HIGH + LOW + "y"))


def test_clean_leaves_a_valid_astral_char_alone_and_returns_the_same_object():
    s = "bot " + ROBOT + " says hi"
    assert len(ROBOT) == 1, "a valid pair is ONE code point in a Python str"
    assert clean(s) is s
    plain = "nothing to see"
    assert clean(plain) is plain
    # An escaped VALID pair joins on json.loads and survives cleaning intact.
    joined = json.loads('"\\ud83e\\udd16"')
    assert joined == ROBOT and clean(joined) is joined


# --- clean_obj -----------------------------------------------------------------


def test_clean_obj_recurses_dicts_lists_tuples_including_keys():
    dirty = {
        "items": [
            {"title": "t" + HIGH, "tags": ["ok", "x" + LOW], "n": 3},
            ("tuple" + HIGH, 1.5, None),
        ],
        "k" + HIGH: True,
    }
    out = clean_obj(dirty)
    assert out == {
        "items": [
            {"title": "t" + REPLACEMENT, "tags": ["ok", "x" + REPLACEMENT], "n": 3},
            ("tuple" + REPLACEMENT, 1.5, None),
        ],
        "k" + REPLACEMENT: True,
    }
    assert isinstance(out["items"][1], tuple), "a tuple stays a tuple"
    _encodes(out)
    assert dirty["items"][0]["title"] == "t" + HIGH, "the input is not mutated"


def test_clean_obj_leaves_non_str_values_untouched():
    for v in (0, 1.25, None, True, b"\xed\xa0\xb4", object()):
        assert clean_obj(v) is v
    mixed = [1, None, b"raw", 2.5]
    assert clean_obj(mixed) is mixed


def test_clean_obj_is_identity_when_clean_at_every_level():
    clean_sub = {"title": "fine", "tags": ["a", "b"]}
    reply = {"items": [clean_sub, {"title": "bad" + HIGH}]}
    assert clean_obj(clean_sub) is clean_sub
    out = clean_obj(reply)
    assert out is not reply
    assert out["items"][0] is clean_sub, "a clean subtree is reused, not copied"
    whole = {"items": [dict(clean_sub)], "meta": ("x", 1)}
    assert clean_obj(whole) is whole


def _nest(leaf: object, depth: int) -> list:
    node: object = leaf
    for _ in range(depth):
        node = [node]
    return node  # type: ignore[return-value]


def test_clean_obj_survives_nesting_far_past_the_recursion_limit():
    """json.loads on 3.12+ parses 5,000-deep arrays (the C scanner no longer
    spends the Python recursion budget); a recursive walk would then raise a
    RecursionError the parse callers (which catch ValueError/TypeError) miss.
    Built directly so the test means the same thing on 3.11."""
    depth = 5000
    node = clean_obj(_nest(HIGH, depth))
    for _ in range(depth):
        node = node[0]
    assert node == REPLACEMENT
    deep_clean = _nest(1, depth)
    assert clean_obj(deep_clean) is deep_clean


def test_clean_obj_handles_shared_subtrees_and_cycles():
    shared = ["s" + HIGH]
    out = clean_obj({"a": shared, "b": [shared]})
    assert out["a"] == ["s" + REPLACEMENT] and out["a"] is out["b"][0], "rebuilt once, shared"
    loop: list = ["x" + HIGH]
    loop.append(loop)
    got = clean_obj(loop)  # terminates; JSON can never produce this
    assert got[0] == "x" + REPLACEMENT


# --- the model-JSON boundary -----------------------------------------------------


def _reply(title: str) -> str:
    return '{"items": [{"type": "gotcha", "title": "' + title + '", "body": "b", "quote": "q"}]}'


def test_parse_model_json_sanitises_an_escaped_lone_surrogate():
    data = llm_json.parse_model_json(_reply("spi " + ESC_HIGH + " clock"))
    assert data["items"][0]["title"] == "spi " + REPLACEMENT + " clock"
    _encodes(data)


def test_parse_model_json_sanitises_a_raw_lone_surrogate():
    data = llm_json.parse_model_json(_reply("spi " + HIGH + " clock"))
    assert data["items"][0]["title"] == "spi " + REPLACEMENT + " clock"
    _encodes(data)


@pytest.mark.parametrize(
    "wrap",
    [
        lambda r: "<think>hm</think>\n```json\n" + r + "\n```",  # fenced-block return path
        lambda r: "Here you go: " + r + " -- done",  # outermost {...} return path
    ],
)
def test_every_parse_return_path_is_sanitised(wrap):
    data = llm_json.parse_model_json(wrap(_reply(ESC_HIGH)))
    assert data["items"][0]["title"] == REPLACEMENT


def test_parse_model_json_keeps_a_valid_escaped_pair_and_the_seam():
    data = llm_json.parse_model_json(_reply("bot \\ud83e\\udd16"))
    assert data["items"][0]["title"] == "bot " + ROBOT
    assert llm.parse_model_json is llm_json.parse_model_json, "the documented seam holds"


def test_salvaged_items_are_sanitised_too():
    """Salvage decodes with raw_decode, bypassing parse_model_json — it must not
    become the side door the escape walks through."""
    good = '{"type": "gotcha", "title": "t' + ESC_HIGH + '", "body": "b", "quote": "q"}'
    broken = '{"type": "gotcha", "title": "x", "body": "we said "hi"", "quote": "q"}'
    reply = '{"items": [' + good + ", " + broken + "]}"
    with pytest.raises(ValueError):
        json.loads(reply)
    got = llm_json.salvage_items(reply)
    assert got is not None and [it["title"] for it in got["items"]] == ["t" + REPLACEMENT]
    _encodes(got)


def test_extract_items_over_http_returns_encodable_items(monkeypatch):
    """The real chain: _extract_call → recover_items → parse_model_json."""
    monkeypatch.setattr(settings, "kb_extract_json_retry", 1)
    content = _reply("lone " + ESC_HIGH)

    def post(url, *, json=None, timeout=None):  # noqa: A002 — httpx's own kwarg name
        req = httpx.Request("POST", url, json=json)  # strict, exactly as httpx.post
        return httpx.Response(200, json={"message": {"content": content}}, request=req)

    monkeypatch.setattr(httpx, "post", post)
    items = llm.extract_items("some transcript")
    assert [it["title"] for it in items] == ["lone " + REPLACEMENT]
    _encodes(items)


def test_the_transport_envelope_decode_is_a_boundary_too(monkeypatch):
    """resp.json() is itself a json.loads: plain-text replies (summaries, deep-search
    answers) never reach parse_model_json, so _chat / _extract_call clean what the
    envelope decoded. Ollama's Go encoder never writes a surrogate escape; this pins
    the guarantee for any server speaking its API."""
    envelope = ('{"message": {"content": "summary ' + ESC_HIGH + ' text"}}').encode("ascii")

    def post(url, *, json=None, timeout=None):  # noqa: A002
        req = httpx.Request("POST", url, json=json)
        return httpx.Response(200, content=envelope, request=req)

    monkeypatch.setattr(httpx, "post", post)
    assert llm.summarize_items("topic", [{"title": "t", "body": "b"}]) == (
        "summary " + REPLACEMENT + " text"
    )
    assert llm.chat([{"role": "user", "content": "q"}]) == "summary " + REPLACEMENT + " text"
    assert llm._extract_call({"model": "m"}) == "summary " + REPLACEMENT + " text"


# --- the embed request -----------------------------------------------------------


def test_embed_texts_encodes_a_lone_surrogate_input(monkeypatch, caplog):
    """THE incident's frame (embed.py:134): httpx's strict json= encoder raised."""
    monkeypatch.setattr(embed, "embed_texts", _REAL_EMBED_TEXTS)  # undo the autouse fake
    monkeypatch.setattr(settings, "kb_embed_url", "")
    embed.reset_host_health()
    sent: list[list[str]] = []

    def post(url, *, json=None, timeout=None):  # noqa: A002
        req = httpx.Request("POST", url, json=json)  # raises on HEAD: surrogates not allowed
        sent.append(list(json["input"]))
        vecs = [[0.1] * settings.kb_embed_dim for _ in json["input"]]
        return httpx.Response(200, json={"embeddings": vecs}, request=req)

    monkeypatch.setattr(httpx, "post", post)
    texts = ["clean chunk", "title " + HIGH + ": body " + LOW, ROBOT]
    with caplog.at_level(logging.WARNING, logger="kbcore.embed"):
        vectors = embed.embed_texts(texts)
    assert len(vectors) == 3
    assert sent == [["clean chunk", "title " + REPLACEMENT + ": body " + REPLACEMENT, ROBOT]]
    assert texts[1] == "title " + HIGH + ": body " + LOW, "the caller's list is not mutated"
    assert "lone surrogate" in caplog.text, "a leak past the upstream boundaries is LOUD"

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="kbcore.embed"):
        embed.embed_texts(["all clean " + ROBOT])
    assert "lone surrogate" not in caplog.text, "a clean batch logs nothing"


# --- transcript assembly -----------------------------------------------------------


def _jsonl(*texts: str) -> bytes:
    """Claude Code-shaped lines; json.dumps(ensure_ascii=True) writes a lone
    surrogate as its \\uXXXX escape — exactly what Node's JSON.stringify emits."""
    blocks = ([{"type": "text", "text": t}] for t in texts)
    rows = [{"type": "assistant", "message": {"content": b}} for b in blocks]
    return ("\n".join(json.dumps(r) for r in rows) + "\n").encode("ascii")


def test_assembly_cleans_an_orphan_escape_from_a_truncated_pair():
    """A Node.js .slice() that split U+1F916 leaves '\\ud83e' alone in the JSONL."""
    data = _jsonl("cut here \ud83e", "intact " + ROBOT)
    assert b"\\ud83e" in data and b"\\ud83e\\udd16" in data, "plain ASCII escapes in the JSONL"
    for out in (assemble_transcript(data), assemble_text(data), assemble_text(data.decode())):
        assert "cut here " + REPLACEMENT in out
        assert "intact " + ROBOT in out, "a valid astral char is kept"
        out.encode("utf-8")  # strict

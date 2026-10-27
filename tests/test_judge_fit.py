"""kbcore.judge_fit — the pair-judge prompt budget (KB 55589). Pure: no DB, no Ollama."""

from __future__ import annotations

import logging

import pytest

from kbcore import judge_fit
from kbcore.chunk import count_tokens
from kbcore.config import settings
from kbcore.llm_prompts import CONTRADICT_SYSTEM, EVIDENCE_SYSTEM


def _words(n: int, tag: str = "w") -> str:
    """~2-3 cl100k tokens per word, deterministic, no long merges."""
    return " ".join(f"{tag}{i}" for i in range(n))


@pytest.fixture(autouse=True)
def _num_ctx_16k(monkeypatch):
    monkeypatch.setattr(settings, "kb_llm_num_ctx", 16384)
    monkeypatch.setattr(judge_fit, "FIT_STATS", {"contradict": 0, "evidence": 0, "same_fact": 0})


def _tokens(messages: list[dict[str, str]]) -> int:
    return count_tokens(messages[0]["content"]) + count_tokens(messages[1]["content"])


def test_ceiling_is_the_window_minus_the_reply_over_the_tokenizer_margin(monkeypatch):
    assert judge_fit.judge_ceiling() == 8904  # (16384 - 6144) / 1.15
    monkeypatch.setattr(settings, "kb_llm_num_ctx", 32768)  # read at call time
    assert judge_fit.judge_ceiling() == int((32768 - 6144) / 1.15)
    monkeypatch.setattr(settings, "kb_llm_num_ctx", 2048)
    assert judge_fit.judge_ceiling() == 1024  # floor
    # FROZEN: the window every pre-fit verdict was judged in, at the reserve decided
    # 2026-10-27. Never move it with a later reserve or num_ctx change.
    assert judge_fit.LEGACY_CEILING == 8904
    assert judge_fit.judge_ceiling(16384) == 8904  # today's reserve — only THIS may change
    assert judge_fit.FIT_EPOCH.utcoffset() is not None  # TIMESTAMPTZ-comparable (rule 7)


def test_a_pair_that_fits_is_sent_byte_identical(caplog):
    caplog.set_level(logging.WARNING, logger="kbcore.judge_fit")
    msgs = judge_fit.contradict_messages("Title A", "body a\nline 2", "Title B", "body b")
    assert msgs == [
        {"role": "system", "content": CONTRADICT_SYSTEM},
        {"role": "user", "content": "Item A: Title A\nbody a\nline 2\n\nItem B: Title B\nbody b"},
    ]
    assert judge_fit.FIT_STATS["contradict"] == 0
    assert not caplog.records


def test_whitespace_is_never_normalised_on_the_byte_identical_path():
    a_body, b_body = "  lead\n\n\tindented  \n\n\n", "\n\ntrailing   \r\n"
    user = judge_fit.contradict_messages(" A ", a_body, "B\t", b_body)[1]["content"]
    assert user == f"Item A:  A \n{a_body}\n\nItem B: B\t\n{b_body}"  # the legacy f-string


def test_an_oversized_pair_keeps_both_heads_and_marks_the_cut(caplog):
    caplog.set_level(logging.WARNING, logger="kbcore.judge_fit")
    a_body, b_body = _words(12000, "a"), _words(12000, "b")
    assert count_tokens(a_body) + count_tokens(b_body) > 2 * judge_fit.judge_ceiling()
    msgs = judge_fit.contradict_messages("Spec A", a_body, "Spec B", b_body)
    assert _tokens(msgs) <= judge_fit.judge_ceiling()
    user = msgs[1]["content"]
    assert msgs[0]["content"] == CONTRADICT_SYSTEM  # the system prompt is never cut
    head_a, rest = user.removeprefix("Item A: Spec A\n").split("\n\nItem B: Spec B\n")
    for head, body in ((head_a, a_body), (rest, b_body)):
        kept, marker = head.rsplit("\n[... ", 1)
        assert body.startswith(kept) and len(kept) > 1000
        omitted = int(marker.removesuffix(" tokens omitted ...]"))
        assert omitted == count_tokens(body) - count_tokens(kept)
    assert judge_fit.FIT_STATS["contradict"] == 1
    (rec,) = caplog.records
    assert "contradict 'Spec A' / 'Spec B'" in rec.getMessage()
    assert "ceiling 8904" in rec.getMessage()


def test_the_log_label_cannot_carry_a_raw_newline_from_item_text(caplog):
    caplog.set_level(logging.WARNING, logger="kbcore.judge_fit")
    judge_fit.contradict_messages("evil\nFAKE LOG LINE", _words(12000), "B", _words(12000))
    assert "\n" not in caplog.records[-1].getMessage()


def test_a_short_body_stays_whole_and_the_long_one_takes_the_room():
    short = "a short body that is about one decision"
    msgs = judge_fit.contradict_messages("A", short, "B", _words(20000))
    user = msgs[1]["content"]
    assert user.startswith(f"Item A: A\n{short}\n\nItem B: B\n")
    assert _tokens(msgs) <= judge_fit.judge_ceiling()
    assert _tokens(msgs) > judge_fit.judge_ceiling() - 64  # the long body used the room


def test_evidence_fits_three_bodies_and_keeps_a_short_evidence_item_whole():
    msgs = judge_fit.evidence_messages(
        "A", _words(9000, "a"), "B", _words(9000, "b"), "C", "the fix landed in v2"
    )
    assert msgs[0]["content"] == EVIDENCE_SYSTEM
    assert msgs[1]["content"].endswith("\n\nEvidence: C\nthe fix landed in v2")
    assert _tokens(msgs) <= judge_fit.judge_ceiling()
    assert judge_fit.FIT_STATS["evidence"] == 1


def test_evidence_that_fits_is_byte_identical():
    msgs = judge_fit.evidence_messages("A", "x", "B", "y", "C", "z")
    assert msgs[1]["content"] == "Item A: A\nx\n\nItem B: B\ny\n\nEvidence: C\nz"
    assert judge_fit.FIT_STATS["evidence"] == 0


@pytest.mark.parametrize(
    ("sizes", "room", "want"),
    [
        ([10, 100, 1000], 500, [10, 100, 390]),
        ([600, 600], 1000, [500, 500]),
        ([5, 5], 100, [5, 5]),
        ([700, 100], 0, [0, 0]),
        ([700, 100], -50, [0, 0]),
        ([], 100, []),
    ],
)
def test_shares_water_fill(sizes, room, want):
    assert judge_fit.shares(sizes, room) == want


def test_a_cut_inside_a_character_drops_the_partial_bytes_never_a_replacement_char():
    from kbcore.chunk import _enc

    toks = _enc.encode("🙂", disallowed_special=())
    assert len(toks) == 2  # b"\xf0\x9f" + b"\x99\x82": the cut below splits the character
    assert judge_fit._cut(toks, 1) == judge_fit.MARKER.format(n=1)
    assert judge_fit._cut(toks, 2) == "🙂" + judge_fit.MARKER.format(n=0)


@pytest.mark.parametrize(
    "body",
    [
        "a" * 200_000,  # long merges: the cut-and-recount loop must still land under
        "é" * 60_000,  # multi-byte: a cut never ends on half a character
        "🙂 " * 30_000,
        "\n".join(f"| {i} | 0x{i:08x} | {i * 7} |" for i in range(6000)),  # dense table
    ],
)
def test_pathological_bodies_still_fit(body):
    msgs = judge_fit.contradict_messages("A", body, "B", body)
    assert _tokens(msgs) <= judge_fit.judge_ceiling()
    assert "�" not in msgs[1]["content"]


def test_a_frame_over_the_ceiling_degrades_to_markers_and_says_so(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="kbcore.judge_fit")
    monkeypatch.setattr(settings, "kb_llm_num_ctx", 2048)  # ceiling floor: 1024
    huge_title = _words(1500, "t")  # the title alone is over the ceiling
    msgs = judge_fit.contradict_messages(huge_title, "body a", huge_title, _words(3000))
    assert huge_title in msgs[1]["content"]  # titles are never cut
    assert "STILL OVER" in caplog.records[-1].getMessage()


def test_fitting_is_deterministic():
    a, b = _words(15000, "a"), _words(15000, "b")
    assert judge_fit.contradict_messages("A", a, "B", b) == judge_fit.contradict_messages(
        "A", a, "B", b
    )

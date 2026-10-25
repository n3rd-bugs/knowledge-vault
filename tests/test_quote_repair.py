"""kbcore.quote_repair.find_span — pure unit tests (no DB, no LLM).

The locator recovers quotes the model got NEARLY right (whitespace/punctuation
drift) as true verbatim spans of the transcript, and refuses genuinely absent
content (bench 2026-07-18: 62% of ungrounded quotes locate at >= 0.95).

Its worst case is bounded deterministically (2026-10-25: a 5,946-char base64
"quote" pinned the nightly drain for >15 min): a length cap, an exact refine
floor at min_ratio, and a ratio() work budget — none of which may change the
result for an ordinary quote under the cap.
"""

from __future__ import annotations

import base64
import random
import re
import time
from difflib import SequenceMatcher

import pytest

from kbcore import quote_repair
from kbcore.quote_repair import MAX_QUOTE_CHARS, MAX_RATIO_WORK, find_span

SPAN = (
    "Decision: run the DRV8706 SPI bus in mode 1 (CPOL=0, CPHA=1), because mode 0 "
    "clocks data on the wrong edge and every register read returns zeros."
)
TEXT = (
    "We spent the afternoon on the DRV8706 SPI bring-up. "
    + SPAN
    + " After the change the fault register finally reported 0x00 as expected."
)


def test_near_miss_repaired_with_true_span_and_ratio():
    # Whitespace/punctuation drift: comma dropped inside the parens, final period
    # dropped — the classic near-miss shape the bench measured.
    mangled = SPAN.replace("(CPOL=0, CPHA=1)", "(CPOL=0 CPHA=1)").replace("zeros.", "zeros")
    assert mangled not in TEXT, "sanity: genuinely fails the exact substring check"

    result = find_span(mangled, TEXT, 0.95)
    assert result is not None
    located, ratio = result
    assert located in TEXT, "the located span is a verbatim substring by construction"
    assert 0.95 <= ratio < 1.0
    # The located span carries the TRUE transcript punctuation, not the model's.
    assert "(CPOL=0, CPHA=1)" in located
    assert "clocks data on the wrong edge" in located


def test_exact_substring_locates_perfectly():
    located, ratio = find_span(SPAN, TEXT, 0.95)
    assert ratio == 1.0
    assert located == SPAN


def test_genuinely_absent_content_returns_none():
    assert find_span("the moon is made of green cheese and telepathy", TEXT, 0.95) is None


def test_threshold_respected():
    # Heavier mangling: measure the best achievable ratio, then bracket it.
    heavy = SPAN.replace("mode 1", "mode one").replace("zeros", "0s").replace("Decision:", "")
    located, ratio = find_span(heavy, TEXT, 0.0)
    assert located in TEXT
    assert ratio < 1.0
    assert find_span(heavy, TEXT, ratio) == (located, ratio), "at the floor: accepted"
    assert find_span(heavy, TEXT, ratio + 1e-4) is None, "just above the floor: refused"


def test_empty_quote_and_empty_text():
    assert find_span("", TEXT, 0.5) is None
    assert find_span("   \n\t ", TEXT, 0.5) is None
    assert find_span(SPAN, "", 0.5) is None


def test_quote_longer_than_text():
    short = "short transcript line"
    quote = short + " plus a long hallucinated continuation that never happened at all"
    result = find_span(quote, short, 0.2)
    assert result is not None
    located, ratio = result
    assert located == short, "window is capped at the whole text"
    assert ratio < 0.95
    assert find_span(quote, short, 0.95) is None


def test_unicode_content():
    span = "فیصلہ: DRV8706 کے لیے SPI موڈ 1 استعمال کریں کیونکہ موڈ 0 میں تمام ریڈز صفر ہیں۔"
    text = "آج ہم نے ڈرائیور کی جانچ کی۔ " + span + " اس کے بعد سب کچھ ٹھیک چلا۔"
    mangled = span.replace("۔", "").replace("فیصلہ:", "فیصلہ :")
    assert mangled not in text

    result = find_span(mangled, text, 0.9)
    assert result is not None
    located, ratio = result
    assert located in text
    assert ratio >= 0.9
    assert "کیونکہ موڈ 0 میں تمام ریڈز صفر ہیں" in located


# ---------------------------------------------------------------------------
# Bounded worst case (2026-10-25)
# ---------------------------------------------------------------------------

_VOCAB = (
    "the drain cohort window quote span extractor judge model token review queue item "
    "repair ratio budget scan pass coarse refine length cap nightly worker session flush "
    "checkpoint transcript chunk embed rerank search project grant scope trust pending "
    "verified source raw archive migration schema index vector keyword leg rank fusion "
    "is was has will must never always only once each every after before when while "
    "because so but and or not with without into from over under per via at on in of to"
).split()


def _prose(seed: int, chars: int) -> str:
    """Deterministic engineering-flavoured prose: sentences, commas, paragraphs."""
    rng = random.Random(seed)
    out: list[str] = []
    size = 0
    while size < chars:
        words = [rng.choice(_VOCAB) for _ in range(rng.randrange(8, 22))]
        if len(words) > 10:
            words[rng.randrange(3, len(words) - 3)] += ","
        sentence = " ".join(words).capitalize() + "."
        sep = "\n\n" if rng.random() < 0.2 else " "
        out.append(sentence + sep)
        size += len(sentence) + len(sep)
    return "".join(out)[:chars]


def _b64(seed: int, nbytes: int) -> str:
    """Deterministic base64 'image data' — the uniform alphabet that defeats pruning."""
    return base64.b64encode(random.Random(seed).randbytes(nbytes)).decode()


def _sub_every(s: str, every: int) -> str:
    """A near-miss copy of ``s``: one character changed every ``every`` chars."""
    return "".join(
        ("A" if c != "A" else "B") if i % every == every // 2 else c for i, c in enumerate(s)
    )


def _timed(quote: str, text: str, min_ratio: float = 0.95):
    t0 = time.perf_counter()
    result = find_span(quote, text, min_ratio)
    return result, time.perf_counter() - t0


def _assert_verbatim_or_none(result, text: str, min_ratio: float = 0.95) -> None:
    if result is not None:  # SEC-3: whatever comes back is a real substring
        located, ratio = result
        assert located in text
        assert ratio >= min_ratio


# A ~20 KB transcript window around a base64 image block — the bench's shape.
BLOB = _b64(2026, 15_000)  # 20,000 chars
B64_TEXT = (
    "The screenshot is attached; the panel renders blank after the resize.\n"
    '{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "'
    + BLOB
    + "\"}}\nAgainst yesterday's capture the canvas never repaints after a DPI change."
)


class _Metered(SequenceMatcher):
    """Counts FULL ratio() evaluations and their cells (len(window) * len(quote))."""

    calls = 0
    cells = 0

    def ratio(self) -> float:
        _Metered.calls += 1
        _Metered.cells += len(self.a) * len(self.b)
        return super().ratio()


@pytest.fixture
def metered(monkeypatch):
    _Metered.calls = _Metered.cells = 0
    monkeypatch.setattr(quote_repair, "SequenceMatcher", _Metered)
    return _Metered


def test_6k_base64_near_miss_returns_none_fast():
    # (a) The bench's exact shape: a 5,946-char base64 "quote" with drift, inside a
    # 20 KB window. Pre-fix: >15 min CPU in the refine pass, hours projected.
    quote = _sub_every(BLOB[1000:6946], 400)
    assert len(quote) == 5946 > MAX_QUOTE_CHARS
    assert quote not in B64_TEXT
    result, elapsed = _timed(quote, B64_TEXT)
    assert result is None, "over the cap: never relocated (counted as unrepairable)"
    assert elapsed < 1.0, f"the cap is a length check, took {elapsed:.2f}s"


def test_1500_char_base64_near_miss_is_fast():
    # (b) A mid-length base64 near-miss: repaired or None, but never slow.
    quote = _sub_every(BLOB[8000:9500], 200)
    assert len(quote) == 1500 and quote not in B64_TEXT
    result, elapsed = _timed(quote, B64_TEXT)
    _assert_verbatim_or_none(result, B64_TEXT)
    assert elapsed < 5.0, f"took {elapsed:.2f}s"


def test_base64_just_under_the_cap_is_bounded(metered):
    # Both base64 shapes at ~950 chars, where the full scan still runs. The incident
    # shape (verbatim start, hallucinated rest) can never reach min_ratio, so the
    # refine floor leaves it ONE coarse pass of full ratio() work (pre-fix: ~10x that,
    # 2,593 evaluations); the near-miss still repairs to the span it always did.
    n = 950
    coarse = sum(1 for _ in quote_repair._coarse_windows(n, len(B64_TEXT)))
    drift = BLOB[5000:5570] + _b64(7, 300)[:380]
    assert len(drift) == n and drift not in B64_TEXT
    result, elapsed = _timed(drift, B64_TEXT)
    assert result is None
    assert metered.calls <= coarse, "no full ratio() in the refine for an unreachable quote"
    assert elapsed < 30.0, f"took {elapsed:.2f}s"

    near = _sub_every(BLOB[5000:5950], 200)
    result, elapsed = _timed(near, B64_TEXT)
    _assert_verbatim_or_none(result, B64_TEXT)
    assert result == (BLOB[5000:5950], 0.9947368421052631)  # == pre-fix (cbb6b45)
    assert elapsed < 30.0, f"took {elapsed:.2f}s"


def test_long_prose_near_miss_under_the_cap_is_unchanged():
    # (c) A ~900-char prose near-miss in a 9 KB window of the same kind of text (the
    # costliest ordinary shape: quick_ratio prunes prose weakly). It must repair to
    # EXACTLY the span + ratio the pre-fix implementation (cbb6b45) returned.
    text = _prose(11, 9_000)
    start = text.index(". ", 3_200) + 2  # whole sentences, like a real quote
    span = text[start : text.rindex(".", start, start + 990) + 1]
    quote = re.sub(r"\s+", " ", span).replace(",", "", 1)
    quote = (quote[: len(quote) // 2] + quote[len(quote) // 2 + 1 :]).rstrip(".")
    assert 900 < len(quote) <= MAX_QUOTE_CHARS and quote not in text
    assert find_span(quote, text, 0.95) == (text[3202 : 3202 + 914], 0.9956236323851203)


def test_length_cap_boundary(monkeypatch):
    # (d) Exactly MAX_QUOTE_CHARS (after strip) is attempted; one more char is not.
    prose = _prose(6, MAX_QUOTE_CHARS + 200)
    s = 0  # both quotes must END on a non-space, or strip() would shorten them
    while any(prose[i].isspace() for i in (s, s + MAX_QUOTE_CHARS, s + MAX_QUOTE_CHARS + 1)):
        s += 1
    seg = prose[s : s + MAX_QUOTE_CHARS + 2]
    text = "Intro line. " + seg + " Outro."  # tight window: cheap scans
    at_cap = seg[:500] + seg[501 : MAX_QUOTE_CHARS + 1]  # one char dropped
    over_cap = seg[:500] + seg[501:]
    assert (len(at_cap), len(over_cap)) == (MAX_QUOTE_CHARS, MAX_QUOTE_CHARS + 1)
    assert at_cap not in text and over_cap not in text

    located, ratio = find_span(at_cap, text, 0.95)
    assert located in text and ratio >= 0.95
    assert find_span(f"  \n{at_cap}\t ", text, 0.95) == (located, ratio), "strip first"
    assert find_span(over_cap, text, 0.95) is None
    # ...and it is the cap, not the content, that refuses it.
    monkeypatch.setattr(quote_repair, "MAX_QUOTE_CHARS", MAX_QUOTE_CHARS + 1)
    raised = find_span(over_cap, text, 0.95)
    assert raised is not None
    _assert_verbatim_or_none(raised, text)


def test_work_budget_is_a_hard_deterministic_cap(monkeypatch, metered):
    # Whatever the input, full ratio() work stops at the budget (plus the one
    # evaluation that crosses it) and the best-so-far is still judged honestly.
    budget = 20_000_000
    monkeypatch.setattr(quote_repair, "MAX_RATIO_WORK", budget)
    quote = BLOB[5000:5570] + _b64(7, 300)[:380]
    assert find_span(quote, B64_TEXT, 0.95) is None
    assert budget <= metered.cells <= budget + int(len(quote) * 1.3) * len(quote)
    assert MAX_RATIO_WORK > budget, "sanity: the real budget is not what bound this run"


def test_spent_budget_returns_best_so_far_only_if_it_clears_min_ratio(monkeypatch, metered):
    # One cell of budget = exactly one full evaluation: the first coarse window.
    monkeypatch.setattr(quote_repair, "MAX_RATIO_WORK", 1)
    mangled = SPAN.replace("(CPOL=0, CPHA=1)", "(CPOL=0 CPHA=1)").replace("zeros.", "zeros")
    first = TEXT[: int(len(mangled) * 0.7)]
    expected = SequenceMatcher(None, first, mangled, autojunk=False).ratio()
    assert find_span(mangled, TEXT, 0.0) == (first, expected)
    assert metered.calls == 1
    assert expected < 0.95
    assert find_span(mangled, TEXT, 0.95) is None, "the best so far fails the floor"


@pytest.mark.parametrize(
    "quote,text",
    [
        (SPAN.replace("mode 1", "mode one").replace("zeros", "0s"), TEXT),
        (
            "def gate(q, text):  return find_span(q, text, 0.95) if ratio > 0 else None",
            "x = 1\ndef gate(q, text):\n    return find_span(q, text, 0.95) if r > 0 else None\n",
        ),
        (_sub_every(BLOB[300:500], 50), B64_TEXT[:2000]),
        (BLOB[300:420] + _b64(9, 90)[:80], B64_TEXT[:2000]),
    ],
    ids=["prose-heavy-mangle", "code-whitespace", "base64-near-miss", "base64-drift"],
)
def test_refine_floor_is_exact_at_every_threshold(quote, text):
    # min_ratio=0 disables the floor (the pre-fix scan); any higher threshold must
    # return exactly that best window when it clears, and None otherwise.
    unfloored = find_span(quote, text, 0.0)
    assert unfloored is not None
    best = unfloored[1]
    for threshold in (0.5, 0.8, 0.9, 0.95, best, min(1.0, best + 1e-9)):
        expected = unfloored if best >= threshold else None
        assert find_span(quote, text, threshold) == expected, threshold


def test_a_long_quote_verbatim_after_strip_is_grounded_past_the_cap():
    # gate_quotes' exact check runs on the UNSTRIPPED quote; a long block quoted with a
    # stray leading/trailing space must still ground at 1.0 (as before the cap), since
    # nothing needs relocating and the cap's cost rationale does not apply.
    block = " ".join(f"line {i}: the regulator re-arms after UVLO" for i in range(40))
    text = "preamble\n" + block + "\nepilogue"
    assert len(block) > quote_repair.MAX_QUOTE_CHARS
    assert quote_repair.find_span(" " + block + " ", text, 0.95) == (block, 1.0)

"""kbcore.quote_repair.find_span — pure unit tests (no DB, no LLM).

The locator recovers quotes the model got NEARLY right (whitespace/punctuation
drift) as true verbatim spans of the transcript, and refuses genuinely absent
content (bench 2026-07-18: 62% of ungrounded quotes locate at >= 0.95).
"""

from __future__ import annotations

from kbcore.quote_repair import find_span

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

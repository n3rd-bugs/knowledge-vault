"""kbcore.quote_repair — fuzzy relocation of nearly-verbatim extractor quotes.

Extraction models quote the transcript NEARLY right — whitespace collapsed,
punctuation drifted, a trailing period dropped — and SEC-3's exact-substring
check rejects the lot: on the 2026-07-18 bench 62% of ALL ungrounded quotes
fuzzy-located to a true verbatim span at difflib ratio >= 0.95 (usable-item
survival for the incumbent extractor: 46% -> 92%). :func:`find_span` finds that
span; the CALLER stores the LOCATED span, never the model's version, so SEC-3's
verbatim invariant is preserved by construction. Pure stdlib — no LLM, no DB.
"""

from __future__ import annotations

from difflib import SequenceMatcher


def find_span(quote: str, text: str, min_ratio: float) -> tuple[str, float] | None:
    """Best-matching window of ``text`` for a nearly-verbatim ``quote``.

    Coarse pass: windows of ``len(quote)`` and ±30%, scanned at step ``len//4``;
    refine pass: every start within ±(coarse step) of the best coarse hit, over a
    finer sweep of window lengths (the measured prototype algorithm, bench
    2026-07-18). Returns ``(located_span, ratio)`` when the best
    ``SequenceMatcher(autojunk=False)`` ratio clears ``min_ratio``, else ``None``.
    The returned span is a verbatim substring of ``text`` by construction.
    """
    quote = quote.strip()
    n = len(quote)
    if n == 0 or not text:
        return None

    sm = SequenceMatcher(autojunk=False)
    sm.set_seq2(quote)
    best: list = [0.0, 0, 0]  # ratio, start, window length

    def _scan(start: int, wl: int) -> None:
        sm.set_seq1(text[start : start + wl])
        # Cheap upper bounds first — skip windows that cannot beat the best.
        if sm.real_quick_ratio() < best[0] or sm.quick_ratio() < best[0]:
            return
        r = sm.ratio()
        if r > best[0]:
            best[0], best[1], best[2] = r, start, wl

    # Coarse pass.
    for wl in sorted({max(1, int(n * 0.7)), n, int(n * 1.3)}):
        wl = min(wl, len(text))
        step = max(1, wl // 4)
        for start in range(0, len(text) - wl + 1, step):
            _scan(start, wl)
    if best[0] == 0.0:
        return None  # no window shares anything with the quote

    # Refine around the best coarse hit.
    step0 = max(1, best[2] // 4)
    s0 = best[1]
    fine_lengths = sorted(
        set(range(max(1, int(n * 0.7)), int(n * 1.3) + 1, max(1, n // 10))) | {n, best[2]}
    )
    for wl in fine_lengths:
        wl = min(wl, len(text))
        for start in range(max(0, s0 - step0), min(len(text) - wl, s0 + step0) + 1):
            _scan(start, wl)

    ratio, start, wl = best
    if ratio < min_ratio:
        return None
    return text[start : start + wl], ratio

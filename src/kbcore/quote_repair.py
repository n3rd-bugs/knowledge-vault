"""kbcore.quote_repair — fuzzy relocation of nearly-verbatim extractor quotes.

Extraction models quote the transcript NEARLY right — whitespace collapsed,
punctuation drifted, a trailing period dropped — and SEC-3's exact-substring
check rejects the lot: on the 2026-07-18 bench 62% of ALL ungrounded quotes
fuzzy-located to a true verbatim span at difflib ratio >= 0.95 (usable-item
survival for the incumbent extractor: 46% -> 92%). :func:`find_span` finds that
span; the CALLER stores the LOCATED span, never the model's version, so SEC-3's
verbatim invariant is preserved by construction. Pure stdlib — no LLM, no DB.

BOUNDED (2026-10-25). A bench caught gemma3:12b "quoting" a 5,946-char base64
blob: a uniform character mix defeats both cheap upper bounds
(``real_quick_ratio``/``quick_ratio``), so nearly every window paid a full
O(n·m) ``ratio()`` — one call sat >15 min CPU-bound in the refine pass (hours
projected) inside the single-threaded nightly drain, GPU idle. The worst case is
now bounded DETERMINISTICALLY (never a wall clock — a result must not depend on
machine speed or load) by a length cap, an exact refine floor, and a work budget;
see the constants and :func:`find_span`.
"""

from __future__ import annotations

from collections.abc import Iterator
from difflib import SequenceMatcher

# Longest model quote (after strip) worth relocating; longer ones are rejected as
# unrepairable, exactly like a miss. Production 2026-10-25: the ORIGINAL model quotes
# behind 17,116 repaired items run p99.9 = 904 chars, only 10 above 1,000 (max 3,159).
# Cost grows ~n^3 (ratio() ~n^2 per window, ~n windows). A 1,894-case harness
# (near-misses in 4-25 KB prose/code/log/JSON/base64 windows) timed legit ~2,000-char
# prose repairs at 13-77 s EVEN with the floor below, and base64 near-misses of that
# length need comparable work (8-43 s) — so no budget holds a 2,000 cap to seconds
# without changing ordinary results. At 1,000 every base64 shape ran <= 3.5 s on a
# heavily loaded box; the price is those 10 repairs (0.06%).
MAX_QUOTE_CHARS = 1_000

# Backstop budget for FULL ratio() evaluations, in cells = len(window) * len(quote)
# summed over them (difflib's comparison area: a deterministic, machine-independent
# proxy for its cost). Once spent, the scan stops and the best window found SO FAR is
# judged against min_ratio. The harness's costliest ordinary repair under the cap
# reached its final best at 386M cells, so 1G (2.6x) left every ordinary result
# byte-identical while capping ANY input — whatever defeats the cheap bounds — at ~1G
# cells. A count of calls could not do both: short quotes legitimately need 2,576.
# The cheap bounds are not charged: they are O(window) per window, bounded by the cap.
MAX_RATIO_WORK = 1_000_000_000


def _coarse_windows(n: int, size: int) -> Iterator[tuple[int, int]]:
    """(start, length) windows of the coarse pass, in scan order."""
    for wl in sorted({max(1, int(n * 0.7)), n, int(n * 1.3)}):
        wl = min(wl, size)
        step = max(1, wl // 4)
        for start in range(0, size - wl + 1, step):
            yield start, wl


def _refine_windows(n: int, size: int, s0: int, wl0: int) -> Iterator[tuple[int, int]]:
    """(start, length) windows of the refine pass around the coarse hit (s0, wl0)."""
    step0 = max(1, wl0 // 4)
    fine_lengths = sorted(
        set(range(max(1, int(n * 0.7)), int(n * 1.3) + 1, max(1, n // 10))) | {n, wl0}
    )
    for wl in fine_lengths:
        wl = min(wl, size)
        for start in range(max(0, s0 - step0), min(size - wl, s0 + step0) + 1):
            yield start, wl


def find_span(quote: str, text: str, min_ratio: float) -> tuple[str, float] | None:
    """Best-matching window of ``text`` for a nearly-verbatim ``quote``.

    Coarse pass: windows of ``len(quote)`` and ±30%, scanned at step ``len//4``;
    refine pass: every start within ±(coarse step) of the best coarse hit, over a
    finer sweep of window lengths (the measured prototype algorithm, bench
    2026-07-18). Returns ``(located_span, ratio)`` when the best
    ``SequenceMatcher(autojunk=False)`` ratio clears ``min_ratio``, else ``None``.
    The returned span is a verbatim substring of ``text`` by construction.

    Bounded: a quote over :data:`MAX_QUOTE_CHARS` is never relocated; the refine
    skips windows whose upper bound is below ``min_ratio`` (exact — see below);
    and the scan stops once :data:`MAX_RATIO_WORK` is spent, judging the best
    window found so far. Below the budget the result is unchanged.
    """
    quote = quote.strip()
    n = len(quote)
    if n == 0 or n > MAX_QUOTE_CHARS or not text:
        return None

    sm = SequenceMatcher(autojunk=False)
    sm.set_seq2(quote)
    best: list = [0.0, 0, 0]  # ratio, start, window length
    spent = 0  # cells of full ratio() work so far

    def _sweep(windows: Iterator[tuple[int, int]], floor: float) -> bool:
        """Scan ``windows`` in order; False once the work budget is spent."""
        nonlocal spent
        for start, wl in windows:
            sm.set_seq1(text[start : start + wl])
            # Cheap upper bounds first — skip windows that cannot beat the bar.
            bar = max(best[0], floor)
            if sm.real_quick_ratio() < bar or sm.quick_ratio() < bar:
                continue
            r = sm.ratio()
            if r > best[0]:
                best[0], best[1], best[2] = r, start, wl
            spent += wl * n
            if spent >= MAX_RATIO_WORK:
                return False
        return True

    # Coarse pass: NO floor. Its best hit picks the refine neighbourhood, so letting
    # min_ratio change which coarse window wins could move the result.
    if _sweep(_coarse_windows(n, len(text)), 0.0):
        if best[0] == 0.0:
            return None  # no window shares anything with the quote
        # Refine around the best coarse hit, floored at min_ratio. EXACT: these
        # windows are fixed once the coarse hit is, and one bounded below min_ratio
        # can neither be returned nor — if it were the best — make the result
        # anything but None. The base64 blob's refine was nearly all such windows.
        _sweep(_refine_windows(n, len(text), best[1], best[2]), min_ratio)

    ratio, start, wl = best
    if ratio == 0.0 or ratio < min_ratio:
        return None
    return text[start : start + wl], ratio

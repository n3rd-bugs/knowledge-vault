"""kbcore.judge_fit — fit the pair-judge prompts to the judge's context window (KB 55589).

THE DEFECT (found 2026-10-27). The pair judges sent both items' whole title and body with
no token budget: :func:`kbcore.llm.items_contradict` (the nightly contradiction step and,
byte for byte, the precision gate's re-ask), :func:`kbcore.llm.evidence_side` (A, B and
one evidence item) and :func:`kbcore.twin_judge.same_fact_messages`. A pair of rm3 spec
documents (16,298 cl100k tokens; 17,060 in the seat's tokenizer) overflowed the context:
Ollama kept the first 4 tokens and the last 8,190, the system prompt was lost, and the
blind reply was memorized as "no conflict" for good (``contradiction_judgements``).

THE THRESHOLD (corrects the premise in :mod:`kbworker.prep` and KB 55031). Ollama v0.40
truncates only a prompt longer than num_ctx - 1 tokens, and then cuts it TO num_ctx/2
(8,194 at 16384). A judge prompt may therefore use the window minus its REPLY — and the
seats think before they answer (600-3,231 hidden reply tokens measured; at a 2,048 reserve
one reply ran into num_ctx and came back empty): :func:`judge_ceiling` is (num_ctx -
:data:`JUDGE_RESERVE_TOKENS`) / 1.15 cl100k tokens (8,904 at 16384), 1.15 being the drain's
cl100k-vs-model tokenizer margin.

THE RULE. A prompt is rendered exactly as before, and at or under the ceiling it is sent
BYTE-IDENTICAL: no verdict drift for the ~97% of pairs that fit. Over it, each body keeps
its head and ends with an ``[... N tokens omitted ...]`` marker, the room water-filled so
a short body stays whole and the long ones share the rest; titles, labels, fences and
the system prompt are never cut. Each fitted prompt logs one WARNING (labelled by the
items' titles) and counts in :data:`FIT_STATS` for the nightly report. Callers keep
drawing their seed from the ORIGINAL texts, so a pair's draw does not move with the
budget, and the fit itself is deterministic.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from kbcore.config import settings
from kbcore.llm_prompts import CONTRADICT_SYSTEM, EVIDENCE_SYSTEM

logger = logging.getLogger("kbcore.judge_fit")

#: cl100k vs the judge's own tokenizer: :data:`kbworker.prep.TOKENIZER_SAFETY`'s margin
#: (measured for qwen3; 1.047 for the bonsai seat on the rm3 specs), restated because
#: kbcore never imports kbworker.
TOKENIZER_SAFETY = 1.15
#: Model tokens kept free inside num_ctx for the reply, its hidden think block and the chat
#: template. The seats think by default: measured 2026-10-27 on bonsai-2:27b with ~13k-token
#: fitted prompts, replies took 600-3,231 tokens and one hit num_ctx (done_reason=length,
#: empty reply — no verdict) at a 2,048 reserve. 6,144 leaves ~6.9k at the seat's 1.06 ratio.
JUDGE_RESERVE_TOKENS = 6144
#: Every ``contradiction_judgements`` row checked before this instant was asked UNFITTED at
#: num_ctx 16384 (explicit since 2026-07-18): over :data:`LEGACY_CEILING` its "no" may be a
#: blind reply. Rows from the first fitted nightly on were asked the fitted way.
FIT_EPOCH = datetime(2026, 10, 27, 20, 0, tzinfo=UTC)
MARKER = "\n[... {n} tokens omitted ...]"
_MARKER_ROOM = 16  # tokens set aside per cut body for its marker; the re-count is the check
_PASSES = 8  # re-measure rounds: boundary merges can cost a token or two per cut

#: Prompts fitted since the last reset, by judge kind — the nightly report's counters.
FIT_STATS: dict[str, int] = {"contradict": 0, "evidence": 0, "same_fact": 0}

Render = Callable[[Sequence[str]], str]


def judge_ceiling(num_ctx: int | None = None) -> int:
    """cl100k tokens one whole judge prompt (system + user text) may carry: ``num_ctx`` or,
    by default, ``settings.kb_llm_num_ctx`` read at call time — the window every
    ``llm._chat`` call requests. Floored so a tiny test window still leaves room."""
    window = int(settings.kb_llm_num_ctx if num_ctx is None else num_ctx)
    return max(1024, int((window - JUDGE_RESERVE_TOKENS) / TOKENIZER_SAFETY))


#: Over this many cl100k tokens, a "no" memorized before :data:`FIT_EPOCH` may be blind — the
#: prompt truncated (past ~14k) or the thinking reply cut short by num_ctx. FROZEN: it is
#: judge_ceiling(16384) as decided 2026-10-27, (16384 - 6144) / 1.15, the window every
#: pre-fit verdict was judged in; a later reserve or num_ctx change must not move it.
LEGACY_CEILING = 8_904


def prompt_tokens(system: str, user: str) -> int:
    """The cl100k count of a two-message prompt, each message measured as sent."""
    from kbcore.chunk import count_tokens  # lazy: tiktoken loads on the first judge call

    return count_tokens(system) + count_tokens(user)


def shares(sizes: Sequence[int], room: int) -> list[int]:
    """Water-fill ``room`` tokens over bodies of ``sizes``: a body that fits its fair share
    is kept whole and what it leaves over goes to the longer ones."""
    out = [0] * len(sizes)
    left = max(0, room)
    order = sorted(range(len(sizes)), key=lambda i: sizes[i])
    for k, i in enumerate(order):
        out[i] = min(sizes[i], left // (len(order) - k))
        left -= out[i]
    return out


def _cut(tokens: list[int], keep: int) -> str:
    from kbcore.chunk import _enc

    # A token prefix is a byte prefix of the valid original: at most its last character
    # is incomplete, and "ignore" drops exactly that (no U+FFFD reaches the judge).
    head = _enc.decode_bytes(tokens[:keep]).decode("utf-8", "ignore")
    return head + MARKER.format(n=len(tokens) - keep)


def fit(kind: str, system: str, render: Render, bodies: Sequence[str], *, what: str) -> list[str]:
    """``bodies`` such that ``render(bodies)`` fits under :func:`judge_ceiling` beside
    ``system``: the originals when they already fit, else heads plus markers (module doc).
    ``kind`` keys :data:`FIT_STATS`; ``what`` labels the log line. Total: never raises on
    content — a frame that alone exceeds the ceiling is sent with every body cut and says
    so in the log."""
    ceiling = judge_ceiling()
    before = prompt_tokens(system, render(bodies))
    if before <= ceiling:
        return list(bodies)
    from kbcore.chunk import _enc

    toks = [_enc.encode(b, disallowed_special=()) for b in bodies]
    frame = prompt_tokens(system, render([""] * len(bodies)))
    keep = shares([len(t) for t in toks], ceiling - frame - _MARKER_ROOM * len(bodies))
    fitted, after = list(bodies), before
    for _ in range(_PASSES):
        cuts = zip(bodies, toks, keep, strict=True)
        fitted = [b if k >= len(t) else _cut(t, k) for b, t, k in cuts]
        after = prompt_tokens(system, render(fitted))
        if after <= ceiling:
            break
        worst = max(range(len(keep)), key=lambda i: keep[i])
        keep[worst] = max(0, keep[worst] - (after - ceiling))
    FIT_STATS[kind] = FIT_STATS.get(kind, 0) + 1
    logger.warning(
        "judge prompt fitted (%s %s): %d -> %d cl100k tokens, ceiling %d (num_ctx %d)%s",
        kind,
        what,
        before,
        after,
        ceiling,
        settings.kb_llm_num_ctx,
        "" if after <= ceiling else " - STILL OVER: the frame alone exceeds the ceiling",
    )
    return fitted


def label(*titles: str) -> str:
    """The log label of a fitted prompt: its items' titles, clipped and repr-quoted (item
    text is untrusted — no raw newline or control character reaches the journal)."""
    return " / ".join(repr(t[:48]) for t in titles)


def contradict_user(a_title: str, a_body: str, b_title: str, b_body: str) -> str:
    """The contradiction request's user text, unfitted."""
    return f"Item A: {a_title}\n{a_body}\n\nItem B: {b_title}\n{b_body}"


def contradict_tokens(a_title: str, a_body: str, b_title: str, b_body: str) -> int:
    """cl100k size of the UNFITTED contradiction prompt — what a pre-:data:`FIT_EPOCH`
    verdict was given on (the re-judge repair and the bench labels measure with it)."""
    return prompt_tokens(CONTRADICT_SYSTEM, contradict_user(a_title, a_body, b_title, b_body))


def contradict_messages(a_title: str, a_body: str, b_title: str, b_body: str) -> list[dict]:
    """THE contradiction request: :func:`kbcore.llm.items_contradict` sends it, the
    precision gate asks through that function, and the seat bench re-asks it verbatim."""

    def render(bodies: Sequence[str]) -> str:
        return contradict_user(a_title, bodies[0], b_title, bodies[1])

    what = label(a_title, b_title)
    fitted = fit("contradict", CONTRADICT_SYSTEM, render, [a_body, b_body], what=what)
    return [
        {"role": "system", "content": CONTRADICT_SYSTEM},
        {"role": "user", "content": render(fitted)},
    ]


def evidence_messages(
    a_title: str, a_body: str, b_title: str, b_body: str, c_title: str, c_body: str
) -> list[dict]:
    """:func:`kbcore.llm.evidence_side`'s request: the pair, then the evidence item."""

    def render(bodies: Sequence[str]) -> str:
        return (
            f"Item A: {a_title}\n{bodies[0]}\n\n"
            f"Item B: {b_title}\n{bodies[1]}\n\n"
            f"Evidence: {c_title}\n{bodies[2]}"
        )

    what = label(a_title, b_title, c_title)
    fitted = fit("evidence", EVIDENCE_SYSTEM, render, [a_body, b_body, c_body], what=what)
    return [
        {"role": "system", "content": EVIDENCE_SYSTEM},
        {"role": "user", "content": render(fitted)},
    ]

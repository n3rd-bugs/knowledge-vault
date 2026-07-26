"""kbcore.quote_modality — the MODALITY gate on a grounding quote (audit 2026-07-26).

SEC-3's quote gate proves a quote is a verbatim SUBSTRING of the transcript. It
says nothing about the quote's MOOD, and that is the hole the code-grounded audit
of 30 machine-extracted items found: a session's PLAN ("Ensure the hook returns a
JSON response", "**Expected Results**: …", "execute the following steps") grounds
PERFECTLY and was then stored as established fact. Confirmed damage — item 1073
asserted a ``{"status":"ok","queued":true}`` response contract that exists in NO
code path, and the nightly consolidation restated it as "Testing confirmed".

:func:`is_aspirational` returns a SHORT REASON when the quote reads as intent
(imperative / future / modal / plan heading) rather than observation; the caller
(kbworker.sessions.gate_quotes) then rejects the item exactly like an ungrounded
one, with the reason recorded in the review-queue detail so the rejection is
auditable.

CONSERVATIVE BY CONSTRUCTION: a quote that also carries observed-output evidence
(shell prompt, log line, exit code, ``HTTP/`` status, or a JSON body beside a
command — kbcore.evidence_signals) is treated as observational and returns None.
A wrongly-kept item is cheap; a wrongly-dropped insight is not. Pure stdlib.
"""

from __future__ import annotations

import re

from kbcore.evidence_signals import observed_evidence

# Leading imperatives: an instruction, not a finding. Anchored at the start of a
# LINE (optionally behind a bullet / ordinal / bold marker) so "we ensure the pool
# stays at 8" mid-sentence is not swept up.
_IMPERATIVE = re.compile(
    r"(?m)^[ \t]*(?:[-*+>][ \t]+|\d+[.)][ \t]+|\*\*[ \t]*)?"
    r"(?P<hit>ensure|verify\s+that|make\s+sure|execute\s+the\s+following"
    r"|run\s+the\s+following|confirm\s+that|check\s+that)\b",
    re.IGNORECASE,
)

# Future / modal / to-do phrasing anywhere in the quote. The colon-terminated
# markers ("to do:", "plan:") are separate alternatives because a trailing \b
# after ':' would not match at end-of-quote.
_FUTURE_MODAL = re.compile(
    r"\b(?:we\s+will|we\s+should|should\s+return|should\s+be|will\s+be|would\s+be"
    r"|expected\s+results?|expected\s+output|next\s+steps?|todo)\b"
    r"|\bto\s+do:"
    r"|\bplan:",
    re.IGNORECASE,
)

# Heading-ish plan markers: "## Plan", "### Steps", "**Expected Results**".
_PLAN_HEADING = re.compile(
    r"(?m)^[ \t]*#{1,6}[ \t]*(?:plan|steps?|next\s+steps?|expected\s+results?|todo)\b"
    r"|\*\*[ \t]*(?:plan|steps?|next\s+steps?|expected\s+results?|expected\s+output|todo)"
    r"[ \t]*:?[ \t]*\*\*",
    re.IGNORECASE,
)

_MAX_HIT = 48  # reasons ride into review_queue.detail; keep them short


def _short(hit: str) -> str:
    """Collapse whitespace and cap the echoed fragment (detail stays small)."""
    flat = " ".join(hit.split())
    return flat if len(flat) <= _MAX_HIT else flat[: _MAX_HIT - 1] + "…"


def is_aspirational(quote: str) -> str | None:
    """Reason the quote states INTENT rather than observation, or None.

    Returns e.g. ``"future/modal phrasing: 'expected results'"``. None means the
    quote is usable as grounding: either it carries no plan marker at all, or it
    carries observed-output evidence, which always wins (see the module docstring
    — the gate errs toward keeping).
    """
    if not quote or not quote.strip():
        return None
    if observed_evidence(quote) is not None:
        return None
    m = _PLAN_HEADING.search(quote)
    if m:
        return f"plan heading: {_short(m.group(0))!r}"
    m = _IMPERATIVE.search(quote)
    if m:
        return f"leading imperative: {_short(m.group('hit'))!r}"
    m = _FUTURE_MODAL.search(quote)
    if m:
        return f"future/modal phrasing: {_short(m.group(0))!r}"
    return None

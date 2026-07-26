"""kbcore.session_noise — skip minting pure session receipts (audit 2026-07-26).

Some machine-extracted items in the audited sample carry no knowledge at all:
Claude Code's IDE telemetry line ("The user opened <file> … this may or may not
be related to the current task"), a bare counter snapshot from a flush log
("items: 3 chunks: 12"), and contentless receipts ("the smoke test passed") that
name no command, no threshold and no failure signature. Each one is verbatim-
grounded, so the SEC-3 gate passes it, and each one then competes for retrieval
slots and feeds consolidation.

:func:`noise_reason` returns a short reason for the three shapes above, and the
caller skips minting the item (counted in the review detail — an invisible drop
would be worse than the noise). The filter is deliberately TIGHT: every rule
requires a distinctive marker AND the absence of any evidence signal
(kbcore.evidence_signals), so it favours FALSE NEGATIVES. A wrongly-kept item is
cheap; a wrongly-dropped insight is not.
"""

from __future__ import annotations

import re

from kbcore.evidence_signals import has_command, has_failure_signature, has_measurement

# 1. IDE telemetry. "may or may not be related/relevant" is Claude Code's own
#    hedge boilerplate — it does not occur in genuine engineering prose.
_IDE_TELEMETRY = re.compile(r"may\s+or\s+may\s+not\s+be\s+(?:related|relevant)", re.IGNORECASE)

# 2. Counter snapshot: the WHOLE body is a run of `name: 12` / `name=12` counters
#    and nothing else. Deliberately strict (full match, >= 2 pairs, plain
#    non-negative integers): "Postgres 16: max_connections = 100" and
#    "num_ctx=16384 keep_alive=-1" are configuration KNOWLEDGE and must survive,
#    while "items: 3 chunks: 12" — a flush-log receipt — must not be minted.
_COUNTER_RUN = re.compile(r"(?:[\w.-]+[ \t]*[:=][ \t]*\d[\d,_]*[ \t,;|]*){2,}")
_MAX_SNAPSHOT_CHARS = 400

# 3. Contentless receipt: "<thing> passed/worked", short, with no evidence.
_RECEIPT = re.compile(
    r"\b(?:smoke\s+test|tests?|test\s+suite|it|everything|all\s+of\s+it|the\s+fix|"
    r"the\s+change|the\s+hook|the\s+run|the\s+deploy|the\s+build)\b"
    r"[^.!?\n]{0,40}?"
    r"\b(?:passed|passes|worked|works|working|succeeded|successful|green|ok|fine)\b",
    re.IGNORECASE,
)
_MAX_RECEIPT_CHARS = 200
_PATHISH = re.compile(r"(?<![\w/])(?:\.{0,2}/[\w./-]+|~/[\w./-]+|\b[\w-]+\.[a-z]{1,4}\b)")


def noise_reason(title: str, body: str) -> str | None:
    """Short reason this item is pure session receipt/telemetry, else None.

    ``title`` is used only to size the receipt rule's content budget; the three
    rules all key off ``body``, which is what would be stored and retrieved.
    """
    text = (body or "").strip()
    if not text:
        return None  # empty bodies are dropped by the caller already

    if _IDE_TELEMETRY.search(text):
        return "IDE telemetry line"

    if len(text) <= _MAX_SNAPSHOT_CHARS and _COUNTER_RUN.fullmatch(" ".join(text.split())):
        return "counter snapshot"

    if (
        len(text) <= _MAX_RECEIPT_CHARS
        and _RECEIPT.search(text)
        and not has_command(text)
        and not has_measurement(text)
        and not has_failure_signature(text)
        and not _PATHISH.search(text)
    ):
        return "contentless pass/fail receipt"

    return None

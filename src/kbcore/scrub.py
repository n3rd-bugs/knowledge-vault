"""kbcore.scrub — secret redaction on every ingest (SEC-2, PLAN.md §5.3).

Matches are replaced with ``[REDACTED:{label}]`` and counted per label so the
item carries ``meta.redactions`` and can be flagged in review.
"""

from __future__ import annotations

import re

# Order matters: specific token formats first, the generic assignment last so
# e.g. `token=ghp_...` redacts as a github token, not a generic credential —
# the credential value pattern therefore refuses to match an existing
# [REDACTED:...] marker (review finding: it used to re-redact and double-count).
# The private-key body is bounded to keep the lazy scan linear-ish on
# pathological BEGIN-without-END input (ReDoS finding).
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,20000}?-----END [A-Z ]*PRIVATE KEY-----"
        ),
    ),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"ghp_[A-Za-z0-9]{36}")),
    ("api_key", re.compile(r"sk-[A-Za-z0-9\-_]{20,}")),
    ("jwt", re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")),
    (
        "credential",
        re.compile(r"(?i)(?:password|passwd|secret|token)\s*[=:]\s*(?!\[REDACTED:)\S{6,}"),
    ),
]


def scrub(text: str) -> tuple[str, dict[str, int]]:
    """Redact secrets from *text*.

    Returns ``(clean_text, redactions)`` where redactions maps label -> count.
    Runs on every ingest path before anything is stored (SEC-2).
    """
    redactions: dict[str, int] = {}
    # The KB's OWN bearer, wherever it appears (bare, in a config snippet, in a
    # flushed transcript of a kb_setup conversation) — the generic token= pattern
    # only catches prefixed forms. Exact match, no regex risk.
    from kbcore.config import settings  # local import — scrub stays dependency-light

    own = settings.kb_token
    if own and len(own) >= 16 and own in text:
        n = text.count(own)
        text = text.replace(own, "[REDACTED:kb_token]")
        redactions["kb_token"] = redactions.get("kb_token", 0) + n
    for label, pattern in SECRET_PATTERNS:
        text, n = pattern.subn(f"[REDACTED:{label}]", text)
        if n:
            redactions[label] = redactions.get(label, 0) + n
    return text, redactions

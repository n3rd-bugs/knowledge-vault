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


def merge_redactions(dst: dict[str, int], src: dict[str, int]) -> None:
    """Fold scrub counts from ``src`` into ``dst`` (accumulate per label).

    Lets the ingest chokepoint accumulate redaction counts across every scrubbed
    field (title, body, uri, tags, code_ref, meta) into one ``meta.redactions``.
    """
    for label, count in src.items():
        dst[label] = dst.get(label, 0) + count


def scrub_tree(value: object, redactions: dict[str, int]) -> object:
    """Scrub every STRING leaf of a JSON-ish value (str / list / dict), folding
    redaction counts into ``redactions``; non-string leaves pass through.

    A11-3 finding 24 (the kbcore chokepoint, Invariant 2): the ingest path used to
    scrub only title/body, so a secret pasted into a uri, a tag, or a code_ref/meta
    string stored unredacted and rendered live in the owner's item/review views.
    Recursing here lets ingest_in redact all of them at the single write point.
    Deterministic ⇒ idempotent (a re-scrub of already-redacted text is a no-op).
    """
    if isinstance(value, str):
        cleaned, red = scrub(value)
        merge_redactions(redactions, red)
        return cleaned
    if isinstance(value, dict):
        return {k: scrub_tree(v, redactions) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_tree(v, redactions) for v in value]
    return value


def scrub(text: str) -> tuple[str, dict[str, int]]:
    """Redact secrets from *text*.

    Returns ``(clean_text, redactions)`` where redactions maps label -> count.
    Runs on every ingest path before anything is stored (SEC-2).
    """
    redactions: dict[str, int] = {}
    # Postgres TEXT cannot store NUL (\x00); a transcript message carrying a
    # literal  (json.loads decodes it to a real NUL) would otherwise crash
    # the enqueue INSERT and — on the LOCAL path — wedge behind an advanced
    # checkpoint. Strip NUL before anything else. (Counted so it's visible.)
    if "\x00" in text:
        redactions["nul_stripped"] = text.count("\x00")
        text = text.replace("\x00", "")
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

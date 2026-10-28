"""kbcore.logsafe — redact secrets from every log record before a handler sees it.

The served processes log at INFO, and libraries log what they touch: httpx writes
one INFO line per request WITH the full URL, and FastMCP's Google verifier calls
``tokeninfo?access_token=<the caller's Google access token>``. Until 2026-10-28
kb-mcp-public therefore wrote a live Google bearer into journald on every
verify-cache miss (48 lines over one container's lifetime). Lowering httpx's level
would hide that one line; the next library — or the next URL with a secret in it —
leaks the same way. So the fix is ONE funnel: a LogRecord factory that passes each
record through :func:`kbcore.scrub.scrub`, the same shape patterns and this
deployment's own secret values that guard ingest (CLAUDE.md rule 12), plus the
ntfy topic (a capability URL that scrub's credential-field walk does not cover).

A factory, not a handler filter: uvicorn and FastMCP configure their own handlers
(and uvicorn's loggers do not propagate), so a filter on the root handler would
miss them. Every record is created through the factory, whoever handles it.
Installed once per process by each server's ``main()``.
"""

from __future__ import annotations

import logging

from kbcore import scrub

# Records whose formatter unpacks ``record.args`` positionally (uvicorn's
# AccessFormatter takes a 5-tuple apart), so they are redacted argument by argument
# instead of being collapsed into one pre-formatted string.
_POSITIONAL_ARGS_LOGGERS = frozenset({"uvicorn.access"})
# Same floor as scrub's own-secret walk: a short topic is more likely a word.
_MIN_TOPIC_LEN = 12
_WITHHELD = "[log record withheld: redaction failed (%s)]"

_installed = False


def redact(text: str) -> str:
    """``text`` with every secret scrub knows about, and the ntfy topic, removed."""
    from kbcore.config import settings

    clean, _ = scrub.scrub(text)
    topic = (settings.kb_notify_topic or "").strip()
    if len(topic) >= _MIN_TOPIC_LEN and topic in clean:
        clean = clean.replace(topic, "[REDACTED:kb_notify_topic]")
    return clean


def _redact_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else str(value)
    clean = redact(text)
    return value if clean == text else clean


def _redact_record(record: logging.LogRecord) -> None:
    try:
        full = record.getMessage()
    except Exception:  # noqa: BLE001 — a malformed record is logging's own error to report
        return
    if redact(full) == full:
        return  # the common case: nothing to hide, the record is left untouched
    if record.name in _POSITIONAL_ARGS_LOGGERS and isinstance(record.args, tuple):
        record.args = tuple(_redact_value(a) for a in record.args)
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return
    record.msg = redact(full)
    record.args = None


def install() -> None:
    """Route every LogRecord this process creates through :func:`redact`. Idempotent."""
    global _installed
    if _installed:
        return
    base = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = base(*args, **kwargs)
        try:
            _redact_record(record)
        except Exception as e:  # noqa: BLE001 — fail CLOSED: withhold, never leak
            record.msg, record.args = _WITHHELD % type(e).__name__, None
        return record

    logging.setLogRecordFactory(factory)
    _installed = True

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
ntfy topic (a capability URL that scrub's credential-field walk does not cover) and
credential-bearing query parameters (an OAuth callback's ``code=``).

A factory, not a handler filter: uvicorn and FastMCP configure their own handlers
(and uvicorn's loggers do not propagate), so a filter on the root handler would
miss them. Every record is created through the factory, whoever handles it.

The work per record is BOUNDED and linear: uvicorn's access record carries the
request path and query of every request — text anyone can send, up to h11's 16 KiB
— and this runs on the event loop. A long record is scanned to a fixed margin past
its cap and the REDACTED text is then cut, so a secret crossing the cut is still
seen whole and nothing past the cap is emitted at all.
Installed once per process by each server's ``main()``.
"""

from __future__ import annotations

import logging
import re
import traceback
from collections.abc import Mapping

from kbcore import scrub

# Records whose formatter unpacks ``record.args`` positionally (uvicorn's
# AccessFormatter takes a 5-tuple apart), so they are redacted argument by argument
# instead of being collapsed into one pre-formatted string.
_POSITIONAL_ARGS_LOGGERS = frozenset({"uvicorn.access"})
_MAX_MESSAGE = 8192  # chars of one record that are emitted
_MAX_ACCESS = 2048  # ...of one uvicorn access line (a URL longer than this is not diagnostics)
_MAX_TRACEBACK = 16384  # head and tail of a traceback (its last line names the error)
# Scanned PAST every cut, then the redacted text is cut: a secret that crosses a
# cut is seen whole (a Google access token is at most 2048 bytes; own secrets and
# JWTs are far shorter) instead of losing the prefix or the length its pattern needs.
_MARGIN = 4096
# Log-only, linear pre-pass for private keys, run BEFORE scrub. scrub's private_key
# rule (kept unchanged for ingest) rescans up to 20000 chars from EVERY BEGIN header
# — ~60 ms on a 16 KiB traceback full of them, holding the GIL — and needs the END
# line, which a cut may have dropped. This one stops at the next BEGIN or at the end
# of the scanned text, and redacts an unterminated key up to there (fail closed).
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:(?!-----BEGIN )[\s\S])*?"
    r"(?:-----END [A-Z ]*PRIVATE KEY-----|(?=-----BEGIN )|\Z)"
)
# ...and, for a TAIL window, a key whose BEGIN line lies before the window: the
# body and END it starts with carry no header for either rule to anchor on.
_PRIVATE_KEY_TAIL = re.compile(r"\A(?:(?!-----BEGIN )[\s\S])*?-----END [A-Z ]*PRIVATE KEY-----")
# Same floor as scrub's own-secret walk: a short topic is more likely a word.
_MIN_TOPIC_LEN = 12
# Query parameters that carry a credential in URLs this system logs: an OAuth
# callback's authorization code, and tokens/keys a client or library put in a URL.
_SECRET_QUERY = re.compile(
    r"(?i)([?&](?:code|access_token|id_token|refresh_token|client_secret|token|"
    r"password|key|api_key|apikey)=)(?!\[REDACTED:)[^&\s\"'#]+"
)
_WITHHELD = "[log record withheld: redaction failed (%s)]"

_installed = False


def redact(text: str) -> str:
    """``text`` with every secret scrub knows about, credential query parameters,
    and the ntfy topic removed."""
    from kbcore.config import settings

    # NULs first: scrub strips them anyway, and a NUL inside a key header would hide
    # it from the linear pre-pass and hand it back to scrub's costly rule.
    text = text.replace("\x00", "")
    clean, _ = scrub.scrub(_PRIVATE_KEY.sub("[REDACTED:private_key]", text))
    clean = _SECRET_QUERY.sub(r"\1[REDACTED:query]", clean)
    # Normalised exactly as kbcore.notify builds the push URL.
    topic = (settings.kb_notify_topic or "").strip().strip("/")
    if len(topic) >= _MIN_TOPIC_LEN and topic in clean:
        clean = clean.replace(topic, "[REDACTED:kb_notify_topic]")
    return clean


def _dropped(n: int) -> str:
    return f"…[{n} chars dropped by kbcore.logsafe]"


def _first(text: str, keep: int) -> str:
    """The first ``keep`` chars of ``text`` once redacted. When the scanned window
    itself had to be cut, its far ``_MARGIN`` chars are context only — a secret can
    straddle that edge, and redactions earlier in the window may have pulled the
    edge forward into what is kept — so they are dropped before the final cut."""
    if len(text) <= keep + _MARGIN:
        return redact(text)[:keep]
    clean = redact(text[: keep + _MARGIN])
    return clean[: min(keep, max(0, len(clean) - _MARGIN))]


def _last(text: str, keep: int) -> str:
    """The last ``keep`` chars of ``text`` once redacted (mirror of :func:`_first`)."""
    if len(text) <= keep + _MARGIN:
        return redact(text)[-keep:]
    window = text[-(keep + _MARGIN) :]
    clean = redact(_PRIVATE_KEY_TAIL.sub("[REDACTED:private_key]", window))
    clean = clean[_MARGIN:] if len(clean) > _MARGIN else ""
    return clean[-keep:]


def _head(text: str, limit: int) -> str:
    """``text`` redacted and cut to its first ``limit`` chars (marked when cut)."""
    if len(text) <= limit:
        return redact(text)
    return _first(text, limit) + _dropped(len(text) - limit)


def _ends(text: str, limit: int) -> str:
    """``text`` redacted and cut to its first and last ``limit // 2`` chars."""
    if len(text) <= limit:
        return redact(text)
    half = limit // 2
    return f"{_first(text, half)}\n{_dropped(len(text) - limit)}…\n{_last(text, half)}"


def _redact_value(value: object, limit: int) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else str(value)
    clean = _head(text, limit)
    return value if clean == text else clean


def _redact_parts(record: logging.LogRecord, limit: int) -> None:
    """Redact the format string and each argument separately, keeping their shape."""
    if isinstance(record.msg, str):
        record.msg = _head(record.msg, limit)
    if isinstance(record.args, tuple):
        record.args = tuple(_redact_value(a, limit) for a in record.args)
    elif isinstance(record.args, Mapping):
        record.args = {k: _redact_value(v, limit) for k, v in record.args.items()}


def _redact_exception(record: logging.LogRecord) -> None:
    """Tracebacks are rendered at emit time from exc_info, and their last line
    repeats str(exc) — scan them now. A changed traceback replaces exc_info with
    the redacted exc_text, which formatters print as-is (one that renders exc_info
    itself, like rich's, can then no longer print the raw one)."""
    if record.exc_info and not record.exc_text:
        text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
        clean = _ends(text, _MAX_TRACEBACK)
        if clean != text:
            record.exc_text, record.exc_info = clean, None
    if record.stack_info:
        record.stack_info = _ends(record.stack_info, _MAX_TRACEBACK)


def _redact_record(record: logging.LogRecord) -> None:
    _redact_exception(record)
    positional = record.name in _POSITIONAL_ARGS_LOGGERS and isinstance(record.args, tuple)
    limit = _MAX_ACCESS if positional else _MAX_MESSAGE
    try:
        full = record.getMessage()
    except Exception:  # noqa: BLE001 — logging reports a bad call itself, printing msg AND args
        _redact_parts(record, limit)
        return
    clean = _head(full, limit)
    if clean == full:
        return  # the common case: nothing to hide and nothing to cut, the record is untouched
    if positional:
        _redact_parts(record, limit)  # keep the tuple its formatter unpacks
        return
    record.msg, record.args = clean, None


def _withhold(record: logging.LogRecord, why: Exception, msg, args) -> None:
    record.exc_info = record.exc_text = record.stack_info = None
    if record.name in _POSITIONAL_ARGS_LOGGERS and isinstance(args, tuple):
        # Keep the arity its formatter unpacks (and the int status it int()s); the
        # format string is the logger's own constant, never request text.
        record.msg = msg
        record.args = tuple(a if isinstance(a, int) else "[withheld]" for a in args)
        return
    record.msg, record.args = _WITHHELD % type(why).__name__, None


def install() -> None:
    """Route every LogRecord this process creates through :func:`redact`. Idempotent."""
    global _installed
    if _installed:
        return
    base = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = base(*args, **kwargs)
        msg, record_args = record.msg, record.args
        try:
            _redact_record(record)
        except Exception as e:  # noqa: BLE001 — fail CLOSED: withhold, never leak
            _withhold(record, e, msg, record_args)
        return record

    logging.setLogRecordFactory(factory)
    _installed = True

"""kbcore.evidence_signals — "was this OBSERVED?" primitives (audit 2026-07-26).

The code-grounded audit of 30 machine-extracted items found two distinct defects
that need the SAME judgement call: a grounding quote may be a session's PLAN
rather than an observation (kbcore.quote_modality), and a minted body may be a
contentless receipt rather than knowledge (kbcore.session_noise). Both hinge on
one question — does this text carry evidence that something actually RAN?

Defining that once, here, keeps the two gates from drifting apart, and keeps both
of them CONSERVATIVE in the same direction: every signal below makes a gate LESS
likely to drop an item (observed evidence => keep). A wrongly-kept item is cheap;
a wrongly-dropped insight is not. Pure stdlib — no LLM, no DB, no I/O.
"""

from __future__ import annotations

import re

# A shell prompt at the start of a line, with or without a `user@host:~/dir` lead
# ("$ ./hook.sh"). '#' is deliberately NOT a prompt character here: '## Plan' is a
# markdown heading, and treating it as a root prompt would blind the plan gate.
_SHELL_PROMPT = re.compile(r"(?m)^[ \t]*(?:[\w.@:~/\\-]+[ \t]*)?\$[ \t]+\S")

# A log line: an ISO date / clock stamp or a bracketed level at line start, or a
# Python traceback banner anywhere.
_LOG_LINE = re.compile(
    r"(?m)^[ \t]*\[?(?:\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}"
    r"|(?:ERROR|WARN|WARNING|INFO|DEBUG|FATAL|TRACE|CRITICAL)\b)"
    r"|Traceback \(most recent call last\)",
)

# An exit/return code readout, or a bare `$?`.
_EXIT_CODE = re.compile(
    r"\bexit(?:ed with)?\s+(?:code|status)\s*[:=]?\s*\d+"
    r"|\breturn(?:ed|s)?\s+(?:code|status)\s*[:=]?\s*\d+"
    r"|\$\?",
    re.IGNORECASE,
)

# An HTTP status line ("HTTP/1.1 200 OK") — protocol output, never a plan.
_HTTP_STATUS = re.compile(r"\bHTTP/\d")

# A JSON object with at least one quoted key. Alone this is NOT evidence: a plan
# quotes the response contract it INTENDS ({"status":"ok"}); only a JSON body
# sitting next to a command counts (see :func:`observed_evidence`).
_JSON_OBJECT = re.compile(r"\{[^{}]*[\"'][\w.-]+[\"'][ \t]*:")

# A command invocation: a prompt line, a `./script` path, or a known CLI verb
# followed by an argument-shaped token.
_COMMAND = re.compile(
    r"(?m)^[ \t]*\$[ \t]+\S"
    r"|(?<![\w/])\./[\w./-]+"
    r"|\b(?:curl|wget|git|docker|docker[ \t]+compose|make|npm|pnpm|yarn|pytest|uv|"
    r"python3?|systemctl|journalctl|psql|kubectl|ssh|scp|rsync|bash|sh|jq|kb)"
    r"[ \t]+[-\w./]",
)

# A measurement / threshold: a comparison against a number, a number with a unit,
# or any multi-digit run (a real threshold is essentially never a single digit).
_MEASUREMENT = re.compile(
    r"[<>]=?[ \t]*\d"
    r"|\b\d+(?:\.\d+)?[ \t]*(?:%|ms|s|sec|secs|seconds|m|min|h|hz|khz|mhz|"
    r"b|kb|mb|gb|tb|px|v|mv|a|ma|rpm)\b"
    r"|\b\d{2,}\b",
    re.IGNORECASE,
)

# A failure signature: the vocabulary of something that went wrong.
_FAILURE = re.compile(
    r"\b(?:fail|fails|failed|failure|failing|error|errors|traceback|exception|"
    r"timeout|timed[ \t]+out|refused|denied|panic|crash|crashed|abort|aborted|"
    r"regress|regression|broke|broken)\b",
    re.IGNORECASE,
)


def has_command(text: str) -> bool:
    """True when the text shows a command being invoked (prompt, ./script, CLI verb)."""
    return bool(_COMMAND.search(text))


def has_measurement(text: str) -> bool:
    """True when the text carries a threshold / measured quantity."""
    return bool(_MEASUREMENT.search(text))


def has_failure_signature(text: str) -> bool:
    """True when the text names a failure mode (error/timeout/traceback/…)."""
    return bool(_FAILURE.search(text))


def observed_evidence(text: str) -> str | None:
    """Short label for the observed-output evidence in ``text``, else None.

    Order is cheapest-and-most-specific first. A JSON object counts ONLY when a
    command appears beside it — a plan that states its intended response contract
    (``Expected Results: the hook returns {"status":"ok"}``) must NOT read as
    evidence, while a captured response next to the curl that produced it must.
    """
    if _SHELL_PROMPT.search(text):
        return "shell prompt"
    if _LOG_LINE.search(text):
        return "log line"
    if _EXIT_CODE.search(text):
        return "exit code"
    if _HTTP_STATUS.search(text):
        return "HTTP status line"
    if _JSON_OBJECT.search(text) and has_command(text):
        return "JSON output beside a command"
    return None

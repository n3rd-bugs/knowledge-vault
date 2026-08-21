"""kbcore.notify — best-effort push of the nightly digest to the owner's phone.

The nightly report is only useful if the owner actually reads it, and the whole
point of the reject-apply cooling-off (:mod:`kbcore.review_apply`) is that a
queued application appears in several digests BEFORE the machine acts — a
digest nobody sees turns that veto window into a formality. So the digest is
pushed to the same ntfy channel the session hooks already use
(``KB_NOTIFY_URL`` / ``KB_NOTIFY_TOPIC`` / ``KB_NOTIFY_TOKEN``, already in
config; no new settings, no new secret).

BEST EFFORT, ALWAYS. :func:`push_digest` returns False on ANY failure and never
raises: a dead tunnel, an unconfigured channel or an ntfy outage must never fail
a nightly consolidation that has already done its real work.

THE TOKEN IS NEVER LOGGED and never reaches argv. ``KB_NOTIFY_TOKEN`` is a
WRITE-ONLY, per-topic ntfy token, but it is still a credential distributed to
every enrolled machine, and on this system an ntfy admin password has already
leaked into a transcript through a ``curl -u`` command line (2026-07-31) — which
is why the shell hook feeds curl its credential on stdin. Here it rides an httpx
header (process memory, not ``/proc/<pid>/cmdline``), and the failure log carries
only the exception class plus a :mod:`kbcore.scrub`-redacted message, so a
credential embedded in the URL cannot ride an httpx error string into the journal.
"""

from __future__ import annotations

import logging

import httpx

from kbcore import scrub
from kbcore.config import settings

logger = logging.getLogger("kbcore.notify")

PUSH_TIMEOUT = 8.0  # seconds; matches the session hook's --max-time budget
TITLE = "KB nightly"
# ntfy renders the body as the message; a nightly digest is a handful of lines,
# and a runaway one must not become a megabyte POST.
MAX_BODY_CHARS = 3500


def push_digest(lines: list[str], *, title: str = TITLE, priority: str = "default") -> bool:
    """POST ``lines`` as one ntfy message. True iff delivered.

    Defaults are byte-identical to the historical behavior (title "KB nightly",
    priority "default") — the keyword overrides exist because a red-eval night
    used to arrive looking exactly like a healthy one (08-18/19: eval_regression
    true, yet the phone showed a normal 'KB nightly'; OnFailure never fires for
    a SUCCESSFUL unit run). The nightly passes ``title='KB ALERT'`` /
    ``priority='high'`` on red nights; the weekly passes ``title='KB weekly'``.

    Returns False WITHOUT contacting anything when the channel is not configured
    (empty ``kb_notify_url``/``kb_notify_topic`` — the documented "feature off"
    state) or when there is nothing to say. Never raises.
    """
    base = (settings.kb_notify_url or "").strip().rstrip("/")
    topic = (settings.kb_notify_topic or "").strip().strip("/")
    body = "\n".join(str(line) for line in lines if str(line).strip())
    if not (base and topic and body):
        return False
    headers = {"Title": title, "Priority": priority}
    if settings.kb_notify_token:
        headers["Authorization"] = f"Bearer {settings.kb_notify_token}"
    try:
        resp = httpx.post(
            f"{base}/{topic}",
            content=body[:MAX_BODY_CHARS].encode("utf-8"),
            headers=headers,
            timeout=PUSH_TIMEOUT,
        )
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001 — a push must never fail the nightly run
        redacted, _ = scrub.scrub(f"{e}")
        logger.warning("digest push failed (%s: %s)", e.__class__.__name__, redacted)
        return False
    return True

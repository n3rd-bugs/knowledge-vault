"""kbcore.setup_url — validate the rest_url kb_setup turns into a fetch-and-execute
install source (SEC, Wave-2 review #2, KB 16601).

The kb_setup bootstrap block does, verbatim:

    export KB_REST_URL=<rest_url>
    export KB_ENROL_CODE=<live kbe_ code>
    curl -H "Authorization: Bearer $KB_ENROL_CODE" "$KB_REST_URL/api/setup/install.sh" -o install.sh
    bash install.sh

so a caller-supplied ``rest_url`` decides WHERE a brand-new machine downloads and
then runs its installer, and under which host the live enrolment code is exported
as a bearer. Left unvalidated (the old code did only ``.strip()/.rstrip('/')``), a
foreign value would redirect that curl|bash at an attacker's host, and a
metacharacter-laden one could break out of the shell block.

This module is the single kbcore chokepoint that validates it BEFORE
:mod:`kbmcp.tools_setup` interpolates it — mirroring
``offsite_config._RCLONE_REMOTE_RE``'s tight-charset, validate-before-use rule
(Invariant 2: the guarantee lives in kbcore, never at the MCP tool).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from kbcore.config import settings
from kbcore.errors import InvalidOperation

# https:// + a bare host ([A-Za-z0-9.-], optional :port) + an EMPTY or SIMPLE path.
# The charset admits no space, quote, $, ;, |, &, backtick or newline — nothing
# that could break out of the bash block the URL is interpolated into. Same
# posture as offsite_config._RCLONE_REMOTE_RE (validate before any shell use).
_REST_URL_RE = re.compile(r"https://[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?(?:/[A-Za-z0-9._/-]*)?")


def _hosts_allowed() -> set[str]:
    """Hostnames a caller-supplied rest_url may use: the configured
    ``kb_public_rest_url`` host plus every host in ``KB_SETUP_ALLOWED_REST_HOSTS``."""
    hosts: set[str] = set()
    configured = (settings.kb_public_rest_url or "").strip()
    if configured:
        host = (urlsplit(configured).hostname or "").lower()
        if host:
            hosts.add(host)
    hosts |= {
        h.strip().lower() for h in settings.kb_setup_allowed_rest_hosts.split(",") if h.strip()
    }
    return hosts


def rest_url_to_render(rest_url: str | None) -> str:
    """The REST base tools_setup should interpolate, trailing slash trimmed.

    * No override -> the configured ``kb_public_rest_url`` (``''`` when unset; the
      caller then falls back to a labelled placeholder).
    * A MALFORMED override (non-https, a shell metacharacter, a fancy path) ->
      :class:`InvalidOperation`; it is NEVER interpolated.
    * A well-formed override whose host is neither the configured host nor in the
      allowlist -> IGNORED: the configured value is rendered instead, so a foreign
      host can never become the curl|bash install source.
    """
    configured = (settings.kb_public_rest_url or "").strip().rstrip("/")
    override = (rest_url or "").strip()
    if not override:
        return configured
    if not _REST_URL_RE.fullmatch(override):
        raise InvalidOperation(
            "rest_url must be https://<host>[/simple/path] with no shell "
            "metacharacters — it is fetched and executed at install time"
        )
    host = (urlsplit(override).hostname or "").lower()
    return override.rstrip("/") if host in _hosts_allowed() else configured

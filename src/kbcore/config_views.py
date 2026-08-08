"""kbcore.config_views — the DERIVED views over the raw ``.env`` fields.

Mechanical split from :mod:`kbcore.config` (2026-08-08), for the same reason
``consolidate`` shed ``prep``/``cohort``/``tail_ops``: that file sat one line
under its size-ratchet ceiling, so the next knob could not be added at all.
Nothing here reads the environment — every property is a pure function of the
fields declared on :class:`kbcore.config.Settings`, which mixes this in.

Config STILL flows through ``kbcore.config.settings`` only (CLAUDE.md hard
rule): this module declares no fields, is never instantiated on its own, and is
not an import target for the rest of the codebase.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


class SettingsViews:
    """Derived, read-only views mixed into ``Settings`` (declares NO fields).

    A plain mixin rather than a second ``BaseSettings``: pydantic collects fields
    from annotations, and there are none here, so the settings surface is exactly
    what ``config.py`` declares.
    """

    @property
    def db_url(self) -> str:
        """DB URL with ``KB_DB_PASSWORD`` injected.

        The `.env` template keeps the URL password-free (``postgresql://kb@...``)
        precisely so neither python-dotenv nor docker-compose ever interpolates
        the secret into it (unquoted specials would corrupt the URL). Cases
        handled here: a password-less URL gets ``kb_db_password`` injected,
        URL-quoted; a literal ``${KB_DB_PASSWORD}`` placeholder (env var set
        directly, PLAN.md §3 form) is replaced, quoted; a URL that already
        carries an inline password is respected as-is.
        """
        url = self.kb_db_url.replace("${KB_DB_PASSWORD}", quote(self.kb_db_password, safe=""))
        parts = urlsplit(url)
        if parts.username and not parts.password and self.kb_db_password:
            host = parts.hostname or ""
            port = f":{parts.port}" if parts.port else ""
            netloc = f"{parts.username}:{quote(self.kb_db_password, safe='')}@{host}{port}"
            url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
        return url

    @property
    def deep_model(self) -> str:
        """Model for deep search; falls back to the extractor model."""
        return self.kb_deep_model or self.kb_extract_model

    @property
    def inbox(self) -> Path:
        return self.kb_inbox.expanduser()

    @property
    def raw(self) -> Path:
        return self.kb_raw.expanduser()

    @property
    def backups(self) -> Path:
        return self.kb_backups.expanduser()

    @property
    def git_repos(self) -> list[Path]:
        """`KB_GIT_REPOS` parsed as a colon-separated list of repo paths."""
        return [Path(p.strip()).expanduser() for p in self.kb_git_repos.split(":") if p.strip()]

    @property
    def web_allowed_emails(self) -> list[str]:
        """`KB_WEB_ALLOWED_EMAILS` parsed as a lowercased comma-separated list."""
        return [e.strip().lower() for e in self.kb_web_allowed_emails.split(",") if e.strip()]

    @property
    def embed_url(self) -> str:
        """The embedder endpoint — its own host when set, else the main one."""
        return (self.kb_embed_url or self.ollama_url).rstrip("/")

    @property
    def extract_ensemble(self) -> list[str]:
        """`KB_EXTRACT_ENSEMBLE` parsed as a comma-separated list of extra models."""
        return [m.strip() for m in self.kb_extract_ensemble.split(",") if m.strip()]

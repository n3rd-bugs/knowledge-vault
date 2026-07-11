"""kbcore.config — single source of runtime configuration (PLAN.md §2/§3).

All configuration flows through `settings`. Nothing else in the codebase reads
`os.environ` or hardcodes paths, ports, model names, or secrets (CLAUDE.md hard
rule): import `settings` from here instead.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root is two levels up from src/kbcore/config.py.
_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Typed view over `.env` (see `.env.example` for the documented template)."""

    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    kb_db_url: str = "postgresql://kb@127.0.0.1:5433/kb"
    kb_db_password: str = "change-me"
    kb_db_schema: str = "public"  # tests point this at kb_test (CLAUDE.md §Code style)
    kb_token: str = "change-me-long-random"
    kb_bind: str = "127.0.0.1"
    kb_port: int = 8321
    kb_rest_port: int = 8322
    kb_web_port: int = 8323
    # Google sign-in for the read-only web UI (kbweb): comma-separated allowlist
    # of emails and the public base URL used to build the OAuth redirect_uri.
    kb_web_allowed_emails: str = ""
    kb_web_public_url: str = "https://web.n3rd-bugs.dev"
    # Containerized runs bind 0.0.0.0 (container-private, fronted by Traefik/TLS);
    # set true only there. The default still refuses accidental host-public binds.
    kb_allow_any_bind: bool = False
    # Public/cloud instance gate: when true, reads see only projects flagged
    # public and writes to non-public projects are rejected (no existence leak).
    kb_public_only: bool = False
    # OAuth (for claude.ai web/mobile connectors). When enabled the public MCP
    # instance accepts BOTH Google OAuth and the static bearer (MultiAuth).
    kb_oauth_enabled: bool = False
    kb_public_url: str = "https://kb.n3rd-bugs.dev"  # base URL for OAuth metadata/redirects
    # Public REST base (e.g. https://api.<host>), used by kb_setup to pre-fill the
    # REMOTE session-hook config so a new machine needs only a token. Empty ->
    # kb_setup emits a generic placeholder.
    kb_public_rest_url: str = ""
    # Lifetime of the FastMCP-issued access token. Decoupled from Google's ~1h
    # upstream expiry so a client hiccup doesn't surface as an hourly logout;
    # the proxy refreshes Google underneath (access_type=offline).
    kb_oauth_token_ttl: int = 86_400
    google_client_id: str = ""
    google_client_secret: str = ""
    ollama_url: str = "http://127.0.0.1:11434"
    kb_embed_model: str = "bge-m3"
    kb_embed_dim: int = 1024
    kb_embed_device: str = "auto"
    # qwen3:14b won the 6-model extractor benchmark (KB item 313): best granularity,
    # 100% verbatim grounding. gemma3:12b is the approved fast fallback.
    kb_extract_model: str = "qwen3:14b"
    # Extractor keep_alive (owner decision 2026-07): -1 keeps the model RESIDENT —
    # idle residency draws single-digit watts (GPU idles down; VRAM refresh is on
    # regardless), and it removes the ~20s reload per LLM call that dominated
    # consolidation runtime. Cost: ~10GB VRAM stays occupied. Kill switch: set 0
    # (unload per call) or a bounded value like 300 (seconds) to reclaim VRAM.
    kb_llm_keep_alive: int = -1
    # Deep search (explicit user-invoked, local Ollama): empty -> extract model.
    kb_deep_model: str = ""
    kb_deep_max_items: int = 16
    # WP2 graph expansion: default n-hop neighbor pull-in in search (0=off). A
    # per-call hops>0 overrides this; search caps the effective value to 3 hops.
    kb_graph_expand_hops: int = 0
    # Auto-resolution (owner decision, 2026-07): a re-ingested newer revision of
    # the SAME source doc supersedes its predecessor (git history IS the human
    # decision), and lopsided-evidence contradictions auto-resolve after a
    # 2-night stable margin (owner refinement of verdict V4). Kill switches:
    kb_auto_supersede_doc_refresh: bool = True
    kb_auto_resolve_evidence: bool = True
    # Search-quality regression sentinel (§8): the nightly consolidation replays
    # the golden retrieval eval and flags a hit@5/MRR drop vs the prior run. Set
    # False to skip the eval entirely (kill switch).
    kb_eval_in_consolidation: bool = True
    kb_inbox: Path = Path("~/kb/inbox")
    kb_raw: Path = Path("~/kb/raw")
    kb_backups: Path = Path("~/kb/backups")
    kb_git_repos: str = ""
    kb_archive_after_days: int = 180
    kb_dup_threshold: float = 0.97

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

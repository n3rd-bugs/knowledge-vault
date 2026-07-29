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
    # Dedicated web signing secret (WEBUI_TIERED_ACCESS_PLAN Phase 1): when set,
    # kbweb signs session cookies / CSRF tokens / OAuth-state with THIS secret
    # instead of the master KB_TOKEN, so cookie forgery and API-master are no
    # longer the same secret. Empty = fall back to KB_TOKEN (existing
    # deployments keep working; setting it forces one clean re-login).
    kb_web_signing_secret: str = ""
    kb_bind: str = "127.0.0.1"
    kb_port: int = 8321
    kb_rest_port: int = 8322
    kb_web_port: int = 8323
    # Google sign-in for the read-only web UI (kbweb): comma-separated allowlist
    # of emails and the public base URL used to build the OAuth redirect_uri.
    kb_web_allowed_emails: str = ""
    # No hardcoded hosts (CLAUDE.md): the real value is supplied via .env /
    # docker-compose. Empty is safe-and-loud — servers that NEED it (Google
    # sign-in) refuse to start without it; surfaces that only display it degrade.
    kb_web_public_url: str = ""
    # HARD row cap on the web /export.{csv,json} result set (tiered access,
    # WEBUI_TIERED_ACCESS_PLAN §5 Phase 3): bounds the bulk-exfiltration
    # economics of the CAP_EXPORT surface no matter what the client asks for.
    kb_web_export_cap: int = 500
    # Containerized runs bind 0.0.0.0 (container-private, fronted by Traefik/TLS);
    # set true only there. The default still refuses accidental host-public binds.
    kb_allow_any_bind: bool = False
    # Public/cloud instance gate: when true, reads see only projects flagged
    # public and writes to non-public projects are rejected (no existence leak).
    kb_public_only: bool = False
    # Per-project sharing (migration 011): TTL, in seconds, of the server-side
    # cache mapping a presented `kbg_` bearer to its resolved principal. Kept
    # short (default 60s) so a `kb principal revoke` takes effect within one
    # minute on every surface — the cache must never mask a revocation for long.
    kb_principal_cache_ttl: int = 60
    # OAuth (for claude.ai web/mobile connectors). When enabled the public MCP
    # instance accepts BOTH Google OAuth and the static bearer (MultiAuth).
    kb_oauth_enabled: bool = False
    # base URL for OAuth metadata/redirects. No hardcoded hosts (CLAUDE.md): the
    # real value comes from .env / docker-compose. Empty is safe-and-loud — the
    # server refuses to start with KB_OAUTH_ENABLED and this unset.
    kb_public_url: str = ""
    # Public REST base (e.g. https://api.<host>), used by kb_setup to pre-fill the
    # REMOTE session-hook config so a new machine needs only a token. Empty ->
    # kb_setup emits a generic placeholder.
    kb_public_rest_url: str = ""
    # Lifetime of the FastMCP-issued access token. Decoupled from Google's ~1h
    # upstream expiry so a client hiccup doesn't surface as an hourly logout;
    # the proxy refreshes Google underneath (access_type=offline).
    kb_oauth_token_ttl: int = 86_400
    # OAuth client onboarding: CIMD (Client ID Metadata Document) vs Dynamic Client
    # Registration. False = advertise DCR-only (historical claude.ai-compat default,
    # see the enable_cimd site in kbmcp.server). Flip true to A/B the now-switchable
    # DCR-vs-CIMD choice from .env without a rebuild. Default UNCHANGED.
    kb_oauth_cimd: bool = False
    google_client_id: str = ""
    google_client_secret: str = ""
    ollama_url: str = "http://127.0.0.1:11434"
    # Where the EMBEDDER lives. Defaults to ollama_url, so a single-box setup is
    # unchanged. Pointing it at a second machine is the cleanest fix for VRAM
    # contention: embedding runs on every chunk of every row, so moving it off
    # the extractor's card removes the swap that dominated drain time — the
    # local card then holds one 14B at a time instead of thrashing between a
    # 11.4GB extractor and a 2.3GB embedder that never both fit.
    kb_embed_url: str = ""
    kb_embed_model: str = "bge-m3"
    kb_embed_dim: int = 1024
    kb_embed_device: str = "auto"
    # Instruction prefix REQUIRED by instruction-aware embedders (qwen3-embedding)
    # on the QUERY side only ("Instruct: ...\nQuery: <q>"); document/chunk
    # embeddings stay plain. Empty = none (bge-m3). The exact prefix ships in
    # .env alongside the model swap — code stays model-agnostic. Measured
    # 2026-07-18 (MODEL_RESEARCH_2026-07 addendum): qwen3-embedding WITHOUT the
    # prefix is WORSE than bge-m3; with it, hit@5 0.759 vs 0.724.
    kb_embed_query_prefix: str = ""
    # qwen3:14b won the 6-model extractor benchmark (KB item 313): best granularity,
    # 100% verbatim grounding. gemma3:12b is the approved fast fallback.
    # How many queued jobs the drain extracts as ONE cohort. With
    # OLLAMA_MAX_LOADED_MODELS=1 every model switch is a full load, so running
    # each extractor once across N jobs instead of once per job turns ~3 loads
    # per row into ~3 per cohort. 1 disables cohorting entirely (kill switch).
    kb_drain_cohort: int = 8
    # keep_alive for a model while it works through its phase of a cohort.
    # Seconds, because llm.extract_items/verify_item are typed int | None —
    # only the low-level _chat accepts Ollama's "10m" string form.
    kb_drain_phase_keep_alive: int = 600
    kb_extract_model: str = "qwen3:14b"
    # Extractor keep_alive (owner decision 2026-07): -1 keeps the model RESIDENT —
    # idle residency draws single-digit watts (GPU idles down; VRAM refresh is on
    # regardless), and it removes the ~20s reload per LLM call that dominated
    # consolidation runtime. Cost: ~10GB VRAM stays occupied. Kill switch: set 0
    # (unload per call) or a bounded value like 300 (seconds) to reclaim VRAM.
    # Seconds the LLM stays resident. MUST be finite: with OLLAMA_MAX_LOADED_MODELS=1
    # a negative ("resident forever") value pins the only slot and every other model
    # — extractors, judges, embedder — waits for a slot that can never free, surfacing
    # as ReadTimeout rather than OOM. Measured 2026-07-29; see Invariant 8.
    kb_llm_keep_alive: int = 600
    # Context window for every /api/chat LLM call. Without an explicit num_ctx,
    # Ollama runs its RUNTIME default (4K), silently truncating transcript input
    # and capping JSON output (done_reason=length) — measured 2026-07-18: the
    # incumbent's grounding collapsed to 0% on >4K-token transcripts and every
    # model's output could be cut mid-JSON. 16384 covers our 16K-char extraction
    # window with output headroom; KV cost at 16K ≈ +2-3GB (fits beside the
    # resident 14B). Raise only after checking VRAM headroom.
    kb_llm_num_ctx: int = 16384
    # Quote repair (pipeline v2 WP-B1, owner decision 2026-07-18): an extracted
    # quote that fails the exact substring check gets ONE fuzzy-relocation attempt
    # (kbcore.quote_repair.find_span); the located TRUE span replaces the model's
    # version when the difflib ratio clears this floor, so SEC-3's verbatim
    # invariant holds by construction. Measured: 62% of ungrounded quotes locate
    # at >= 0.95; usable-item survival 46% -> 92% for the incumbent extractor.
    # Kill switch: 0 disables repair entirely (unrepaired quotes reject as before).
    kb_quote_repair_min_ratio: float = 0.95
    # Pipeline v2 ensemble (WP-B2, owner-approved 2026-07-18; measurements in
    # MODEL_RESEARCH_2026-07.md addendum 2): comma-separated EXTRA extractor
    # models run alongside the primary (e.g. "granite4.1:8b,gemma3:12b").
    # Measured: models find DIFFERENT knowledge (only 29% of cross-model
    # clusters were multi-model), so the pipeline UNIONs per-model output and
    # verifies per item — never consensus voting, which would discard most
    # knowledge; ensemble + quote repair yielded 4x quote-anchored knowledge
    # units per session. Empty = pipeline v1 (single model), the safe default.
    kb_extract_ensemble: str = ""
    # Cross-model item-clustering cosine threshold for the converge step.
    # 0.80 is the measured primary: 0.85 fragments same-fact clusters and
    # 0.75 over-merges distinct facts (addendum 2 prototype).
    kb_ensemble_cluster_threshold: float = 0.80
    # Shadow mode: the ensemble runs and REPORTS (one log line per extraction +
    # nightly counts) but ingests NOTHING — the v1 path's output stands. Flip
    # false to let ensemble survivors ingest (the ensemble then replaces v1).
    kb_ensemble_shadow: bool = True
    # Deep search (explicit user-invoked, local Ollama): empty -> extract model.
    kb_deep_model: str = ""
    kb_deep_max_items: int = 16
    # WP2 graph expansion: default n-hop neighbor pull-in in search (0=off). A
    # per-call hops>0 overrides this; search caps the effective value to 3 hops.
    kb_graph_expand_hops: int = 0
    # Optional cross-encoder rerank sidecar (llama-server --reranking with the
    # verified Qwen3-Reranker-0.6B GGUF). Empty = off (no HTTP call ever made).
    # Fail-open: a down/slow sidecar leaves the fused order untouched. Measured
    # 2026-07-18: ~425 ms warm for top_n=20 x ~500-token docs on the 4080S.
    kb_rerank_url: str = ""
    kb_rerank_top_n: int = 20
    kb_rerank_timeout: float = 2.0
    # Cross-family judge for PRIMARY-sourced ensemble singletons (probe-benched
    # 2026-07-18: granite4.1:8b approved 7/7 reals, caught 6/6 fabricated
    # probes, ~2.7s/item). Empty = primary singletons pass unjudged (the
    # pre-secondary behavior). A judge FAIL still ingests, flagged
    # verified_by='secondary-judge-failed' for triage (demote, never drop).
    kb_ensemble_secondary_judge: str = ""
    # Auto-resolution (owner decision, 2026-07): a re-ingested newer revision of
    # the SAME source doc supersedes its predecessor (git history IS the human
    # decision), and lopsided-evidence contradictions auto-resolve after a
    # 2-night stable margin (owner refinement of verdict V4). Kill switches:
    kb_auto_supersede_doc_refresh: bool = True
    kb_auto_resolve_evidence: bool = True
    # Review-queue hygiene (owner decision 2026-07-18, a bounded V4 refinement in
    # the spirit of kb_auto_resolve_evidence): nightly quiet-expiry closes open
    # unverified_extraction rows older than this many days whose item was NEVER
    # recalled (zero access_log rows). Only the review ROW closes — the item
    # stays active/unverified/searchable. Kill switch: 0 disables quiet-expiry.
    kb_review_expire_days: int = 30
    # WP-2 LLM triage (owner decision 2026-07-18): the nightly consolidation asks
    # the 14B for a keep / reject_noise / reject_duplicate SUGGESTION per open
    # unverified_extraction row (stored in review_queue.detail.triage); a human
    # applies suggestions in bulk (kb review apply-triage / kb_review_bulk). The
    # LLM never archives anything itself. This caps rows triaged per nightly
    # run. Kill switch: 0 disables the nightly step.
    kb_review_triage_batch: int = 50
    # Search-quality regression sentinel (§8): the nightly consolidation replays
    # the golden retrieval eval and flags a hit@5/MRR drop vs the prior run. Set
    # False to skip the eval entirely (kill switch).
    kb_eval_in_consolidation: bool = True
    # Anchored ABSOLUTE floor for the sentinel: flag whenever hit@5 / MRR fall below
    # these regardless of the night-over-night trend, so a gradual sub-epsilon drift
    # (invisible to the previous-run comparison) is still caught, and the
    # auto-resolve target never ratchets below the floor (review finding).
    kb_eval_min_hit5: float = 0.75
    kb_eval_min_mrr: float = 0.5
    kb_inbox: Path = Path("~/kb/inbox")
    kb_raw: Path = Path("~/kb/raw")
    kb_backups: Path = Path("~/kb/backups")
    # Encrypted offsite backups (PLAN.md §6d). Consumed by scripts/backup.sh /
    # scripts/offsite.sh straight from .env; typed here so Python surfaces share
    # the single documented config view. Off by default — backup.sh stays purely
    # local until the owner flips the switch.
    kb_offsite_enabled: bool = False
    # rclone remote:path for the encrypted dumps (owner configures via `rclone
    # config`, e.g. "gdrive:kb-backups"). No hardcoded remotes (CLAUDE.md).
    kb_rclone_remote: str = ""
    # age PUBLIC key ("age1..."): encryption recipient. Only ciphertext is ever
    # uploaded; the matching private key stays OFF this box.
    kb_age_recipient: str = ""
    # Offsite retention: keep this many newest kb-*.sql.zst.age objects remote.
    kb_offsite_keep: int = 30
    # Path to an age IDENTITY (private key) file — needed only by
    # `restore_test.sh --offsite`; in production it lives off-box.
    kb_age_identity: str = ""
    # --- Owner-only WEB backup-setup surface (owner decision 2026-07-21) --------
    # The web /backups page (kbcore.offsite_config + kbweb.views_backup) writes
    # offsite config to a DEDICATED file — NOT the main .env — as KEY=VALUE lines
    # the host scripts (scripts/backup.sh + scripts/offsite.sh) can `source`. The
    # conf file + the rclone-credential dir below are bind-mounted into kb-web
    # ONLY (infra done by the main agent). The age PRIVATE key is never written to
    # either: it is shown once by the web and stays OFF this box.
    kb_offsite_conf: str = str(Path.home() / "kb" / "webconf" / "offsite.env")
    # rclone credential store (a service-account JSON or an rclone.conf remote
    # block), written WRITE-ONLY by store_rclone_cred; never read back by the web.
    kb_rclone_conf_dir: str = str(Path.home() / "kb" / "webconf" / "rclone")
    # age-keygen binary seam: a list-argv subprocess (never shell=True). Overridden
    # in tests to a stub emitting a fixed keypair; prod default is the installed bin.
    kb_age_bin: str = "age-keygen"
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

    @property
    def embed_url(self) -> str:
        """The embedder endpoint — its own host when set, else the main one."""
        return (self.kb_embed_url or self.ollama_url).rstrip("/")

    @property
    def extract_ensemble(self) -> list[str]:
        """`KB_EXTRACT_ENSEMBLE` parsed as a comma-separated list of extra models."""
        return [m.strip() for m in self.kb_extract_ensemble.split(",") if m.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

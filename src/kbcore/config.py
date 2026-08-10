"""kbcore.config — single source of runtime configuration (PLAN.md §2/§3).

All configuration flows through `settings`. Nothing else in the codebase reads
`os.environ` or hardcodes paths, ports, model names, or secrets (CLAUDE.md hard
rule): import `settings` from here instead.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from kbcore.config_views import SettingsViews

logger = logging.getLogger("kbcore.config")

# Repo root is two levels up from src/kbcore/config.py.
_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(SettingsViews, BaseSettings):
    """Typed view over `.env` (see `.env.example` for the documented template).

    The DERIVED properties (db_url, raw, embed_url, …) live in
    ``kbcore.config_views``; this class declares the fields and nothing else.
    """

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
    # Dedicated web signing secret (WEBUI_TIERED_ACCESS_PLAN Phase 1): when set, kbweb signs
    # session cookies / CSRF tokens / OAuth-state with THIS secret instead of the master KB_TOKEN,
    # so cookie forgery and API-master are no longer the same secret. Empty = fall back to KB_TOKEN
    # (existing deployments keep working; setting it forces one clean re-login).
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
    # Per-project sharing (migration 011): TTL, in seconds, of the server-side cache mapping a
    # presented `kbg_` bearer to its resolved principal. Kept short (default 60s) so a `kb
    # principal revoke` takes effect within one minute on every surface — the cache must never mask
    # a revocation for long.
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
    # Extra hosts kb_setup may honor in a caller-supplied rest_url (comma-separated);
    # that URL is curl|bash'd at install, so a foreign host is ignored unless listed
    # here — the configured host is always honored. See kbcore.setup_url.
    kb_setup_allowed_rest_hosts: str = ""
    # Completion-notification channel handed to enrolled machines. Empty disables the feature
    # entirely — no endpoint output, nothing written on install. KB_NOTIFY_TOKEN must be a
    # WRITE-ONLY ntfy token scoped to the one topic, not an admin credential: it is distributed to
    # every machine that enrols, so it must be able to announce completions and nothing else. It
    # cannot read the notification history (which carries client project names) and is revocable
    # per-machine at the ntfy side.
    kb_notify_url: str = ""
    kb_notify_topic: str = ""
    kb_notify_token: str = ""
    # 401-burst alert (Package O): >N failed-auth/10min pushes one ntfy line, 1/h max; 0=off.
    kb_auth_alert_threshold: int = 20
    # Lifetime of the FastMCP-issued access token. Decoupled from Google's ~1h
    # upstream expiry so a client hiccup doesn't surface as an hourly logout;
    # the proxy refreshes Google underneath (access_type=offline).
    kb_oauth_token_ttl: int = 86_400
    # OAuth client onboarding: CIMD (Client ID Metadata Document) vs Dynamic Client Registration.
    # False = advertise DCR-only (historical claude.ai-compat default, see the enable_cimd site in
    # kbmcp.server). Flip true to A/B the now-switchable DCR-vs-CIMD choice from .env without a
    # rebuild. Default UNCHANGED.
    kb_oauth_cimd: bool = False
    google_client_id: str = ""
    google_client_secret: str = ""
    ollama_url: str = "http://127.0.0.1:11434"
    # Where the EMBEDDER lives. Defaults to ollama_url, so a single-box setup is unchanged.
    # Pointing it at a second machine is the cleanest fix for VRAM contention: embedding runs on
    # every chunk of every row, so moving it off the extractor's card removes the swap that
    # dominated drain time — the local card then holds one 14B at a time instead of thrashing
    # between a
    # 11.4GB extractor and a 2.3GB embedder that never both fit.
    kb_embed_url: str = ""
    # MUST stay coherent with kb_embed_query_prefix below — the pair is the config, not either
    # half. An instruction-aware embedder run WITHOUT its prefix is measurably WORSE than bge-m3
    # (hit@5 0.724 vs 0.759 with it, MODEL_RESEARCH_2026-07.md:192), so a default that names the
    # model but leaves the prefix empty ships the worst of both. Defaults now match what is
    # actually deployed, and _check_embed_pairing() warns if the two are ever split.
    kb_embed_model: str = "qwen3-embedding:0.6b"
    kb_embed_dim: int = 1024
    kb_embed_device: str = "auto"
    # Instruction prefix REQUIRED by instruction-aware embedders (qwen3-embedding) on the QUERY
    # side only ("Instruct: ...\nQuery: <q>"); document/chunk embeddings stay plain. Empty = none
    # (bge-m3). The exact prefix ships in .env alongside the model swap — code stays
    # model-agnostic. Measured 2026-07-18 (MODEL_RESEARCH_2026-07 addendum): qwen3-embedding
    # WITHOUT the prefix is WORSE than bge-m3; with it, hit@5 0.759 vs 0.724.
    kb_embed_query_prefix: str = (
        "Instruct: Given a web search query, retrieve relevant passages that "
        "answer the query\nQuery: "
    )
    # qwen3:14b won the 6-model extractor benchmark (KB item 313): best granularity, 100% verbatim
    # grounding. gemma3:12b is the approved fast fallback. How many queued jobs the drain extracts
    # as ONE cohort. With OLLAMA_MAX_LOADED_MODELS=1 every model switch is a full load, so running
    # each extractor once across N jobs instead of once per job turns ~3 loads per row into ~3 per
    # cohort. 1 disables cohorting entirely (kill switch).
    #
    # Sized on JOB COUNT, not bytes (measured 2026-07-29): assembled inputs vary 40x (1.0-43.5 kB)
    # while cohort times varied only +/-3%, because per-job cost is dominated by MODEL LOADING, not
    # input size. At 77.6 s/job, 46 jobs is ~1 hour at FULL power. Derated to 30 on 2026-07-30 when
    # the GPU was capped at 150W (UPS could not ride out a grid fluctuation): inference is slower,
    # so a 46-job cohort risked exceeding jobs.PROGRESS_WINDOW_MINUTES and being reaped as dead. A
    # shorter cohort also loses less in-memory work to the next power cut. Raising this further
    # requires raising jobs.PROGRESS_WINDOW_MINUTES too: no row reaches 'extracted' until its
    # cohort finishes, so a longer cohort looks dead to the stale-run reaper.
    kb_drain_cohort: int = 30
    # Memory ceiling for one cohort's assembled text — an OOM GUARD, not a throughput knob (bytes
    # are a poor predictor of time; see above). It exists so a future large transcript cannot make
    # a 46-job cohort exhaust RAM. Assembly must always take AT LEAST ONE job even if that job
    # alone exceeds this, or an oversized row would wedge the queue permanently.
    kb_drain_cohort_bytes: int = 48_000_000
    # PER-WINDOW RESUME MEMORY (owner request 2026-08-10). A multi-window group interrupted part
    # way — a failing window, the tail reserve, the deadline — re-extracted from window 1 the next
    # night, and the night after. Measured on three giant sessions (aquamonix 269 rows/116 MB at
    # deferrals=6, ims 59/52 MB, weird-rtos 10/8 MB): ~8 GPU-hours a night re-mining windows whose
    # items were already stored (they dedupe to nothing on content_hash), then the same failing
    # window, then the whole group deferred again — 7.9 h drain, 0 drained, 0 saved. The drain now
    # records which windows SUCCEEDED on the group's first row, keyed to a fingerprint of the
    # assembled text, and skips them on the next attempt. ROW FLIPS ARE UNCHANGED (still
    # all-or-nothing, still group-atomic); only the extraction CALLS remember. False = the old
    # full-re-extract behaviour.
    kb_drain_window_checkpoint: bool = True
    # Wall-clock time (HH:MM, local) after which the drain starts NO NEW cohort. The box is shared
    # with interactive work and sits on a UPS that cannot ride out a grid fluctuation, so an
    # unbounded overnight run is both a latency and a power risk. Stopping is always SAFE and
    # always RESUMABLE: rows the drain has not reached are still 'pending', ingest is idempotent
    # (Invariant 3), and the next run — nightly, or the boot catch-up after a power cut — picks up
    # exactly where this one stopped. Empty disables the deadline.
    kb_drain_deadline: str = "12:00"
    # Days the deadline APPLIES (local). Weekends default to unbounded: the box
    # is free, so the nightly may run past noon and finally clear the LLM tail.
    kb_drain_deadline_days: str = "mon,tue,wed,thu,fri"
    # MINUTES OF THE WINDOW RESERVED FOR THE LLM TAIL. The 01:00-12:00 window is subscribed at
    # 100-110% on a median night (drain 5.9-7.1 h + tail 5.0-6.0 h against 11 h), so whichever runs
    # last gets nothing — today the tail (report 9684: 99.5% of the window drained, every tail step
    # stood down). The drain therefore stops admitting NEW refill cohorts once now +
    # estimated_next_cohort_cost > deadline - reserve; 0 disables it. See kbworker.allocator for
    # why the DRAIN is the right residual claimant. Never extends past kb_drain_deadline —
    # deadline/battery still preempt the tail.
    kb_tail_reserve_minutes: int = 180
    # FRACTION OF THE TAIL RESERVE HANDED TO TRIAGE so the FIRST tail step cannot eat the window
    # contradictions/evidence run in (stops triage at reserve_start + share*reserve). 0 = OLD
    # pooled reserve (inert) and is the default => opt-in, reproduces today's run exactly; weekends
    # have no deadline so inert there too.
    kb_tail_triage_share: float = 0.0
    # PAUSE THE DRAIN WHEN THE HOUSE IS ON BATTERY. The GPU is the largest load on this box and the
    # UPS could not ride out a grid fluctuation (2026-07-30), so hours of inference during an
    # outage is exactly the wrong thing to be doing. Read-only path to the sungrow monitor DB;
    # empty disables the check entirely.
    kb_power_db: str = "/home/usama/sungrow/db.sqlite3"
    # Grid-healthy threshold. Measured over 219,306 readings 2026-07-31:
    #     0-50V 0.77% (outage) | 50-150V 0.08% | 150-200V 0.07% (sag)
    #     200-210V 0.37% | 210-220V 6.22% | 220V+ 92.50% (normal)
    # 200 captures every abnormal band INCLUDING transitions and brownouts, while leaving ~12V of
    # margin below the 212-213V readings seen today, so ordinary mains never trips it. Owner
    # decision: keep the GPU down through transitional readings, not just hard outages — a sagging
    # grid is when the UPS is least able to absorb a 150W spike.
    kb_power_min_grid_volts: float = 200.0
    # A reading older than this tells us nothing about NOW. Treating a stale row as
    # "grid is fine" is precisely how you flatten a battery through an outage, so
    # staleness is handled explicitly rather than by trusting the last known value.
    kb_power_max_age_seconds: int = 300
    # Readings inspected per check. ANY sample below the threshold in this window keeps the GPU
    # down, so the drain resumes only once the grid has been consistently healthy — it will not
    # restart into a flickering supply, which is the failure mode a single-sample check invites.
    kb_power_window: int = 5
    # keep_alive for a model while it works through its phase of a cohort.
    # Seconds, because llm.extract_items/verify_item are typed int | None —
    # only the low-level _chat accepts Ollama's "10m" string form.
    kb_drain_phase_keep_alive: int = 600
    kb_extract_model: str = "qwen3:14b"
    # Seconds the LLM stays resident. MUST be finite: with OLLAMA_MAX_LOADED_MODELS=1 a negative
    # ("resident forever") value pins the only slot and every other model — extractors, judges,
    # embedder — waits for a slot that can never free, surfacing as ReadTimeout rather than OOM.
    # Measured 2026-07-29; see Invariant 8.
    kb_llm_keep_alive: int = 600
    # Context window for every /api/chat LLM call. Without an explicit num_ctx, Ollama runs its
    # RUNTIME default (4K), silently truncating transcript input and capping JSON output
    # (done_reason=length) — measured 2026-07-18: the incumbent's grounding collapsed to 0% on
    # >4K-token transcripts and every model's output could be cut mid-JSON. 16384 covers our
    # 16K-char extraction window with output headroom; KV cost at 16K ≈ +2-3GB (fits beside the
    # resident 14B). Raise only after checking VRAM headroom.
    kb_llm_num_ctx: int = 16384
    # Quote repair (pipeline v2 WP-B1, owner decision 2026-07-18): an extracted quote that fails
    # the exact substring check gets ONE fuzzy-relocation attempt (kbcore.quote_repair.find_span);
    # the located TRUE span replaces the model's version when the difflib ratio clears this floor,
    # so SEC-3's verbatim invariant holds by construction. Measured: 62% of ungrounded quotes
    # locate at >= 0.95; usable-item survival 46% -> 92% for the incumbent extractor. Kill switch:
    # 0 disables repair entirely (unrepaired quotes reject as before).
    kb_quote_repair_min_ratio: float = 0.95
    # Pipeline v2 ensemble (WP-B2, owner-approved 2026-07-18; measurements in
    # MODEL_RESEARCH_2026-07.md addendum 2): comma-separated EXTRA extractor models run alongside
    # the primary (e.g. "granite4.1:8b,gemma3:12b"). Measured: models find DIFFERENT knowledge
    # (only 29% of cross-model clusters were multi-model), so the pipeline UNIONs per-model output
    # and verifies per item — never consensus voting, which would discard most knowledge; ensemble
    # + quote repair yielded 4x quote-anchored knowledge units per session. Empty = pipeline v1
    # (single model), the safe default.
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
    # ---- Wave-3 search dials (design review #2, KB 16601). Each ships at its
    # NO-OP default so today's ranking is byte-identical (kbcore.search / kbcore.search_rank),
    # proven by the golden eval. ---- (2) Trust dial — the real form of V4's "unverified ranked
    # slightly lower": a VERIFIED candidate's fused score is multiplied by this in _apply_bonuses.
    # 1.0 = inert; size it BELOW one rank-1 RRF increment (like BOOST_FACTOR) so it
    # only breaks near-ties toward verified, never lifts a weak verified over a
    # strong unverified answer.
    kb_trust_factor: float = 1.0
    # (3) Rare-token identifier leg — for a NON-id-dominant query, ALSO run the id leg for
    # identifier tokens whose corpus df (tsv GIN index) is <= kb_id_leg_rare_max_df, fused at a
    # rank offset below any real lexical hit. False = today's candidate set exactly.
    kb_id_leg_rare_tokens: bool = False
    kb_id_leg_rare_max_df: int = 2
    # (4) Rank-time twin collapse — two candidates in the SAME result set joined by
    # a cross-project 'alternative_to' edge collapse to the higher-scored one
    # (loser's score folded in, loser dropped; no status flip). False = inert.
    kb_collapse_alternatives: bool = False
    # Optional cross-encoder rerank sidecar (llama-server --reranking with the verified
    # Qwen3-Reranker-0.6B GGUF). Empty = off (no HTTP call ever made). Fail-open: a down/slow
    # sidecar leaves the fused order untouched. Measured 2026-07-18: ~425 ms warm for top_n=20 x
    # ~500-token docs on the 4080S.
    kb_rerank_url: str = ""
    kb_rerank_top_n: int = 20
    kb_rerank_timeout: float = 2.0
    # Rerank EVIDENCE source (A/B PENDING — do not flip the default without one). False (today's
    # behaviour): the reranker scores `title + body prefix` read back from items. True: it scores
    # `title + the best-matching CHUNK` that the RRF fusion already computed and then threw away —
    # the passage that actually matched the query, at the cost of less surrounding context (a chunk
    # is 300-500 tokens, the body prefix is 1500 chars). Which wins is empirical, so this ships
    # OFF: flip it, run `make eval` against the real corpus, compare hit@5/MRR with the OFF
    # baseline, and only then consider a new default.
    kb_rerank_use_chunk: bool = False
    # Cross-family judge for PRIMARY-sourced ensemble singletons (probe-benched 2026-07-18:
    # granite4.1:8b approved 7/7 reals, caught 6/6 fabricated probes, ~2.7s/item). Empty = primary
    # singletons pass unjudged (the pre-secondary behavior). A judge FAIL still ingests, flagged
    # verified_by='secondary-judge-failed' for triage (demote, never drop).
    kb_ensemble_secondary_judge: str = ""
    # Auto-resolution (owner decision, 2026-07): a re-ingested newer revision of the SAME source
    # doc supersedes its predecessor (git history IS the human decision), and lopsided-evidence
    # contradictions auto-resolve after a 2-night stable margin (owner refinement of verdict V4).
    # Kill switches:
    kb_auto_supersede_doc_refresh: bool = True
    kb_auto_resolve_evidence: bool = True
    # Review-queue hygiene (owner decision 2026-07-18, a bounded V4 refinement in the spirit of
    # kb_auto_resolve_evidence): nightly quiet-expiry closes open unverified_extraction rows older
    # than this many days whose item was NEVER recalled (zero access_log rows). Only the review ROW
    # closes — the item stays active/unverified/searchable. Kill switch: 0 disables quiet-expiry.
    kb_review_expire_days: int = 30
    # BLAST RADIUS of one quiet-expiry run (owner amendment 2026-08-08). The sweep was a single
    # uncapped UPDATE that had never actually fired in anger; its first real firing (2026-08-19)
    # faced a ~6,600-row 30-day pool and would have closed all of it in one statement, with no undo
    # written yet and no way to eyeball what went. Capped and ordered by rq.created_at (oldest
    # first), the cliff becomes a ramp: each night closes the rows nearest their deadline and the
    # report says how many were left. 0 = UNCAPPED, explicitly — the kill switch is
    # kb_review_expire_days=0 above, never this knob.
    kb_review_expire_cap: int = 500
    # WP-2 LLM triage (owner decision 2026-07-18): the nightly consolidation asks the 14B for a
    # keep / reject_noise / reject_duplicate SUGGESTION per open unverified_extraction row (stored
    # in review_queue.detail.triage); a human applies suggestions in bulk (kb review apply-triage /
    # kb_review_bulk). The LLM never archives anything itself. This caps rows triaged per nightly
    # run. Kill switch: 0 disables the nightly step. COUPLED TO kb_review_expire_days ABOVE — the
    # pair is what decides whether a bad extraction is ever looked at. Triage is oldest-first
    # (review_triage.py ORDER BY rq.id), which is also expiry order, so each night it examines the
    # rows closest to aging out. But a row that expires before triage reaches it closes having
    # NEVER been examined, and its item stays live and searchable forever. So this batch must be >=
    # the nightly MINT rate, or the gap silently becomes permanently-unexamined knowledge. Measured
    # 2026-07-30: 8.35 review rows per session extracted (pipeline v2 raised this ~8x from ~1.0),
    # and at batch=50 coverage was 227/4560 = 5% — i.e. ~95% of rows were on track to expire
    # unread. 500 covers a normal night and lets the backlog drain instead of grow. Cost is ~500
    # LLM calls, which is minutes now that model-major cohorting landed. Kill switch: 0.
    kb_review_triage_batch: int = 500
    # WEEKEND TRIAGE TOP-UP (owner request 2026-08-08): "at weekend triage as many items as we
    # can". On a day with NO drain deadline (kbcore.drainsched) the nightly can finish at 19:45 and
    # leave the GPU idle until the next 01:00 firing, so the tail keeps triaging in SLICES until
    # this local HH:MM cutoff, the queue empties, or the run's stand-down guard fires. EMPTY (or a
    # malformed value) disables the feature entirely; the batch knob above is unchanged, so a
    # normal weekday night behaves exactly as before. Applies to UNATTENDED runs only (the guarded
    # `nightly()` path) — an owner-invoked `kb consolidate-now` is never turned into a nine-hour
    # triage grind.
    kb_triage_topup_until: str = "23:00"
    # Rows per top-up slice, and the reason this is a slice loop rather than one big batch: the run
    # holds pg_advisory_lock(42) for its whole duration, so a slice still running at 01:00 makes
    # the NEXT nightly skip outright. At the measured 10.4 s/row, 200 rows is ~35 min — a slice
    # admitted at 22:59 ends ~23:35, leaving the archive/re-embed leftovers, the report and the
    # post-run backup room before 01:00. Kill switch: <= 0.
    kb_triage_topup_slice: int = 200
    # AUTOMATED reject-apply (owner decision 2026-08-02, kbcore.review_apply), revising "a human
    # applies in bulk": that loop sat dead 15 days while 853 reject-suggested items stayed live and
    # expiry-exempt. Cooling days = the nightly digests the owner sees a queued application in
    # before the machine acts (veto = resolve the row); cap bounds one run; enabled is the switch.
    kb_auto_apply_cooling_days: int = 3
    kb_auto_apply_cap: int = 300
    # Per-run cap on dead-dup-target triage invalidation (design review #2, KB
    # 16601): re-queue a reject_duplicate whose dup_of is provably dead for a fresh
    # triage verdict instead of re-counting it skipped_target_gone forever. 0 = off.
    kb_triage_invalidate_cap: int = 200
    kb_auto_apply_enabled: bool = True
    # Search-quality regression sentinel (§8): the nightly consolidation replays
    # the golden retrieval eval and flags a hit@5/MRR drop vs the prior run. Set
    # False to skip the eval entirely (kill switch).
    kb_eval_in_consolidation: bool = True
    # Anchored ABSOLUTE floor for the sentinel: flag whenever hit@5 / MRR fall below these
    # regardless of the night-over-night trend, so a gradual sub-epsilon drift (invisible to the
    # previous-run comparison) is still caught, and the auto-resolve target never ratchets below
    # the floor (review finding).
    kb_eval_min_hit5: float = 0.75
    kb_eval_min_mrr: float = 0.5
    kb_inbox: Path = Path("~/kb/inbox")
    kb_raw: Path = Path("~/kb/raw")
    kb_backups: Path = Path("~/kb/backups")
    # Encrypted offsite backups (PLAN.md §6d). Consumed by scripts/backup.sh / scripts/offsite.sh
    # straight from .env; typed here so Python surfaces share the single documented config view.
    # Off by default — backup.sh stays purely local until the owner flips the switch.
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
    # The web /backups page (kbcore.offsite_config + kbweb.views_backup) writes offsite config to a
    # DEDICATED file — NOT the main .env — as KEY=VALUE lines the host scripts (scripts/backup.sh +
    # scripts/offsite.sh) can `source`. The conf file + the rclone-credential dir below are
    # bind-mounted into kb-web ONLY (infra done by the main agent). The age PRIVATE key is never
    # written to either: it is shown once by the web and stays OFF this box.
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
    # --- Owner-fired CROSS-PROJECT twin detector (kbcore.twin_crossproject; KB 13061).
    # The nightly dedupe is per-project by settled design, so the SAME gotcha captured in two
    # projects (124/858, 383/488) leaves both copies active and dilutes rank. This owner-invoked
    # pass links such pairs with 'alternative_to' (NO status flip, Inv 5 — nothing leaves any
    # project's surface; the rank-time collapse folds them only when both land in one result set).
    # A candidate is a cross-project lead-chunk cosine SIMILARITY strictly above this, then
    # confirmed same-fact by the 14B judge.
    kb_xtwin_threshold: float = 0.93
    # Max candidate pairs enumerated (and thus 14B-judged) per run — the call budget of
    # an owner-invoked pass. 0 is the KILL SWITCH (nothing enumerated, judged, or linked).
    kb_xtwin_cap: int = 200
    # Contradiction pairs the 14B may judge per NIGHT, across all projects — the sibling of
    # kbworker.consolidate.EVIDENCE_PAIR_CAP for the step BEFORE it. The step had no bound at all
    # and grew 4,972s -> 8,194s in two days. It is a per-night budget rather than a per-project one
    # so one large project cannot eat the whole GPU window. 200 is roughly a 10-20 minute step at
    # measured per-pair cost, and it is a CATCH-UP bound rather than a steady-state one: with the
    # judged-pair memory (migration 017) a normal night only sees pairs that are genuinely new, so
    # the cap binds only while a backlog drains. Unreached pairs are simply offered again next run.
    # Kill switch: 0.
    kb_contradiction_pair_cap: int = 500
    # 500 (was 200, 2026-08-03): measured 9.2 s/pair (not the 41 inferred) => ~77
    # min; safe to widen because judged pairs PERSIST across a deadline cut.

    @model_validator(mode="after")
    def _check_embed_pairing(self) -> Settings:
        """Warn when an instruction-aware embedder is configured without its prefix.

        These two settings are one decision wearing two names. qwen3-embedding
        needs its "Instruct: …\\nQuery: " prefix on the QUERY side to beat bge-m3
        (hit@5 0.759 vs 0.724); without it, it LOSES to the model it replaced
        (MODEL_RESEARCH_2026-07.md:192). Splitting the pair is silent — retrieval
        just gets quietly worse, which is the hardest kind of regression to notice
        and the reason this warns instead of trusting a comment.
        """
        if "qwen3-embedding" in self.kb_embed_model and not self.kb_embed_query_prefix.strip():
            logger.warning(
                "KB_EMBED_MODEL=%s is instruction-aware but KB_EMBED_QUERY_PREFIX is empty — "
                "retrieval is measurably WORSE than bge-m3 in this configuration",
                self.kb_embed_model,
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

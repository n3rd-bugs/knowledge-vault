"""kbcore.config_llm — the LLM / model-seat / judge / pair-cap knobs.

Mechanical split from :mod:`kbcore.config` (2026-09-03, review #8 H0), for the
reason ``config_views`` was split before it: config.py sat at its size-ratchet
ceiling (492 = 467 + 25) and the pair-judge swap (H3) must add two knobs here.

A ``BaseSettings`` MIXIN, not a second settings object: :class:`kbcore.config.
Settings` inherits it, and pydantic-settings collects the inherited fields
under the CHILD's ``model_config`` — the ``.env`` file, ``extra='ignore'`` and
case-insensitive env names apply to these fields exactly as before. VERIFIED at
landing (pydantic-settings 2.14.2 spike): an inherited field reads from the
child's env_file, a lowercase process-env override beats the file, and the
derived views on ``config_views`` see the value — so the documented fallback
(prose-trim in config.py) was not needed. Field ORDER in
``Settings.model_fields`` now lists these first; every consumer (kbcore.scrub,
kbcore.ref_check) walks the fields as a set / sorted tuple. Never instantiate
this class on its own: config STILL flows through ``kbcore.config.settings``
only (CLAUDE.md hard rule), and nothing else may import it.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings


class LLMSettings(BaseSettings):
    """The model seats and their budgets, mixed into ``kbcore.config.Settings``.

    Extractor + residency + context window, quote repair, the ensemble, deep
    search, the secondary judge and the contradiction pair caps. Read them as
    ``settings.<name>``; the comments are each knob's documentation of record.
    """

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
    # Malformed-JSON re-ask (incident 2026-09-12): ONE ~900 KB window came back with an unescaped
    # quote inside a string, extract_items raised, the per-job fallback failed identically and
    # 151 rows flipped to failed — and the fixed EXTRACT_SEED guaranteed the next two nightlies
    # would reproduce the same bytes and burn the attempts cap. >0 = re-ask ONCE with seed+1 and
    # a repair hint before salvaging item-by-item (kbcore.llm_json.recover_items). 0 disables the
    # re-ask (salvage still runs). Only ever one retry regardless of the value.
    kb_extract_json_retry: int = 1
    # Unparseable extractor replies are dumped raw (scrubbed) to $KB_STATE_DIR/extract-failures
    # for post-mortem — journald cannot carry a 900 KB body. Keep this many newest; 0 disables.
    # Debug artefacts, not knowledge: rotating them is outside rule 5's scope.
    kb_extract_failure_dumps: int = 20
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
    # Cross-family judge for PRIMARY-sourced ensemble singletons (probe-benched 2026-07-18:
    # granite4.1:8b approved 7/7 reals, caught 6/6 fabricated probes, ~2.7s/item). Empty = primary
    # singletons pass unjudged (the pre-secondary behavior). A judge FAIL still ingests, flagged
    # verified_by='secondary-judge-failed' for triage (demote, never drop).
    kb_ensemble_secondary_judge: str = ""
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
    # Day-aware weekday cap (review #6): deadline days judge min(cap, this) — kbworker.tail_ops.
    # Widen 40 -> 200 ONLY while the CONTRADICTION seat (below) is a fast judge (~2.3 min at
    # 0.7 s/pair; 40-67 min at 12-20 s/pair) and put it back to 40 when it is not.
    kb_contradiction_pair_cap_weekday: int = 40
    # Pair-judge seat (review #8 H3, bench-gate rule): the model behind the TWO pairwise
    # judges — llm.items_contradict (nightly contradiction step) and
    # twin_judge.judge_same_fact (cross-project twin linker) — unless the contradiction
    # seat below is set, which then takes items_contradict alone. Every other seat
    # (extractor, verifier, triage, evidence, deep search, ensemble secondary judge) keeps
    # its incumbent. Empty = the incumbent kb_extract_model with its default thinking.
    # NOT kb_ensemble_secondary_judge — that knob's empty sentinel means the opposite.
    kb_pair_judge_model: str = ""
    # Ollama `think` for the pair judges: None = key omitted (the model's default); false =
    # thinking OFF (measured ~0.7 s/pair vs 10-20 s thinking). Read ONLY while
    # kb_pair_judge_model is set, so emptying the model restores model AND thinking in
    # one line. true|false or absent — a blank value fails validation (bool | None).
    kb_pair_judge_think: bool | None = None
    # CONTRADICTION seat (2026-10-25, KB 54572; KB 31871 asked for the split): the model
    # behind llm.items_contradict ALONE — the nightly contradiction step and the precision
    # gate before any evidence auto-resolve (kbcore.review_judge_gate). Empty = the pair
    # seat above. Measured on the 92 pairs the nightly auto-resolved 08-25..10-24 against a
    # blind two-labeler ground truth (42 real conflicts): qwen3:14b precision 0.52 / recall
    # 0.79 at 12.7 s/pair, bonsai-2:27b 0.78 / 0.83 at 11.8 s/pair. Rollback = empty it.
    # Pairs an earlier seat judged "no conflict" stay remembered (contradiction_judgements is
    # keyed by EMBED model) and are not re-judged by a new seat until a re-embed.
    kb_contradiction_judge_model: str = ""
    # Ollama `think` for that seat, read ONLY while its model is set (the rule above).
    kb_contradiction_judge_think: bool | None = None

    @property
    def pair_judge_model(self) -> str:
        """Model for the two pair judges; falls back to the extractor model."""
        return self.kb_pair_judge_model or self.kb_extract_model

    @property
    def pair_judge_think(self) -> bool | None:
        """``think`` for the two pair judges — None (key omitted) unless a model is set."""
        return self.kb_pair_judge_think if self.kb_pair_judge_model else None

    @property
    def contradiction_judge_model(self) -> str:
        """Model for items_contradict: its own seat, else the pair seat (else the extractor)."""
        return self.kb_contradiction_judge_model or self.pair_judge_model

    @property
    def contradiction_judge_think(self) -> bool | None:
        """``think`` for items_contradict: its own knob while its model is set, else the pair's."""
        if self.kb_contradiction_judge_model:
            return self.kb_contradiction_judge_think
        return self.pair_judge_think

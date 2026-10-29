"""kbworker.consolidate — nightly dedupe/summarize/archive, advisory-locked (§7.3).

Takes ``pg_advisory_lock(42)`` for the whole run (ARCH-5); if another run holds
it, logs and exits without work. Runs the six §7.3 steps in order, each isolated
so one failure logs and the rest proceed. Only kbcore service functions touch the
DB (Invariant 2). The 14B extractor is invoked ONLY here, keep_alive=0 (Invariant
8). Run: ``python -m kbworker.consolidate`` (make consolidate).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from kbcore import (
    consolidation,
    documents,
    drainsched,
    enrolment,
    evidence,  # noqa: F401 — patch surface: tests patch `consolidate.evidence.*`
    fleet_flush,
    fleet_health,
    ingest_queue,
    judge_fit,
    llm,  # noqa: F401 — patch surface: tests patch `consolidate.llm.*`
    logsafe,
    review_auto,
    review_rejects,
    review_triage,
)
from kbcore.config import settings
from kbcore.db import advisory_lock
from kbworker import (
    allocator,
    ensemble,
    eval_sentinel,
    sessions,  # noqa: F401 — patch surface: tests patch `consolidate.sessions.*`
    tail_steps,
)

# MECHANICAL SPLIT (2026-08-04): the extraction-input prep, the per-cohort
# machinery and the LLM-tail steps moved to their own modules. Every moved name
# is re-imported HERE because this module is the established seam: call sites
# below and the test suite's monkeypatches all resolve through consolidate's
# globals (patching `consolidate._summaries` rebinds the name main() looks up),
# and the shared dicts (_COHORT_STATS/_CONTRA_STATS/_STOP_LOG) are one object
# mutated in place on both sides, never rebound. `sessions` and `llm` above are
# likewise part of the patch surface (tests patch attributes ON those modules
# via `consolidate.sessions` / `consolidate.llm`) even where this file no
# longer calls them directly.
from kbworker.cohort import (  # noqa: F401
    _COHORT_STATS,
    _cohort_prepare,
    _drain_job,
    record_deferral_pressure,
    reset_cohort_stats,
)

# The drain loop (QUEUE_BATCH / MAX_REFILLS / _drain_ingest_queue / _drain_step)
# moved to kbworker.drainloop (mechanical split, 2026-09-03, review #8 H0) —
# re-imported for the same seam reason: the tests drive and REBIND them as
# ``consolidate.<name>``, and drainloop reads its five patched collaborators
# back through THIS module at call time (see its docstring).
from kbworker.drainloop import (  # noqa: F401
    MAX_REFILLS,
    QUEUE_BATCH,
    _drain_ingest_queue,
    _drain_step,
)

# The power guard moved to kbworker.powerguard (mechanical split, 2026-08-21,
# review #5) — re-imported for the same seam reason: should_stop resolves the
# global ``_on_battery``, and the test suite patches ``consolidate._on_battery``.
# Step 3b (_evidence / _evidence_batch) moved to kbworker.evidence_step
# (mechanical split, 2026-09-13) — re-imported for the same seam reason; the
# cap, the reserve divisor and the stop log are read back through THIS module.
from kbworker.evidence_step import (  # noqa: F401
    _EVIDENCE_STATS,
    _GATE_STATS,
    _evidence,
    _evidence_batch,
)
from kbworker.powerguard import (  # noqa: F401
    _on_battery,
    _reading_age_seconds,
    record_battery_pause,
)
from kbworker.prep import (  # noqa: F401
    EXTRACT_BYTES_PER_TOKEN,
    EXTRACT_PROMPT_OVERHEAD_TOKENS,
    MIN_WINDOW_BYTES,
    TOKENIZER_SAFETY,
    _assemble_jobs,
    _batch_jobs,
    _fit_windows,
    _token_ceiling,
    _warn_if_oversized,
    window_budget_bytes,
    window_budget_tokens,
)

# Host telemetry, the timed step runner and the run report moved to
# kbworker.run_report (same split) — re-imported: main() and the tests call
# them as ``consolidate.<name>``.
from kbworker.run_report import (  # noqa: F401
    _disk_pct,
    _host_memory,
    _rejection_rate,
    _rerank_probe_step,
    _run_step,
    _write_report,
)
from kbworker.tail_ops import (  # noqa: F401
    _CONTRA_BLANK,
    _CONTRA_STATS,
    _STOP_LOG,
    _SUMMARY_STATS,
    CONTRA_HIGH,
    CONTRA_LOW,
    STOP_CHECK_EVERY,
    StopCheck,
    _canonical_clusters,
    _contradictions,
    _contradictions_all,
    _dedupe,
    _doc_refresh,
    _note_stop,
    _summaries,
)

logger = logging.getLogger("kbworker.consolidate")

ADVISORY_KEY = 42
# DAY-AWARE (review #5 pipe-4, 2026-08-21). The 20 -> 32 raise (review #4 P2)
# assumed ~110 s/pair; the five post-raise nights MEASURED 129-153 s/pair
# (mean ~143, call it ~145), so 32 pairs cost 69-82 min a night — 38-46% of
# the 180-min tail reserve, which the weekday window cannot afford. The cap is
# therefore 20 on deadline days (~30 weekday minutes back) and 48 on unbounded
# days (kbcore.drainsched.is_unbounded_day — the single "box is free tonight"
# definition), where the deeper pass costs nothing anyone is waiting on. Zero
# convergence loss: the streak tolerates revisit gaps — _evidence increments
# off the PRIOR STORED detail, so a pair revisited every other night still
# reaches AUTO_RESOLVE_STREAK, and open_contradiction_rows already cycles
# refreshes oldest-evidence_updated-first. The streak reserve stays
# cap // EVIDENCE_STREAK_RESERVE_DIVISOR; the divisor semantics are unchanged.
EVIDENCE_PAIR_CAP_WEEKDAY = 20  # deadline days: protect the tail reserve
EVIDENCE_PAIR_CAP_UNBOUNDED = 48  # no-deadline days: catch-up capacity
# Explicit override (tests patch this; a knob could set it): None = pick by day.
# Read at call time (_evidence_pair_cap), like the divisor, so a patched cap
# keeps its exact pre-split meaning.
EVIDENCE_PAIR_CAP: int | None = None

# STARVATION FIX. The pair cap // this is reserved, at the FRONT of each
# nightly batch, for rows that already carry a decisive suggestion
# (suggestion_streak >= 1). evidence.open_contradiction_rows orders
# never-enriched rows first — right for a fresh contradiction, fatal for a
# nearly-resolved one: with 57 open contradictions and a cap of 20, twelve
# decisive suggestions sat at streak=1 for weeks because the window never came
# back to them, and AUTO_RESOLVE_STREAK=2 needs exactly one more visit. This
# changes SCHEDULING only — the suggestion thresholds and the two-night
# stability rule (evidence.SUGGEST_* / AUTO_RESOLVE_STREAK) are untouched
# settled design.
EVIDENCE_STREAK_RESERVE_DIVISOR = 2  # half the cap


def _past_deadline() -> bool:
    """True once the configured wall-clock cutoff has passed.

    Checked between cohorts and between ITEMS of the LLM tail, never inside one
    model call: aborting mid-call would throw away work already paid for on the
    GPU, while stopping at an item boundary costs nothing. Every job the drain
    has not reached is still 'pending', so the next run — nightly, or the boot
    catch-up after a power cut — resumes exactly here. Resumption needs no state
    of its own; the queue IS the checkpoint (Invariant 3: ingest is idempotent).
    """
    # Day-aware: weekends run unbounded (kbcore.drainsched — owner 2026-08-02).
    from kbcore.drainsched import effective_deadline

    raw = effective_deadline()
    if not raw:
        return False
    try:
        hh, mm = (int(x) for x in raw.split(":", 1))
        cutoff = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
    except (ValueError, TypeError):
        logger.warning("KB_DRAIN_DEADLINE %r is not HH:MM — ignoring", raw)
        return False
    return datetime.now() >= cutoff


_STOP_CACHE_SECONDS = 5.0
_stop_cache: tuple[float, str | None] | None = None


def reset_stop_cache() -> None:
    """Drop the memoized stand-down verdict (start of a run; tests)."""
    global _stop_cache
    _stop_cache = None


def should_stop() -> str | None:
    """``'deadline'`` / ``'battery'`` / ``None`` — the ONE stand-down question.

    Both guards existed but were asked at exactly ONE site (between drain
    cohorts, first batch exempt), so the LLM TAIL — triage, contradictions,
    evidence, 4.6 h measured — ran unbounded: verified still on the GPU 2h19m
    past the noon deadline. Every long loop now asks this instead of
    re-implementing the pair, and every caller stops the same way: nothing is
    deleted, nothing is half-written, the unreached work is simply left for the
    next run (Invariant 3 makes the retry idempotent).

    The verdict is cached for a few seconds so a per-item check costs a
    dictionary lookup rather than a wall-clock parse plus a SQLite read; neither
    signal can meaningfully change inside one LLM call.
    """
    global _stop_cache
    now = time.monotonic()
    if _stop_cache is not None and now - _stop_cache[0] < _STOP_CACHE_SECONDS:
        return _stop_cache[1]
    reason = "deadline" if _past_deadline() else ("battery" if _on_battery() else None)
    _stop_cache = (now, reason)
    return reason


def _evidence_pair_cap(now: datetime | None = None) -> int:
    """Tonight's evidence-pair cap: the explicit override when set, else day-aware.

    :data:`EVIDENCE_PAIR_CAP` (int) keeps its exact pre-split meaning for tests
    and knobs; None picks :data:`EVIDENCE_PAIR_CAP_UNBOUNDED` on no-deadline days
    and :data:`EVIDENCE_PAIR_CAP_WEEKDAY` otherwise (see the constants' comment).
    ``now`` exists so tests can day-pin the calendar-sensitive branch.
    """
    if EVIDENCE_PAIR_CAP is not None:
        return int(EVIDENCE_PAIR_CAP)
    from kbcore.drainsched import is_unbounded_day

    return EVIDENCE_PAIR_CAP_UNBOUNDED if is_unbounded_day(now) else EVIDENCE_PAIR_CAP_WEEKDAY


def nightly() -> dict[str, Any]:
    """The UNATTENDED entry point (kb-consolidate.service / ``make consolidate``).

    Arms the wall-clock + power guard for the whole pass. It is armed HERE rather
    than inside :func:`main` because standing down is a policy about unattended
    runs: an owner who types ``kb consolidate-now`` at 14:00 asked for the run and
    gets it, while the 03:30 timer — and the ``Persistent=true`` catch-up that
    fires it after a midday boot — is bounded. The drain's between-cohort guard
    is unconditional either way, so no run can keep the GPU busy through a grid
    outage.
    """
    return main(stop=should_stop)


def main(stop: StopCheck | None = None) -> dict[str, Any]:
    """Run the consolidation pass under the advisory lock. Returns a summary dict.

    ``stop`` is the run's stand-down guard (:func:`nightly` passes
    :func:`should_stop`); ``None`` runs the pass to completion, which is what an
    explicitly requested run wants. Whichever steps stand down are recorded in
    ``counts["stopped"]`` — ``{step: {"reason", "deferred"}}`` — so a short run is
    never mistaken for a broken one.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logsafe.install(logsafe.WORKER_MAX_MESSAGE)  # no secret reaches journald, any library
    with advisory_lock(ADVISORY_KEY) as acquired:
        if not acquired:
            logger.info("another consolidation holds lock %d; skipping this run", ADVISORY_KEY)
            return {"skipped": True}

        _STOP_LOG.clear()
        judge_fit.FIT_STATS.update(dict.fromkeys(judge_fit.FIT_STATS, 0))  # per run, in place
        reset_stop_cache()
        run_t0 = time.monotonic()  # wall clock for the digest run line (review #8 H7)
        since = consolidation.last_consolidation_at()
        counts: dict[str, Any] = {
            "queued_drained": 0,
            "queued_items_saved": 0,
            # Review #6: rows the WEEKDAY drain budget shed (weekend catches up).
            "drained_deferred": 0,
            # WP-B2 ensemble observability: pipeline mode this run ran under and
            # the would-be survivors the shadow ensemble reported during the
            # drain (0 unless mode == "shadow" and rows were drained).
            "ensemble_mode": ensemble.mode(),
            "ensemble_shadow_survivors": 0,
            "doc_refresh_resolved": 0,
            "duplicates_marked": 0,
            "review_moot_closed": 0,
            "review_triaged": 0,
            "review_triaged_topup": 0,
            "auto_applied": 0,
            "summaries_written": 0,
            "contradictions_queued": 0,
            "evidence_refreshed": 0,
            "evidence_auto_resolved": 0,
            "auto_resolve_moot": 0,
            "archived": 0,
            "reembedded": 0,
            "review_expired": 0,
            # CURATION VISIBILITY. The report said what automation did and never
            # what it left waiting, so an owner could not see that hundreds of
            # triage verdicts were unapplied, that known-bad items were still
            # live, or that a wave of rows was about to age out. Defaults stand
            # if the step fails, and no existing key is renamed — the report is
            # persisted in consolidation_requests.result and read night-over-night.
            "contradiction_pairs_judged": 0,
            "contradiction_pairs_trimmed": 0,  # KB 55589: oversized prompts fitted
            "contradiction_pairs_no_verdict": 0,  # replies with no verdict: not memorized
            "contradiction_no_verdict_pairs": [],  # ...and which pairs (first 20)
            "contradiction_pairs_resting": 0,  # earlier no-verdicts left out this run
            "triage_awaiting_apply": 0,
            "reject_suggestions_pending": 0,
            "never_triaged_open": 0,
            "contradiction_suggestions_pending": 0,
            "first_expiry_date": None,
        }

        # Step 0: extract remote submissions BEFORE dedupe so the new items take
        # part in this run's dedupe/summary/contradiction passes.
        _run_step(counts, "queued_drained", lambda: _drain_step(counts, stop))
        # Persisted in consolidation_requests.result, so a later run can be
        # compared without re-deriving anything from logs.
        counts.update(_COHORT_STATS)

        # active_projects AFTER the drain so freshly-ingested submissions count.
        projects = consolidation.active_projects()
        # Step 0b BEFORE dedupe: retire stale doc revisions so the revision pair
        # is superseded (correct semantic), never marked 'duplicate'.
        if settings.kb_auto_supersede_doc_refresh:
            _run_step(counts, "doc_refresh_resolved", lambda: _doc_refresh(projects))
        _run_step(counts, "duplicates_marked", lambda: sum(_dedupe(p) for p in projects))
        # WP-1 (owner decision 2026-07-18): sweep review rows made moot by any
        # retirement path — including this run's freshly-marked duplicates, which
        # is why this sits right after the dedupe step. Status flips only.
        _run_step(counts, "review_moot_closed", review_auto.close_moot_rows)
        # WP-2 (owner decision 2026-07-18): the 14B TRIAGES pending unverified
        # extractions — SUGGESTIONS only, stored in review_queue.detail.triage;
        # a human applies them (kb review apply-triage / kb_review_bulk). Runs
        # after the moot sweep so freshly-mooted rows never burn GPU budget.
        # Sanctioned Invariant-8 call site; kill switch: batch knob = 0.
        # WAVE-2: hand triage an INNER SHARE of the tail reserve so this first tail
        # step cannot spend the whole reserve and starve contradictions/evidence
        # behind it (kb_tail_triage_share; 0 / no-deadline / explicit-run = inert =
        # old pooled behaviour). The share's measured budget minutes ride the report.
        reserve = allocator.TailReserve.for_run()
        share = float(settings.kb_tail_triage_share)
        bound = stop is not None  # the share binds only on an unattended, guarded run
        triage_stop = reserve.share_guard(share, stop) if bound else stop
        counts["tail_triage_budget_min"] = reserve.share_budget_minutes(share) if bound else 0.0
        _run_step(
            counts,
            "review_triaged",
            lambda: review_triage.run_triage(drainsched.triage_batch(), should_stop=triage_stop),
        )
        # P0-2: apply the triage verdicts that have COOLED (kbcore.review_apply),
        # right after the step that writes them — status flips only, never a
        # delete (Invariant 5). Stop-gated inside; see kbworker.allocator.
        _run_step(counts, "auto_applied", lambda: tail_steps.auto_apply_step(counts, stop))
        # Review #5 val-3/strat-8: retire the wall-blocked reject-suggestion
        # stock the apply loop would otherwise rescan forever (monotone 395->
        # 1264). Reversible status flip via kbcore.review_rejects — same
        # Invariant-5 posture as the step above; kill switch honored inside.
        _run_step(counts, "wall_blocked_closed", review_rejects.close_wall_blocked_rows)
        # WP-1 quiet-expiry: close never-recalled unverified rows past the age
        # knob (kb_review_expire_days; 0 = kill switch). Item stays searchable.
        # Capped + STOP-GATED inside the step (kbcore.review_expire). MOVED ahead
        # of the LLM tail (review #3 P2, 2026-08-14): scheduled after it, this
        # cheap SQL stood down on 4 of the last 6 deadline-clipped nights. The
        # stop-gate stands — a boot-catch-up run still skips it — and the report
        # key order is unchanged (counts are pre-seeded above).
        _run_step(counts, "review_expired", lambda: tail_steps.quiet_expire_step(counts, stop))
        # Housekeeping for the two non-knowledge stores this system creates.
        # Both sweeps existed with NO call site, so their TTLs were fiction:
        # abandoned upload parts accumulated in the raw volume (which is tarred
        # into every off-site backup) and spent enrolment rows grew forever.
        # Neither touches knowledge, so neither is an Invariant-5 concern.
        _run_step(counts, "upload_parts_swept", documents.sweep_staging)
        _run_step(counts, "enrolment_codes_swept", enrolment.sweep_expired)
        _run_step(counts, "summaries_written", lambda: _summaries(since, stop))
        counts.update(_SUMMARY_STATS)

        def _contra() -> int:
            fitted = judge_fit.FIT_STATS["contradict"]
            try:
                return _contradictions_all(projects, stop)
            finally:  # a step that failed midway still reports what it spent (KB 55589)
                counts["contradiction_pairs_judged"] = _CONTRA_STATS["judged"]
                counts["contradiction_pairs_trimmed"] = judge_fit.FIT_STATS["contradict"] - fitted
                counts["contradiction_pairs_no_verdict"] = _CONTRA_STATS["no_verdict"]
                counts["contradiction_no_verdict_pairs"] = _CONTRA_BLANK[:20]
                counts["contradiction_pairs_resting"] = _CONTRA_STATS["resting"]

        _run_step(counts, "contradictions_queued", _contra)

        # Step 3b AFTER contradictions so rows queued seconds ago get evidence
        # in the same run (open_contradiction_rows orders never-enriched first).
        def _ev() -> int:
            refreshed, auto_resolved, moot = _evidence(stop)
            counts["evidence_auto_resolved"] = auto_resolved
            counts["auto_resolve_moot"] = moot
            counts.update(_EVIDENCE_STATS)  # recency guard: held tonight / held open
            counts.update(_GATE_STATS)  # precision gate: held tonight / open / judge gone
            return refreshed

        _run_step(counts, "evidence_refreshed", _ev)
        _run_step(
            counts,
            "archived",
            lambda: consolidation.archive_stale(
                datetime.now(UTC) - timedelta(days=settings.kb_archive_after_days)
            ),
        )
        _run_step(counts, "reembedded", lambda: tail_steps.reembed_step(stop))

        # AFTER quiet-expiry (which now runs early, before the LLM tail), so the
        # four curation counts describe the queue the owner will actually wake up
        # to (kbcore.review_auto.curation_counts).
        def _curation() -> int:
            counts.update(review_auto.curation_counts(settings.kb_review_expire_days))
            return counts["triage_awaiting_apply"]

        _run_step(counts, "triage_awaiting_apply", _curation)

        # WEEKEND TOP-UP (owner request 2026-08-08), LAST of the working steps
        # and before the report/digest so the count reaches the owner. On a day
        # with no deadline the tail can finish at 19:45 and leave the card idle
        # until 01:30; this keeps triaging in slices until the cutoff knob, an
        # empty queue, or the stand-down guard. Slices, because this run holds
        # the advisory lock: a batch still running at 01:30 makes the NEXT run
        # skip. No-op on a deadline day and whenever the knobs are off.
        _run_step(
            counts,
            "review_triaged_topup",
            lambda: tail_steps.review_triaged_topup(counts, stop),
        )

        # Search-quality regression sentinel (§8): replay the golden eval, record
        # hit@5/MRR, flag a drop vs the previous run. Advisory — never aborts the
        # run. Runs before the counts below so a freshly-enqueued regression row
        # is reflected in review_queue_open.
        eval_sentinel.run_sentinel(counts)

        # WHY the run is as short as it is, and how much it left behind. Rides
        # into consolidation_requests.result with the rest of the report.
        counts["stopped"] = {k: dict(v) for k, v in _STOP_LOG.items()}
        counts["judge_prompts_trimmed"] = dict(judge_fit.FIT_STATS)  # every judge, every step
        # arm/disarm the AC-restore trigger; a run that consumed a pause marker is a
        # RESUME and the digest says so (review #8 H7d — prior payload or None).
        counts["resumed_from_pause"] = record_battery_pause(counts)
        counts["review_queue_open"] = consolidation.open_review_count()
        counts["ingest_queue_pending"] = ingest_queue.queue_depth()
        _run_step(counts, "fleet_degenerate_by_project", fleet_health.degenerate_by_project)
        # ADDED key, never a rename (KB 19663): the same staleness split BY BOX.
        _run_step(counts, "fleet_by_box", fleet_health.fleet_by_box)
        _run_step(counts, "fleet_flush", fleet_flush.fleet_flush_by_box)  # wave S ADDED keys
        _run_step(counts, "fleet_silent", fleet_flush.fleet_silent)
        _run_step(counts, "fleet_stale_hooks", fleet_flush.stale_hook_boxes)
        _run_step(counts, "resend_summary", fleet_flush.resend_summary)
        counts["disk_pct"] = _disk_pct()
        try:  # host memory telemetry (review #5 ops-3): keys absent on failure
            counts["swap_used_pct"], counts["mem_available_gb"] = _host_memory()
        except Exception:  # noqa: BLE001 — telemetry only, never fatal
            logger.exception("host memory telemetry failed")
        counts["extraction_rejection_rate"] = _rejection_rate()
        _rerank_probe_step(counts)  # sidecar health into tonight's meta (review #5)

        counts["run_secs"] = round(time.monotonic() - run_t0, 1)  # ADDED key (H7)
        report_id = _write_report(counts)
        counts["report_item_id"] = report_id
        counts["skipped"] = False
        # P0-2: the only part of the pass an owner sees without going looking.
        counts["digest_pushed"] = tail_steps.push_run_digest(counts)
        logger.info("consolidation done: %s", counts)
        return counts


if __name__ == "__main__":
    print(nightly())

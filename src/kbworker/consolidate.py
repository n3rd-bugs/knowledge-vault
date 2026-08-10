"""kbworker.consolidate — nightly dedupe/summarize/archive, advisory-locked (§7.3).

Takes ``pg_advisory_lock(42)`` for the whole run (ARCH-5); if another run holds
it, logs and exits without work. Runs the six §7.3 steps in order, each isolated
so one failure logs and the rest proceed. Only kbcore service functions touch the
DB (Invariant 2). The 14B extractor is invoked ONLY here, keep_alive=0 (Invariant
8). Run: ``python -m kbworker.consolidate`` (make consolidate).
"""

from __future__ import annotations

import logging
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kbcore import (
    consolidation,
    documents,
    enrolment,
    evidence,
    fleet_health,
    ingest_queue,
    llm,  # noqa: F401 — patch surface: tests patch `consolidate.llm.*`
    review_auto,
    review_triage,
)
from kbcore import reembed as reembed_mod
from kbcore.config import settings
from kbcore.db import advisory_lock
from kbcore.ingest import ingest
from kbcore.review import auto_resolve_contradiction
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
from kbworker.cohort import _COHORT_STATS, _cohort_prepare, _drain_job  # noqa: F401
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
from kbworker.tail_ops import (  # noqa: F401
    _CONTRA_STATS,
    _STOP_LOG,
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
EVIDENCE_PAIR_CAP = 20  # max contradiction pairs evidence-judged per nightly run

# MEANING CHANGED 2026-08-01: this is the ceiling on ingest_queue ROWS one REFILL
# may materialise — a memory bound — and no longer the budget for a whole run.
# It was both, and the two units are not the same thing: a run stopped after 200
# ROWS, but the work is measured in JOBS (a coalesced session of 350 rows is one
# group, a one-row note is also one group), so a night of small submissions
# stopped at 200 rows with hours of window left and a night of one big session
# spent the whole budget on it. The drain now refills until the queue is empty or
# the stand-down guard fires; this only bounds how much is read per refill.
QUEUE_BATCH = 200

# Backstop on the refill loop. Each refill is bounded, but the SUPPLY is not:
# clients can enqueue while the drain runs, and a single-row submission is
# eligible immediately (no quiet period), so "refill until pending is empty"
# could in principle never terminate. The real bound is the deadline/battery
# guard; this exists so a run without one (an owner-requested `main(stop=None)`)
# still ends. At kb_drain_cohort=30 this is 6,000 groups — orders of magnitude
# past what a night's GPU window can extract, so hitting it is a loud anomaly.
MAX_REFILLS = 200

# STARVATION FIX. EVIDENCE_PAIR_CAP // this is reserved, at the FRONT of each
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


def _reading_age_seconds(ts: Any) -> float | None:
    """Age of one sungrow reading in seconds; None when the stamp is unparseable.

    INVARIANT 7 CARVE-OUT — the single foreign-DB boundary in this codebase.
    Everything the KB itself stores is TIMESTAMPTZ/UTC, but the sungrow monitor
    is another project's Django app and writes NAIVE LOCAL wall-clock
    (Asia/Karachi, UTC+5). Reading those stamps as UTC made ``now - seen`` about
    MINUS five hours, so the staleness check could not fire during the first five
    hours of genuine staleness: a poller that died at 03:00 still read as "fresh"
    at 07:00, which is exactly the last-known-good trap the freshness check
    exists to avoid.

    A naive stamp is therefore interpreted in the LOCAL zone — which is what
    :meth:`datetime.astimezone` does to a naive value — and an offset-carrying
    stamp is honoured as written, so a future sungrow schema that starts emitting
    UTC needs no change here. The comparison itself stays UTC-aware.
    """
    try:
        seen = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if seen.tzinfo is None:
        seen = seen.astimezone()  # naive == LOCAL time (Inv. 7 carve-out, above)
    return (datetime.now(UTC) - seen).total_seconds()


def _on_battery() -> bool:
    """True when the house is running on battery, so the drain should stand down.

    The GPU is the largest load on this box and the UPS could not ride out a grid
    fluctuation (2026-07-30, power lost mid-run). Hours of inference during an
    outage is the wrong thing to be doing, so the drain stops between cohorts and
    resumes when mains returns — the same stop-and-resume path as the deadline,
    which needs no state of its own because unreached rows stay 'pending'.

    Signal is grid_voltage from the sungrow monitor DB, read-only. Measured over
    219,304 readings: normal 213-256V, outages cluster below 50V, and the 50-150V
    band holds only ~170 readings — so the threshold sits in a sparse gap rather
    than near either mode.

    FAIL-SAFE ON UNKNOWN-BUT-CONFIGURED STATE. A MISSING or unreadable power DB
    fails OPEN (returns False, warns): the feature is simply not configured on
    that box, and a pipeline that silently never runs is the worse failure there.
    But a DB that IS configured whose newest reading is STALE fails SAFE (returns
    True, ON BATTERY): a reading older than the freshness window right after a
    grid outage is exactly when the sungrow poller is most likely down, so the ONE
    time staleness matters the guard used to say 'mains' and kept the GPU running
    into the outage. A stale value is not evidence the grid is up; on a UPS that
    cannot ride out a fluctuation, pausing the GPU when house-power state is
    unknown-but-configured is the safe default. Both branches warn loudly.
    """
    path = (settings.kb_power_db or "").strip()
    if not path:
        return False
    try:
        import sqlite3

        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as conn:
            rows = conn.execute(
                "SELECT grid_voltage, timestamp FROM monitor_inverterreading "
                "ORDER BY timestamp DESC LIMIT ?",
                (max(1, int(settings.kb_power_window)),),
            ).fetchall()
            row = rows[0] if rows else None
    except Exception as e:  # noqa: BLE001 — another project's DB; never fatal here
        logger.warning(
            "power check unavailable (%s: %s) — proceeding on mains assumption", type(e).__name__, e
        )
        return False
    if not row or row[0] is None:
        logger.warning("power check found no readings — proceeding on mains assumption")
        return False

    volts, ts = float(row[0]), row[1]
    age = _reading_age_seconds(ts)
    if age is None:
        logger.warning("power check: unparseable timestamp %r — ignoring freshness", ts)

    if age is not None and age > settings.kb_power_max_age_seconds:
        logger.warning(
            "power check STALE (%.0fs old, grid=%.1fV) — treating as ON BATTERY and standing "
            "down; the sungrow poller may be down, and a last-known-good value is not evidence "
            "the grid is up. Pausing the GPU is the safe default on a UPS that cannot ride out a "
            "fluctuation; rows stay 'pending' and the next run resumes here",
            age,
            volts,
        )
        return True

    # ANY dip in the window keeps the GPU down: resuming into a flickering supply
    # is worse than staying paused a few minutes longer, and a sagging grid is
    # exactly when the UPS can least absorb a 150W spike.
    floor = min(float(r[0]) for r in rows if r[0] is not None)
    if floor < settings.kb_power_min_grid_volts:
        volts = floor
        logger.warning(
            "ON BATTERY (grid=%.1fV < %.1fV) — drain standing down; rows stay 'pending' "
            "and the next run resumes here",
            volts,
            settings.kb_power_min_grid_volts,
        )
        return True
    return False


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


def _drain_ingest_queue() -> tuple[int, int]:
    """Step 0: extract queued remote submissions — the ONLY place the 14B runs on
    remote content (Invariant 8). Per-group isolation: one bad group logs + continues.

    A REFILL LOOP (2026-08-01), not one poll. The old shape asked
    ``pending_groups`` once for :data:`QUEUE_BATCH` ROWS and processed whatever
    that returned, which conflated two units: the budget was rows, the work is
    jobs. A night of small submissions therefore stopped after 200 one-row groups
    with most of its window unused, while a night holding one 350-row coalesced
    session spent the entire budget on that single session. Each iteration now
    asks for enough groups to fill ONE cohort, processes them through the
    unchanged cohort machinery, and asks again — until the queue is drained, the
    stand-down guard fires, or :data:`MAX_REFILLS` trips.

    PROCESSED GROUPS ARE EXCLUDED FOR THE REST OF THE RUN, and this is load
    bearing rather than an optimisation. A group that DEFERS (extractor/embedder
    outage) is left 'pending' on purpose, and a content failure under the attempts
    cap is left retryable-'failed' — both are still drainable a millisecond
    later, so a naive refill would re-fetch the group it just failed and retry it
    until the deferral/attempts cap retired it, collapsing a policy of "one
    attempt per NIGHT" into "N attempts per run". The keys are tracked here, for
    this run only, and passed to ``pending_groups`` as ``exclude``.

    THREE PASSES PER COHORT (2026-07-29), then ingest job by job. Batching is per
    cohort and never per run: the crash window stays one cohort wide (the same
    exposure the extraction prefetch already had) and only ``kb_drain_cohort``
    plans — each pinning its transcript — are live at once. Ingest and
    ``mark_group_extracted`` deliberately stay per job: batching those would
    widen the pre-existing "written but still pending" window for real.

    The stand-down guard is asked BETWEEN cohorts here — across refills too, so
    the exemption is for the run's FIRST cohort only, not for the first cohort of
    every refill; the check before that first one belongs to the run
    (:func:`_drain_step`), because "should this run start at all" is a question
    about the run, not about a batch. Stopping can land mid-GROUP now that a
    group spans several window jobs — its rows are simply still 'pending', so the
    next run re-extracts the group from its first window and the already-ingested
    items dedupe on (kind, uri, content_hash) (Invariant 3).

    Returns ``(rows_drained, items_saved)``.
    """
    cohort = max(1, int(settings.kb_drain_cohort))
    _COHORT_STATS.clear()
    _COHORT_STATS.update(
        {
            "cohort_size": cohort,
            "cohort_batches": 0,
            "cohort_jobs": 0,
            "cohort_failures": 0,
            "cohort_plans": 0,
            "cohort_plan_failures": 0,
            "cohort_judge_batches": 0,
            "cohort_judge_failures": 0,
            "cohort_eligible": 0,
            "cohort_mode": ensemble.mode(),
            # Refill telemetry (added, never renamed): how many times the drain
            # went back for more, and how many groups it took up in total. A run
            # that did one refill on a deep queue is the old rows-vs-jobs bug
            # coming back, and there would otherwise be no way to see it.
            "cohort_refills": 0,
            "cohort_groups_considered": 0,
            # Per-window checkpoint telemetry (ADDED 2026-08-10, never renamed): windows
            # a resumed group skipped, groups resumed, failed writes (swallowed in cohort).
            "cohort_windows_resumed": 0,
            "cohort_groups_resumed": 0,
            "cohort_checkpoint_failures": 0,
        }
    )
    live = ensemble.mode() == "live"

    drained = 0
    saved_total = 0
    reserve = allocator.TailReserve.for_run()  # window left for the LLM tail
    batches_done = 0  # across ALL refills: only the run's FIRST cohort is exempt
    processed: set[tuple[str, str]] = set()
    stopped = False
    for _refill in range(MAX_REFILLS):
        groups = ingest_queue.pending_groups(
            limit=QUEUE_BATCH, max_groups=cohort, exclude=processed
        )
        # Belt and braces for the exclusion: whatever the SQL returns, a group
        # this run has already touched is never processed twice, so the loop
        # always makes progress and therefore always terminates.
        groups = [g for g in groups if (g["project"], g["group_uri"]) not in processed]
        if not groups:
            break
        processed.update((g["project"], g["group_uri"]) for g in groups)
        _COHORT_STATS["cohort_refills"] += 1
        _COHORT_STATS["cohort_groups_considered"] += len(groups)
        _COHORT_STATS["cohort_eligible"] += len(groups)

        jobs = _assemble_jobs(groups)
        cohorting = cohort > 1 and live and bool(jobs)
        batches = _batch_jobs(jobs, cohort) if cohorting else ([jobs] if jobs else [])

        for n, batch in enumerate(batches):
            # Same exemption as the guard, for the same reason: the run's first
            # cohort is _drain_step's call, not a batch's.
            reason = (should_stop() or reserve.deny_reason(batches[n:])) if batches_done else None
            if reason:
                left = sum(len(b) for b in batches[n:])
                logger.warning(
                    "drain stopping (%s) after %d refill(s): %d cohort(s) / %d job(s) "
                    "deferred — rows stay 'pending' and the next run resumes here",
                    reason,
                    _COHORT_STATS["cohort_refills"],
                    len(batches) - n,
                    left,
                )
                _COHORT_STATS["deadline_deferred_jobs"] = left  # never renamed (telemetry)
                _note_stop("queued_drained", reason, left)
                stopped = True
                break
            batches_done += 1
            t0 = time.monotonic()
            pre_by_key, plan_by_key = _cohort_prepare(batch) if cohorting else ({}, {})
            for job in batch:
                rows, saved = _drain_job(
                    job,
                    pre_extracted=pre_by_key.get(job["key"]),
                    planned=plan_by_key.get(job["key"]),
                )
                drained += rows
                saved_total += saved
            reserve.record(len(batch), time.monotonic() - t0)  # this run's s/job
        if stopped:
            break
    else:
        logger.warning(
            "drain hit the %d-refill backstop with work still pending — the queue is "
            "being filled at least as fast as it drains",
            MAX_REFILLS,
        )
    return drained, saved_total


def _evidence_batch(
    rows: list[tuple[int, int, int, dict[str, Any]]],
) -> list[tuple[int, int, int, dict[str, Any]]]:
    """Cap the nightly evidence batch, RESERVING a slice for near-decided rows.

    A row whose last refresh produced a suggestion (``suggestion_streak >= 1``)
    is ONE more visit away from auto-resolution, but it sorts LAST — the query
    puts never-enriched rows first — so a cap smaller than the open set starved
    it forever (12 suggestions stuck at streak=1 against 57 open rows and a cap
    of 20). Up to ``EVIDENCE_PAIR_CAP // EVIDENCE_STREAK_RESERVE_DIVISOR`` of
    those go to the FRONT of the batch, so the stop guard cannot truncate them
    away either; the rest of the cap keeps the query's never-enriched-first
    order, which is what stops brand-new contradictions being starved in turn.
    Both constants are read at call time so a test (or a future knob) can lower
    the cap and have the reserve follow it.
    """
    cap = EVIDENCE_PAIR_CAP
    reserve = cap // EVIDENCE_STREAK_RESERVE_DIVISOR
    hot = [r for r in rows if r[3].get("suggested_resolution") and r[3].get("suggestion_streak")]
    batch = hot[:reserve]
    picked = {r[0] for r in batch}
    for row in rows:
        if len(batch) >= cap:
            break
        if row[0] not in picked:
            batch.append(row)
    return batch


def _evidence(stop: StopCheck | None = None) -> tuple[int, int]:
    """Step 3b: refresh evidence for open contradictions; auto-resolve stable ones.

    Counts accumulate night over night as repo seeds and session flushes add
    corroborating items; never-enriched rows come first (evidence.py ordering)
    so the cap can't starve brand-new contradictions. A decisive suggestion
    that holds for AUTO_RESOLVE_STREAK consecutive refreshes is executed
    (owner V4 refinement; kill switch kb_auto_resolve_evidence). Per-pair
    isolation. ``stop`` (asked every :data:`STOP_CHECK_EVERY` pairs) bounds the
    step by wall clock/power as well as by EVIDENCE_PAIR_CAP; an unreached row
    is just a row whose evidence is one night older. Returns
    ``(refreshed, auto_resolved)``.
    """
    rows = evidence.open_contradiction_rows()
    refreshed = 0
    auto_resolved = 0
    batch = _evidence_batch(rows)
    for n, (queue_id, a_id, b_id, prior) in enumerate(batch):
        if stop is not None and n % STOP_CHECK_EVERY == 0 and (reason := stop()):
            _note_stop("evidence_refreshed", reason, len(batch) - n)
            break
        try:
            detail = evidence.gather_evidence(a_id, b_id)
            sugg = detail.get("suggested_resolution")
            if sugg:
                prior_sugg = prior.get("suggested_resolution") or {}
                same = prior_sugg.get("keep") == sugg["keep"] and prior_sugg.get(
                    "retire"
                ) == sugg.get("retire")
                detail["suggestion_streak"] = prior.get("suggestion_streak", 0) + 1 if same else 1
            else:
                detail["suggestion_streak"] = 0  # overwrite any stale streak
            if evidence.update_review_detail(queue_id, detail):
                refreshed += 1
            if (
                sugg
                and settings.kb_auto_resolve_evidence
                and detail["suggestion_streak"] >= evidence.AUTO_RESOLVE_STREAK
            ):
                auto_resolve_contradiction(queue_id, sugg["keep"], sugg["retire"])
                auto_resolved += 1
                logger.info(
                    "auto-resolved contradiction row %s: keep %s, retire %s (%s, streak %d)",
                    queue_id,
                    sugg["keep"],
                    sugg["retire"],
                    sugg["rule"],
                    detail["suggestion_streak"],
                )
        except Exception:  # noqa: BLE001 — one bad pair must not abort the step
            logger.exception("evidence refresh for queue row %s failed", queue_id)
    if len(rows) > len(batch):
        logger.info(
            "evidence refresh capped at %d pairs; %d open pairs deferred to the next run",
            len(batch),
            len(rows) - len(batch),
        )
    return refreshed, auto_resolved


def _disk_pct() -> float:
    """Percent used on the filesystem holding KB_RAW."""
    p = settings.raw
    while not p.exists() and p != p.parent:
        p = p.parent
    usage = shutil.disk_usage(p if p.exists() else Path("/"))
    return round(usage.used / usage.total * 100, 1)


def _rejection_rate() -> float | None:
    rejected, saved = consolidation.extraction_stats()
    total = rejected + saved
    return round(rejected / total, 3) if total else None


def _run_step(counts: dict[str, Any], key: str, fn) -> None:
    """Run one step, isolating failure: log and leave the count at 0.

    Also TIMES it. A pass ran for over an hour producing nothing, and there was
    no way to say which step consumed it without guessing — the same blind spot
    that hid a 40% CPU fallback and an inert cohort path. Durations land in the
    run report (counts["step_secs"]), which is persisted in
    consolidation_requests.result, so "where did the hour go" is answerable from
    data on the next run rather than by instrumenting after the fact.
    """
    t0 = time.monotonic()
    try:
        counts[key] = fn()
    except Exception:  # noqa: BLE001 — one step must not abort the pass
        logger.exception("consolidation step %r failed", key)
    finally:
        secs = round(time.monotonic() - t0, 1)
        counts.setdefault("step_secs", {})[key] = secs
        # WARNING so it survives the unit's default level: an INFO line here was
        # invisible in journald and cost hours of archaeology.
        if secs >= 60:
            logger.warning("consolidation step %r took %.0fs", key, secs)


def _write_report(counts: dict[str, Any]) -> int:
    ts = datetime.now(UTC)
    body = "Nightly consolidation report\n\n" + "\n".join(f"- {k}: {v}" for k, v in counts.items())
    return ingest(
        "kb-system",
        "report",
        f"Consolidation report {ts.date().isoformat()}",
        body,
        kind="chat",
        uri=f"report:{ts.isoformat()}",
        trust="verified",
        meta={"consolidation": True, **counts},
    )


def _drain_step(counts: dict[str, Any], stop: StopCheck | None) -> int:
    """Step 0, with the stand-down guard asked BEFORE the first cohort.

    BOOT-CATCH-UP CONSEQUENCE, and it is the intended behaviour: the timer is
    ``Persistent=true``, so a box that boots at 14:00 after a power cut fires the
    missed 03:30 run immediately — already hours past the noon deadline. Asking
    the guard before the first cohort means such a run DEFERS the whole drain to
    the next nightly instead of putting the 14B on the GPU in the middle of the
    working day, which is exactly what the deadline exists to prevent. Nothing is
    lost: the rows stay 'pending' and ingest is idempotent (Invariant 3).
    """
    ensemble.reset_shadow_counter()  # per-run shadow accumulator (WP-B2)
    reason = stop() if stop is not None else None
    if reason:
        # The long-lived watcher runs main() repeatedly in ONE process; leaving
        # the previous run's cohort telemetry standing would report work this
        # run never did.
        _COHORT_STATS.clear()
        _note_stop("queued_drained", reason, ingest_queue.queue_depth())
        return 0
    drained, saved = _drain_ingest_queue()
    counts["queued_items_saved"] = saved
    counts["ensemble_shadow_survivors"] = ensemble.shadow_survivor_total()
    return drained


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
    with advisory_lock(ADVISORY_KEY) as acquired:
        if not acquired:
            logger.info("another consolidation holds lock %d; skipping this run", ADVISORY_KEY)
            return {"skipped": True}

        _STOP_LOG.clear()
        reset_stop_cache()
        since = consolidation.last_consolidation_at()
        counts: dict[str, Any] = {
            "queued_drained": 0,
            "queued_items_saved": 0,
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
            lambda: review_triage.run_triage(
                settings.kb_review_triage_batch, should_stop=triage_stop
            ),
        )
        # P0-2: apply the triage verdicts that have COOLED (kbcore.review_apply),
        # right after the step that writes them — status flips only, never a
        # delete (Invariant 5). Stop-gated inside; see kbworker.allocator.
        _run_step(counts, "auto_applied", lambda: tail_steps.auto_apply_step(counts, stop))
        # Housekeeping for the two non-knowledge stores this system creates.
        # Both sweeps existed with NO call site, so their TTLs were fiction:
        # abandoned upload parts accumulated in the raw volume (which is tarred
        # into every off-site backup) and spent enrolment rows grew forever.
        # Neither touches knowledge, so neither is an Invariant-5 concern.
        _run_step(counts, "upload_parts_swept", documents.sweep_staging)
        _run_step(counts, "enrolment_codes_swept", enrolment.sweep_expired)
        _run_step(counts, "summaries_written", lambda: _summaries(since, stop))

        def _contra() -> int:
            queued = _contradictions_all(projects, stop)
            counts["contradiction_pairs_judged"] = _CONTRA_STATS["judged"]
            return queued

        _run_step(counts, "contradictions_queued", _contra)

        # Step 3b AFTER contradictions so rows queued seconds ago get evidence
        # in the same run (open_contradiction_rows orders never-enriched first).
        def _ev() -> int:
            refreshed, auto_resolved = _evidence(stop)
            counts["evidence_auto_resolved"] = auto_resolved
            return refreshed

        _run_step(counts, "evidence_refreshed", _ev)
        _run_step(
            counts,
            "archived",
            lambda: consolidation.archive_stale(
                datetime.now(UTC) - timedelta(days=settings.kb_archive_after_days)
            ),
        )
        _run_step(counts, "reembedded", reembed_mod.reembed)
        # WP-1 quiet-expiry: close never-recalled unverified rows past the age
        # knob (kb_review_expire_days; 0 = kill switch). Item stays searchable.
        # Capped + STOP-GATED inside the step since 2026-08-08 (kbcore.review_expire).
        _run_step(counts, "review_expired", lambda: tail_steps.quiet_expire_step(counts, stop))

        # AFTER quiet-expiry, so the four curation counts describe the queue the
        # owner will actually wake up to (kbcore.review_auto.curation_counts).
        def _curation() -> int:
            counts.update(review_auto.curation_counts(settings.kb_review_expire_days))
            return counts["triage_awaiting_apply"]

        _run_step(counts, "triage_awaiting_apply", _curation)

        # WEEKEND TOP-UP (owner request 2026-08-08), LAST of the working steps
        # and before the report/digest so the count reaches the owner. On a day
        # with no deadline the tail can finish at 19:45 and leave the card idle
        # until 01:00; this keeps triaging in slices until the cutoff knob, an
        # empty queue, or the stand-down guard. Slices, because this run holds
        # the advisory lock: a batch still running at 01:00 makes the NEXT run
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
        counts["review_queue_open"] = consolidation.open_review_count()
        counts["ingest_queue_pending"] = ingest_queue.queue_depth()
        _run_step(counts, "fleet_degenerate_by_project", fleet_health.degenerate_by_project)
        counts["disk_pct"] = _disk_pct()
        counts["extraction_rejection_rate"] = _rejection_rate()

        report_id = _write_report(counts)
        counts["report_item_id"] = report_id
        counts["skipped"] = False
        # P0-2: the only part of the pass an owner sees without going looking.
        counts["digest_pushed"] = tail_steps.push_run_digest(counts)
        logger.info("consolidation done: %s", counts)
        return counts


if __name__ == "__main__":
    print(nightly())

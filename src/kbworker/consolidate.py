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
from typing import TYPE_CHECKING, Any

from kbcore import (
    consolidation,
    documents,
    enrolment,
    evidence,
    ingest_queue,
    llm,
    review_auto,
    review_triage,
)
from kbcore import reembed as reembed_mod
from kbcore.config import settings
from kbcore.db import advisory_lock
from kbcore.errors import EmbedderUnavailable, ExtractionFailed, ExtractorUnavailable
from kbcore.ingest import ingest
from kbcore.review import auto_resolve_contradiction, close_contradiction_rows_for
from kbcore.supersede import same_uri_active_pairs, supersede
from kbworker import ensemble, eval_sentinel, sessions

if TYPE_CHECKING:
    from kbworker.judging import EnsemblePlan

logger = logging.getLogger("kbworker.consolidate")

ADVISORY_KEY = 42
CONTRA_LOW, CONTRA_HIGH = 0.80, 0.95
EVIDENCE_PAIR_CAP = 20  # max contradiction pairs evidence-judged per nightly run
QUEUE_BATCH = 200  # max ingest_queue rows drained per nightly run


def _canonical_clusters(pairs: list[tuple[int, int, float]]) -> list[set[int]]:
    """Union-find the dedupe pairs into clusters of mutually-similar items."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    for a, b, _ in pairs:
        union(a, b)
    clusters: dict[int, set[int]] = {}
    for node in parent:
        clusters.setdefault(find(node), set()).add(node)
    return [c for c in clusters.values() if len(c) > 1]


# Cohort telemetry for the run report. The FIRST attempt at cohorting shipped
# with only a logger.info, and when throughput did not improve there was no way
# to answer "did it even run?" without grepping journald — the same blind spot
# that let 40% of embeddings run on CPU unnoticed. An optimisation you cannot
# observe is an optimisation you cannot trust.
_COHORT_STATS: dict[str, int] = {}


def _assemble_jobs(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One JOB per coalesced group, with its text assembled EXACTLY ONCE.

    COALESCED BY SESSION. The network path windows RAW JSONL at 900 KB while the
    local path assembles to text first, so the same session arrives as ~42x more
    rows over MCP/REST (measured: 114 MB of JSONL is 2.7 MB of conversation —
    350 rows vs 9 for a 300 MB session). Extracting each raw window separately
    spent the whole 200-row nightly budget on one session and fed the 14B mostly
    JSON scaffolding. Concatenating a session's rows and assembling ONCE restores
    the local path's behaviour. Row order is the hook's byte order (it submits
    sequential offsets), so plain concatenation reconstitutes the delta.

    The cohort phases and the ingest pass now share this ONE assembly, so the
    text a judge saw is provably the text stored as the item's ``raw`` (it used
    to be assembled twice, in two places). Per-group isolation as everywhere else
    in the drain: a group that will not assemble is marked failed, never fatal.
    """
    jobs: list[dict[str, Any]] = []
    for group in groups:
        rows = group["rows"]
        row = rows[0]
        ids = [r["id"] for r in rows]
        try:
            meta = row["meta"] or {}
            text = sessions.assemble_text("".join(r["content"] for r in rows), row["kind"])
            jobs.append(
                {
                    "key": group["uri"],  # cohort key: the coalescing group id
                    "ids": ids,
                    "project": row["project"],
                    "uri": meta.get("source_uri") or f"queue:{row['kind']}:{row['id']}",
                    "branch": meta.get("branch"),
                    "text": text,
                    # FIX 6 (audit 2026-07-26): a queued session/memory window
                    # anchors no FILE — repo/commit ride the item meta as
                    # provenance instead of a phantom {"path": "."} code_ref.
                    "detail": {
                        "ingest_queue_id": row["id"],
                        "source": row["kind"],
                        **({"coalesced_rows": len(ids)} if len(ids) > 1 else {}),
                    },
                    "extra_meta": sessions._session_provenance(
                        meta.get("repo"), meta.get("commit")
                    ),
                }
            )
        except Exception as e:  # noqa: BLE001 — one bad submission must not abort the drain
            logger.exception("assembling ingest_queue group %s failed", ids)
            for rid in ids:
                ingest_queue.mark_failed(rid, f"{type(e).__name__}: {e}")
    return jobs


def _past_deadline() -> bool:
    """True once the configured wall-clock cutoff has passed.

    Checked BETWEEN cohorts, never inside one: aborting mid-cohort would throw
    away extraction work already paid for on the GPU, while stopping at a cohort
    boundary costs nothing. Every job the drain has not reached is still
    'pending', so the next run — nightly, or the boot catch-up after a power cut
    — resumes exactly here. Resumption needs no state of its own; the queue IS
    the checkpoint (Invariant 3: ingest is idempotent).
    """
    raw = (settings.kb_drain_deadline or "").strip()
    if not raw:
        return False
    try:
        hh, mm = (int(x) for x in raw.split(":", 1))
        cutoff = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
    except (ValueError, TypeError):
        logger.warning("KB_DRAIN_DEADLINE %r is not HH:MM — ignoring", raw)
        return False
    return datetime.now() >= cutoff


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

    FAILS OPEN, loudly. If the DB is missing, unreadable, or its newest reading is
    STALE, this returns False and warns. A stale row is not evidence that the grid
    is up, but a pipeline that silently never runs because another project's
    poller died is the worse failure — and the drain is already deadline-bounded.
    Staleness is checked explicitly: trusting a last-known-good value is exactly
    how you flatten a battery through an outage.
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
    age = None
    try:
        seen = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - seen).total_seconds()
    except (ValueError, TypeError):
        logger.warning("power check: unparseable timestamp %r — ignoring freshness", ts)

    if age is not None and age > settings.kb_power_max_age_seconds:
        logger.warning(
            "power check STALE (%.0fs old, grid=%.1fV) — proceeding on mains assumption; "
            "the sungrow poller may be down",
            age,
            volts,
        )
        return False

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


def _batch_jobs(jobs: list[dict[str, Any]], cohort: int) -> list[list[dict[str, Any]]]:
    """Slice jobs into cohort batches, bounded by BOTH job count and total bytes.

    The count is the throughput bound (per-job cost is dominated by model loading,
    so jobs/hour is what sizes a cohort). The byte budget is purely an OOM GUARD:
    a cohort holds every job's assembled text in memory at once, so one unusually
    large transcript must not be able to make a full-sized cohort exhaust RAM.

    A job larger than the whole budget is still taken, ALONE. Skipping it would
    return an empty batch and the drain would make no progress — one oversized row
    would wedge the queue permanently, which is a worse failure than a big cohort.
    """
    budget = max(1, int(settings.kb_drain_cohort_bytes))
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for job in jobs:
        size = len(job.get("text") or "")
        if current and (len(current) >= cohort or used + size > budget):
            batches.append(current)
            current, used = [], 0
        if not current and size > budget:
            logger.warning(
                "cohort: job %r is %.1f MB, over the %.1f MB budget — taken alone",
                job.get("key"),
                size / 1e6,
                budget / 1e6,
            )
        current.append(job)
        used += size
    if current:
        batches.append(current)
    return batches


def _cohort_prepare(
    batch: list[dict[str, Any]],
) -> tuple[dict[str, dict], dict[str, EnsemblePlan]]:
    """The three MODEL-MAJOR phases for one cohort batch, before any ingest.

    Phase 1 EXTRACT (``prefetch_extractions``): every extractor, model-major
    across the batch. Phase 2 PLAN (``plan_ensemble``): gate + cluster + policy,
    which touches only the EMBEDDER. Phase 3 JUDGE (``judge_cohort``): every
    job's judgements grouped by judge model, so each judge loads ONCE for the
    whole batch instead of once per job. Measured 2026-07-29: the drain issued 50
    model loads for 37 chat calls because judging alternated per job.

    Returns ``({key: pre_extracted}, {key: EnsemblePlan})``. EVERY phase is
    swallow-and-count: a job with no plan simply falls back to the per-job path,
    which is exactly today's behaviour. Cohorting is an optimisation, and an
    optimisation that can break the drain is not one.
    """
    pre_by_key: dict[str, dict] = {}
    plan_by_key: dict[str, EnsemblePlan] = {}
    try:
        texts = [job["text"] for job in batch]
        for job, pre in zip(batch, ensemble.prefetch_extractions(texts), strict=True):
            pre_by_key[job["key"]] = pre
        _COHORT_STATS["cohort_batches"] += 1
        _COHORT_STATS["cohort_jobs"] += len(batch)
        logger.warning(
            "cohort prefetch: batch %d, %d job(s) — each extractor loaded once",
            _COHORT_STATS["cohort_batches"],
            len(batch),
        )
    except Exception:  # noqa: BLE001 — never let an optimisation fail the drain
        _COHORT_STATS["cohort_failures"] += 1
        logger.warning("cohort prefetch failed; falling back per job", exc_info=True)
        return pre_by_key, plan_by_key

    for job in batch:
        pre = pre_by_key.get(job["key"])
        if pre is None:
            continue  # no prefetch for this job — the per-job path owns it
        try:
            plan_by_key[job["key"]] = ensemble.plan_ensemble(
                job["text"],
                job["project"],
                job["uri"],
                code_ref=None,
                branch=job["branch"],
                extra_detail=job["detail"],
                pre_extracted=pre,
            )
        except Exception as e:  # noqa: BLE001 — one unplannable job falls back, alone
            _COHORT_STATS["cohort_plan_failures"] += 1
            logger.warning(
                "cohort plan failed for %s (%s: %s); falling back per job",
                job["uri"],
                type(e).__name__,
                e,
            )
    _COHORT_STATS["cohort_plans"] += len(plan_by_key)

    if plan_by_key:
        try:
            ensemble.judge_cohort(list(plan_by_key.values()))
            _COHORT_STATS["cohort_judge_batches"] += 1
        except Exception:  # noqa: BLE001 — never let an optimisation fail the drain
            # Plans are mutated in place, so a half-judged plan would ingest
            # UNJUDGED representatives as if approved. Drop them all: the per-job
            # path re-judges from scratch, which is today's behaviour.
            _COHORT_STATS["cohort_judge_failures"] += 1
            logger.warning("cohort judging failed; falling back per job", exc_info=True)
            plan_by_key.clear()
    return pre_by_key, plan_by_key


def _drain_job(
    job: dict[str, Any], *, pre_extracted: dict | None, planned: EnsemblePlan | None
) -> tuple[int, int]:
    """Extract+ingest ONE coalesced group. Owns the queue self-heal taxonomy.

    Returns ``(rows_drained, items_saved)`` — ``(0, 0)`` for any failure, which is
    isolated here so one bad submission never aborts the drain.
    """
    ids = job["ids"]
    try:
        saved, rejected = sessions.extract_and_ingest(
            job["text"],
            job["project"],
            job["uri"],
            pre_extracted=pre_extracted,
            planned=planned,
            code_ref=None,
            branch=job["branch"],
            extra_detail=job["detail"],
            extra_meta=job["extra_meta"],
        )
        # All-or-nothing: a group extracted once must never leave rows
        # pending for a second, partial extraction of the same session.
        ingest_queue.mark_group_extracted(ids, saved, rejected)
        return len(ids), saved
    except (ExtractorUnavailable, EmbedderUnavailable) as e:
        # CONNECTIVITY (Ollama down/restarting/model-not-loaded during the
        # nightly window — including a judge that went dark mid-cohort, which
        # leaves fail-closed representatives without a verdict): do NOT
        # mark_failed — leave the row 'pending' with attempts UNCHANGED so an
        # outage never burns a row's retry budget; the next run retries once the
        # model is back. mark_deferred bumps a bounded deferral counter and, if a
        # "transient" failure never heals (a payload-deterministic 5xx /
        # mid-response crash), escalates the row to 'failed' so it can't re-drain
        # every night forever (review finding). Every row in the group, not just
        # the first: deferring one and leaving the rest pending would split the
        # session and extract it in pieces on the next run — the exact thing
        # coalescing prevents.
        escalated = [
            rid for rid in ids if ingest_queue.mark_deferred(rid, f"{type(e).__name__}: {e}")
        ]
        if escalated:
            logger.error(
                "rows %s escalated to failed after %d deferrals (transient never healed): %s",
                escalated,
                ingest_queue.DEFERRAL_CAP,
                e,
            )
        else:
            logger.warning("%d row(s) deferred (extractor/embedder unavailable): %s", len(ids), e)
    except ExtractionFailed as e:
        # CONTENT-DETERMINISTIC (unparseable output, unterminated <think>, a
        # per-row read timeout, a 4xx): re-running the 14B on this exact row
        # fails identically, so bound it with the attempts cap instead of
        # retrying every nightly run forever (queue self-heal taxonomy).
        logger.warning("group %s extraction failed (content): %s", ids, e)
        for rid in ids:
            ingest_queue.mark_failed(rid, f"{type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001 — one bad submission must not abort the drain
        logger.exception("draining ingest_queue group %s failed", ids)
        for rid in ids:
            ingest_queue.mark_failed(rid, f"{type(e).__name__}: {e}")
    return 0, 0


def _drain_ingest_queue() -> tuple[int, int]:
    """Step 0: extract queued remote submissions — the ONLY place the 14B runs on
    remote content (Invariant 8). Per-group isolation: one bad group logs + continues.

    THREE PASSES PER COHORT (2026-07-29), then ingest job by job. Batching is per
    cohort and never per run: the crash window stays one cohort wide (the same
    exposure the extraction prefetch already had) and only ``kb_drain_cohort``
    plans — each pinning its transcript — are live at once. Ingest and
    ``mark_group_extracted`` deliberately stay per job: batching those would
    widen the pre-existing "written but still pending" window for real.

    Returns ``(rows_drained, items_saved)``.
    """
    groups = ingest_queue.pending_groups(limit=QUEUE_BATCH)
    jobs = _assemble_jobs(groups)

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
            "cohort_eligible": len(groups),
            "cohort_mode": ensemble.mode(),
        }
    )
    cohorting = cohort > 1 and ensemble.mode() == "live" and bool(jobs)
    batches = _batch_jobs(jobs, cohort) if cohorting else [jobs]

    drained = 0
    saved_total = 0
    for n, batch in enumerate(batches):
        if n and (_past_deadline() or _on_battery()):
            left = sum(len(b) for b in batches[n:])
            logger.warning(
                "drain stopping (deadline %s or on battery): %d cohort(s) / %d job(s) deferred — "
                "rows stay 'pending' and the next run resumes here",
                settings.kb_drain_deadline,
                len(batches) - n,
                left,
            )
            _COHORT_STATS["deadline_deferred_jobs"] = left
            break
        pre_by_key, plan_by_key = _cohort_prepare(batch) if cohorting else ({}, {})
        for job in batch:
            rows, saved = _drain_job(
                job,
                pre_extracted=pre_by_key.get(job["key"]),
                planned=plan_by_key.get(job["key"]),
            )
            drained += rows
            saved_total += saved
    return drained, saved_total


def _dedupe(project: str) -> int:
    """Step 1: collapse near-dupes to one canonical (oldest verified, else oldest)."""
    pairs = consolidation.duplicate_pairs(project, settings.kb_dup_threshold)
    marked = 0
    for cluster in _canonical_clusters(pairs):
        ranks = consolidation.item_ranks(list(cluster))
        canonical = min(cluster, key=lambda i: ranks[i])
        for dup in cluster:
            if dup != canonical and consolidation.mark_duplicate(dup, canonical):
                marked += 1
    return marked


def _summaries(since: datetime | None) -> int:
    """Step 2: (re)write topic summaries for tags with >=3 new items since last run."""
    written = 0
    for project, tag, _new in consolidation.tags_needing_summary(since):
        sources = consolidation.active_items_with_tag(project, tag)
        if len(sources) < 3:
            continue
        body = llm.summarize_items(tag, sources)
        if not body.strip():
            continue
        consolidation.create_summary(project, tag, body, [s["id"] for s in sources])
        written += 1
    return written


def _contradictions(project: str) -> int:
    """Step 3: judge near-but-not-dup pairs; queue confirmed conflicts (never auto-supersede)."""
    queued = 0
    for a_id, b_id, _sim in consolidation.contradiction_pairs(project, CONTRA_LOW, CONTRA_HIGH):
        (a_title, a_body), (b_title, b_body) = consolidation.item_pair_texts(a_id, b_id)
        if llm.items_contradict(
            a_title, a_body, b_title, b_body
        ) and consolidation.record_contradiction(a_id, b_id):
            queued += 1
    return queued


def _doc_refresh(projects: list[str]) -> int:
    """Step 0b: retire stale doc revisions + close their moot contradiction rows.

    Backlog counterpart of the scan-time hook (supersede_doc_refresh): any
    same-uri active pairs that predate the hook get resolved here — BEFORE
    dedupe (which would mislabel a revision pair 'duplicate') and before the
    contradiction scan wastes LLM calls on them. Per-pair isolation.
    """
    resolved = 0
    for project in projects:
        for stale, newest in same_uri_active_pairs(project):
            try:
                supersede(stale, new_item_id=newest, reason="auto: newer revision of same document")
                close_contradiction_rows_for(stale, newest)
                resolved += 1
            except Exception:  # noqa: BLE001 — one bad pair must not abort the sweep
                logger.exception("doc-refresh sweep failed for %s -> %s", stale, newest)
    return resolved


def _evidence() -> tuple[int, int]:
    """Step 3b: refresh evidence for open contradictions; auto-resolve stable ones.

    Counts accumulate night over night as repo seeds and session flushes add
    corroborating items; never-enriched rows come first (evidence.py ordering)
    so the cap can't starve brand-new contradictions. A decisive suggestion
    that holds for AUTO_RESOLVE_STREAK consecutive refreshes is executed
    (owner V4 refinement; kill switch kb_auto_resolve_evidence). Per-pair
    isolation. Returns ``(refreshed, auto_resolved)``.
    """
    rows = evidence.open_contradiction_rows()
    refreshed = 0
    auto_resolved = 0
    for queue_id, a_id, b_id, prior in rows[:EVIDENCE_PAIR_CAP]:
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
    if len(rows) > EVIDENCE_PAIR_CAP:
        logger.info(
            "evidence refresh capped at %d pairs; %d open pairs deferred to the next run",
            EVIDENCE_PAIR_CAP,
            len(rows) - EVIDENCE_PAIR_CAP,
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


def main() -> dict[str, Any]:
    """Run the consolidation pass under the advisory lock. Returns a summary dict."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with advisory_lock(ADVISORY_KEY) as acquired:
        if not acquired:
            logger.info("another consolidation holds lock %d; skipping this run", ADVISORY_KEY)
            return {"skipped": True}

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
            "summaries_written": 0,
            "contradictions_queued": 0,
            "evidence_refreshed": 0,
            "evidence_auto_resolved": 0,
            "archived": 0,
            "reembedded": 0,
            "review_expired": 0,
        }

        # Step 0: extract remote submissions BEFORE dedupe so the new items take
        # part in this run's dedupe/summary/contradiction passes.
        def _drain() -> int:
            ensemble.reset_shadow_counter()  # per-run shadow accumulator (WP-B2)
            drained, saved = _drain_ingest_queue()
            counts["queued_items_saved"] = saved
            counts["ensemble_shadow_survivors"] = ensemble.shadow_survivor_total()
            return drained

        _run_step(counts, "queued_drained", _drain)
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
        _run_step(
            counts,
            "review_triaged",
            lambda: review_triage.run_triage(settings.kb_review_triage_batch),
        )
        # Housekeeping for the two non-knowledge stores this system creates.
        # Both sweeps existed with NO call site, so their TTLs were fiction:
        # abandoned upload parts accumulated in the raw volume (which is tarred
        # into every off-site backup) and spent enrolment rows grew forever.
        # Neither touches knowledge, so neither is an Invariant-5 concern.
        _run_step(counts, "upload_parts_swept", documents.sweep_staging)
        _run_step(counts, "enrolment_codes_swept", enrolment.sweep_expired)
        _run_step(counts, "summaries_written", lambda: _summaries(since))
        _run_step(
            counts, "contradictions_queued", lambda: sum(_contradictions(p) for p in projects)
        )

        # Step 3b AFTER contradictions so rows queued seconds ago get evidence
        # in the same run (open_contradiction_rows orders never-enriched first).
        def _ev() -> int:
            refreshed, auto_resolved = _evidence()
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
        _run_step(
            counts,
            "review_expired",
            lambda: review_auto.expire_quiet_rows(settings.kb_review_expire_days),
        )

        # Search-quality regression sentinel (§8): replay the golden eval, record
        # hit@5/MRR, flag a drop vs the previous run. Advisory — never aborts the
        # run. Runs before the counts below so a freshly-enqueued regression row
        # is reflected in review_queue_open.
        eval_sentinel.run_sentinel(counts)

        counts["review_queue_open"] = consolidation.open_review_count()
        counts["ingest_queue_pending"] = ingest_queue.queue_depth()
        counts["disk_pct"] = _disk_pct()
        counts["extraction_rejection_rate"] = _rejection_rate()

        report_id = _write_report(counts)
        counts["report_item_id"] = report_id
        counts["skipped"] = False
        logger.info("consolidation done: %s", counts)
        return counts


if __name__ == "__main__":
    print(main())

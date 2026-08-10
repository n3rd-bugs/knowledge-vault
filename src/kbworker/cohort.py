"""kbworker.cohort — the three model-major phases and per-job drain of one cohort.

Split from ``kbworker.consolidate`` (mechanical extraction, 2026-08-04). The
refill loop that feeds this stays in ``consolidate`` (it is the patched test
surface); this module owns what happens to ONE batch: prefetch/plan/judge, then
extract+ingest job by job with the queue self-heal taxonomy.

``_COHORT_STATS`` lives here and is shared BY REFERENCE with ``consolidate``
(same dict object; both sides mutate in place, nobody rebinds). Same logger name
as the parent on purpose — journald grep recipes depend on it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kbcore import ingest_queue, jobs
from kbcore.errors import EmbedderUnavailable, ExtractionFailed, ExtractorUnavailable
from kbworker import ensemble, sessions

if TYPE_CHECKING:
    from kbworker.judging import EnsemblePlan

logger = logging.getLogger("kbworker.consolidate")

# Cohort telemetry for the run report. The FIRST attempt at cohorting shipped
# with only a logger.info, and when throughput did not improve there was no way
# to answer "did it even run?" without grepping journald — the same blind spot
# that let 40% of embeddings run on CPU unnoticed. An optimisation you cannot
# observe is an optimisation you cannot trust.
_COHORT_STATS: dict[str, int] = {}


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


def _record_window(state: dict[str, Any], job: dict[str, Any]) -> None:
    """Persist "this window is extracted" on the group's first queue row.

    ONE cheap UPDATE against a multi-minute extraction call, written after EACH
    window rather than once at the end, because what it protects against is the
    run ENDING part-way through a group — batching it would checkpoint only the
    groups that never needed one.

    Swallow-and-count, like every other optimisation in this module: bookkeeping
    must never be able to retire the work it reports on (the same rule
    ``jobs.touch_drain_heartbeat`` follows). A lost checkpoint costs a
    re-extraction; a raised one would mark good content 'failed'.
    """
    if not state.get("checkpoint"):
        return
    state["windows_done"].append(int(job["window"]))
    try:
        ingest_queue.save_window_checkpoint(
            state["ids"][0],
            fp=state["fp"],
            done=state["windows_done"],
            saved=state["saved"],
            rejected=state["rejected"],
        )
    except Exception:  # noqa: BLE001 — never let bookkeeping fail the group
        _COHORT_STATS["cohort_checkpoint_failures"] = (
            _COHORT_STATS.get("cohort_checkpoint_failures", 0) + 1
        )
        logger.warning("window checkpoint for group %s failed", state["ids"], exc_info=True)


def _drain_job(
    job: dict[str, Any], *, pre_extracted: dict | None, planned: EnsemblePlan | None
) -> tuple[int, int]:
    """Extract+ingest ONE WINDOW of a coalesced group. Owns the queue self-heal
    taxonomy.

    Returns ``(rows_drained, items_saved)``. The group's ROWS are counted (and
    flipped) exactly once, when its LAST window lands: a group is still
    all-or-nothing, it just takes one extraction call per context-sized window to
    get there (``prep._assemble_jobs``). Failure semantics stay GROUP-wide — one
    window's failure defers/fails every row of the group and skips its remaining
    windows, so the next run re-extracts the whole group rather than stitching a
    session together out of two different nights (ingest is idempotent,
    Invariant 3, so the re-extraction cannot double-write).

    AMENDED 2026-08-10 (owner decision), the last sentence above: the next run no
    longer re-extracts the WHOLE group, only the windows that have not landed yet.
    The all-or-nothing property that sentence exists to protect is unchanged — the
    ROW FLIPS are still group-atomic, a session still never half-lands in the
    queue's status accounting, and a group is still resolved as a unit. What
    gained a memory is the EXTRACTION CALLS: a window whose extract_and_ingest
    returned has its items durably stored, so re-running it is pure waste, and
    :func:`_record_window` writes down which windows those were. The motivation
    was measurable — three giant sessions burned ~8 GPU-hours a night re-mining
    already-stored windows (they deduped to nothing on content_hash) before
    hitting the same failing window and deferring the whole group again: 7.9 h of
    drain, 0 rows drained, 0 items saved. See :mod:`kbcore.queue_checkpoint`.
    """
    state = job["group"]
    ids = state["ids"]
    if state["done"]:
        # A sibling window already resolved this group (failed or deferred).
        # Extracting the rest would spend the 14B on rows nobody will flip.
        return 0, 0
    # Closed by DEFAULT and re-opened only by a successful non-final window, so
    # every failure path below — including one nobody anticipated — resolves the
    # whole group and skips its siblings, rather than each window discovering the
    # same outage on the GPU.
    state["done"] = True
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
        state["saved"] += saved
        state["rejected"] += rejected
        state["left"] -= 1
        if state["left"] > 0:
            state["done"] = False
            _record_window(state, job)  # resume memory: this window is banked
            jobs.touch_drain_heartbeat(ids)  # the rows won't flip for a while yet
            return 0, saved  # more windows to come — the rows stay 'pending'
        # All-or-nothing: a group extracted once must never leave rows
        # pending for a second, partial extraction of the same session.
        ingest_queue.mark_group_extracted(ids, state["saved"], state["rejected"])
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

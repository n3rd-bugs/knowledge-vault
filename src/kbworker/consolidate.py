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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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


def _drain_ingest_queue() -> tuple[int, int]:
    """Step 0: extract queued remote submissions — the ONLY place the 14B runs on
    remote content (Invariant 8). Per-row isolation: one bad row logs + continues.

    Returns ``(rows_drained, items_saved)``.
    """
    drained = 0
    saved_total = 0
    # COALESCED BY SESSION. The network path windows RAW JSONL at 900 KB while
    # the local path assembles to text first, so the same session arrives as ~42x
    # more rows over MCP/REST (measured: 114 MB of JSONL is 2.7 MB of
    # conversation — 350 rows vs 9 for a 300 MB session). Extracting each raw
    # window separately spent the whole 200-row nightly budget on one session and
    # fed the 14B mostly JSON scaffolding. Concatenating a session's rows and
    # assembling ONCE restores the local path's behaviour.
    groups = ingest_queue.pending_groups(limit=QUEUE_BATCH)

    # COHORT PREFETCH. With one model slot on the card, every switch is a full
    # load, so extracting model-major across a cohort turns ~3 extractor loads
    # per row into ~3 per cohort. Only the extraction phase is batched; gating,
    # clustering, judging and ingest stay strictly per job, so per-job failure
    # isolation and the review/dedupe semantics are untouched.
    #
    # A prefetch failure is NEVER fatal: pre stays None and every job falls back
    # to extracting for itself, which is exactly today's behaviour. Cohorting is
    # an optimisation, and an optimisation that can break the drain is not one.
    cohort = max(1, int(settings.kb_drain_cohort))
    pre_by_uri: dict[str, dict] = {}
    if cohort > 1 and ensemble.mode() == "live" and groups:
        for start in range(0, len(groups), cohort):
            batch = groups[start : start + cohort]
            texts, keys = [], []
            for g in batch:
                r0 = g["rows"][0]
                texts.append(
                    sessions.assemble_text("".join(r["content"] for r in g["rows"]), r0["kind"])
                )
                keys.append(g["uri"])
            try:
                for key, pre in zip(keys, ensemble.prefetch_extractions(texts), strict=True):
                    pre_by_uri[key] = pre
                logger.info("cohort prefetch: %d job(s), extractors loaded once each", len(batch))
            except Exception:  # noqa: BLE001 — never let an optimisation fail the drain
                logger.warning("cohort prefetch failed; falling back per job", exc_info=True)

    for group in groups:
        rows = group["rows"]
        row = rows[0]
        ids = [r["id"] for r in rows]
        try:
            # Row order is the hook's byte order (it submits sequential offsets),
            # so plain concatenation reconstitutes the delta before assembly.
            text = sessions.assemble_text("".join(r["content"] for r in rows), row["kind"])
            meta = row["meta"] or {}
            uri = meta.get("source_uri") or f"queue:{row['kind']}:{row['id']}"
            saved, rejected = sessions.extract_and_ingest(
                text,
                row["project"],
                uri,
                pre_extracted=pre_by_uri.get(group["uri"]),
                # FIX 6 (audit 2026-07-26): a queued session/memory window anchors
                # no FILE — repo/commit ride the item meta as provenance instead of
                # a phantom {"path": "."} code_ref.
                code_ref=None,
                branch=meta.get("branch"),
                extra_detail={
                    "ingest_queue_id": row["id"],
                    "source": row["kind"],
                    **({"coalesced_rows": len(ids)} if len(ids) > 1 else {}),
                },
                extra_meta=sessions._session_provenance(meta.get("repo"), meta.get("commit")),
            )
            # All-or-nothing: a group extracted once must never leave rows
            # pending for a second, partial extraction of the same session.
            ingest_queue.mark_group_extracted(ids, saved, rejected)
            drained += len(ids)
            saved_total += saved
        except (ExtractorUnavailable, EmbedderUnavailable) as e:
            # CONNECTIVITY (Ollama down/restarting/model-not-loaded during the
            # nightly window): do NOT mark_failed — leave the row 'pending' with
            # attempts UNCHANGED so an outage never burns a row's retry budget; the
            # next run retries once the model is back. mark_deferred bumps a bounded
            # deferral counter and, if a "transient" failure never heals (a
            # payload-deterministic 5xx / mid-response crash), escalates the row to
            # 'failed' so it can't re-drain every night forever (review finding).
            # Every row in the group, not just the first: deferring one and
            # leaving the rest pending would split the session and extract it in
            # pieces on the next run — the exact thing coalescing prevents.
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
                logger.warning(
                    "%d row(s) deferred (extractor/embedder unavailable): %s", len(ids), e
                )
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
    """Run one step, isolating failure: log and leave the count at 0."""
    try:
        counts[key] = fn()
    except Exception:  # noqa: BLE001 — one step must not abort the pass
        logger.exception("consolidation step %r failed", key)


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

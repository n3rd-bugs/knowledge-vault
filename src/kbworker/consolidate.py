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

from kbcore import consolidation, ingest_queue, llm
from kbcore import reembed as reembed_mod
from kbcore.config import settings
from kbcore.db import advisory_lock
from kbcore.ingest import ingest
from kbworker import sessions

logger = logging.getLogger("kbworker.consolidate")

ADVISORY_KEY = 42
CONTRA_LOW, CONTRA_HIGH = 0.80, 0.95
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
    for row in ingest_queue.pending(limit=QUEUE_BATCH):
        try:
            text = sessions.assemble_text(row["content"], row["kind"])
            meta = row["meta"] or {}
            code_ref = sessions._code_ref(row["project"], meta.get("repo"), meta.get("commit"))
            uri = meta.get("source_uri") or f"queue:{row['kind']}:{row['id']}"
            saved, rejected = sessions.extract_and_ingest(
                text,
                row["project"],
                uri,
                code_ref=code_ref,
                branch=meta.get("branch"),
                extra_detail={"ingest_queue_id": row["id"], "source": row["kind"]},
            )
            ingest_queue.mark_extracted(row["id"], saved, rejected)
            drained += 1
            saved_total += saved
        except Exception as e:  # noqa: BLE001 — one bad submission must not abort the drain
            logger.exception("draining ingest_queue row %s failed", row["id"])
            ingest_queue.mark_failed(row["id"], f"{type(e).__name__}: {e}")
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
            "duplicates_marked": 0,
            "summaries_written": 0,
            "contradictions_queued": 0,
            "archived": 0,
            "reembedded": 0,
        }

        # Step 0: extract remote submissions BEFORE dedupe so the new items take
        # part in this run's dedupe/summary/contradiction passes.
        def _drain() -> int:
            drained, saved = _drain_ingest_queue()
            counts["queued_items_saved"] = saved
            return drained

        _run_step(counts, "queued_drained", _drain)

        # active_projects AFTER the drain so freshly-ingested submissions count.
        projects = consolidation.active_projects()
        _run_step(counts, "duplicates_marked", lambda: sum(_dedupe(p) for p in projects))
        _run_step(counts, "summaries_written", lambda: _summaries(since))
        _run_step(
            counts, "contradictions_queued", lambda: sum(_contradictions(p) for p in projects)
        )
        _run_step(
            counts,
            "archived",
            lambda: consolidation.archive_stale(
                datetime.now(UTC) - timedelta(days=settings.kb_archive_after_days)
            ),
        )
        _run_step(counts, "reembedded", reembed_mod.reembed)

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

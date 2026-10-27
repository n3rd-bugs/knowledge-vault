"""kbcore.consolidation — service functions for the nightly refinement pass (§7.3).

All SQL for dedupe / summary / staleness / contradiction lives here; the worker
(kbworker.consolidate) only orchestrates. Every write honors the invariants:
raw item bodies are never edited (Invariant 1), automation only flips ``status``
and never deletes rows (Invariant 5), and lead-chunk cosine is computed in SQL
with pgvector (``<=>`` is cosine distance, so similarity = ``1 - distance``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg

from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.errors import NotFound
from kbcore.ingest import ingest_in
from kbcore.review import queue_review_in
from kbcore.supersede import supersede


def last_consolidation_at() -> datetime | None:
    """created_at of the most recent kb-system CONSOLIDATION report — the "since
    last run" mark for the nightly summarize step.

    Requires ``meta->>'consolidation' = 'true'``: deep-search (and any manual
    ``kb_save``) also writes ``type='report'`` items WITHOUT that marker, and
    matching on type alone let such a report poison ``since`` so the next
    nightly missed items saved between the real consolidation and the foreign
    report (KB item 942 §2). Returns None when no consolidation report exists
    (genuine first run — the summarize step then floors ``since`` to the epoch).
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT max(created_at) FROM items "
            "WHERE project = 'kb-system' AND type = 'report' "
            "  AND meta->>'consolidation' = 'true'"
        ).fetchone()
    return row[0] if row and row[0] else None


EVAL_BASELINE_LOOKBACK = 30  # reports scanned back for the last completed eval


def previous_eval_metrics() -> dict[str, float] | None:
    """Eval hit@5/MRR from the newest kb-system report that actually COMPLETED an
    eval (``meta.eval_status == 'ok'``), scanning back a bounded window.

    The search-quality sentinel compares each run against this. Reading only the
    single newest report was fragile (review finding): one errored/skipped/disabled
    night — or ANY interleaved ``type='report'`` item (a deep-search save, a manual
    kb_save) — carries no ok metrics and used to wipe the baseline, blinding the
    sentinel to a regression that merged that same day. We now walk back over
    non-ok/foreign reports (up to :data:`EVAL_BASELINE_LOOKBACK`) to the last real
    baseline. Returns None only when NO recent report carries a completed eval
    (genuine first run), so the first successful eval just establishes a baseline.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT meta FROM items WHERE project = 'kb-system' AND type = 'report' "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (EVAL_BASELINE_LOOKBACK,),
        ).fetchall()
    for (meta,) in rows:
        if not isinstance(meta, dict) or meta.get("eval_status") != "ok":
            continue
        try:
            return {"hit_at_5": float(meta["eval_hit_at_5"]), "mrr": float(meta["eval_mrr"])}
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _lead_pairs(
    project: str,
    low: float,
    high: float,
    *,
    skip_judged: bool = False,
    limit: int | None = None,
) -> list[tuple[int, int, float]]:
    """Active-item pairs whose lead-chunk cosine is in ``[low, high)`` (pass ``high``
    >= 1.0 for no upper bound). Only current-model embeddings compared (DATA-3).

    Consolidation OUTPUT (``summary``/``report``) is excluded on both sides — it
    is not source knowledge to be re-consolidated. Otherwise nightly reports
    (near-identical boilerplate, ~0.98 cosine on real embeddings) would dedup
    each other and collapse the audit trail; this matches the summary/staleness
    steps, which already exclude those types (review finding).

    ``skip_judged`` drops pairs already recorded in ``contradiction_judgements``
    AT THE CURRENT EMBED MODEL (migration 017) — the contradiction step is one
    LLM call per pair and was re-paying for the same negatives every night. Only
    that step passes it: dedupe is pure SQL and costs nothing to redo.
    ``limit`` bounds the returned pairs; the ``sim DESC`` order means a capped
    run spends its budget on the closest (most likely) pairs first.
    """
    sql = [
        """
        SELECT ia.id, ib.id, 1 - (ca.embedding <=> cb.embedding) AS sim
        FROM items ia
        JOIN chunks ca ON ca.item_id = ia.id AND ca.ord = 0
        JOIN items ib ON ib.project = ia.project AND ib.id > ia.id AND ib.status = 'active'
        JOIN chunks cb ON cb.item_id = ib.id AND cb.ord = 0
        WHERE ia.project = %(project)s AND ia.status = 'active'
          AND ia.type NOT IN ('summary', 'report')
          AND ib.type NOT IN ('summary', 'report')
          AND ca.embedding IS NOT NULL AND cb.embedding IS NOT NULL
          AND ca.embed_model = %(model)s AND cb.embed_model = %(model)s
          AND (1 - (ca.embedding <=> cb.embedding)) >= %(low)s
          AND (1 - (ca.embedding <=> cb.embedding)) < %(high)s
        """
    ]
    params: dict[str, Any] = {
        "project": project,
        "model": settings.kb_embed_model,
        "low": low,
        "high": high,
    }
    if skip_judged:
        # ib.id > ia.id above, so the stored pair is always (smaller, larger) —
        # record_pair_judgement normalizes to the same order. Matching on
        # embed_model is what makes a re-embed re-open every pair: judgements
        # written under the previous model simply stop matching (migration 017).
        sql.append(
            """
          AND NOT EXISTS (
              SELECT 1 FROM contradiction_judgements cj
              WHERE cj.a_id = ia.id AND cj.b_id = ib.id AND cj.embed_model = %(model)s
          )
            """
        )
    sql.append("ORDER BY sim DESC, ia.id, ib.id")
    if limit is not None:
        sql.append("LIMIT %(limit)s")
        params["limit"] = max(0, int(limit))
    with connect() as conn:
        rows = conn.execute("\n".join(sql), params).fetchall()
    return [(a, b, float(s)) for a, b, s in rows]


def duplicate_pairs(project: str, threshold: float) -> list[tuple[int, int, float]]:
    """Active-item pairs with lead-chunk cosine >= ``threshold`` (dedupe candidates)."""
    return _lead_pairs(project, threshold, 2.0)  # 2.0 ⇒ no upper bound (cosine ≤ 1)


def contradiction_pairs(
    project: str, low: float = 0.80, high: float = 0.95, limit: int | None = None
) -> list[tuple[int, int, float]]:
    """Active-item pairs with lead-chunk cosine in ``[low, high)`` (near-but-not-dup),
    MINUS every pair already judged at the current embed model (migration 017).

    ``limit`` is the caller's remaining per-night judging budget.
    """
    return _lead_pairs(project, low, high, skip_judged=True, limit=limit)


def record_pair_judgement(a_id: int, b_id: int, verdict: bool) -> bool:
    """Remember that this pair was judged at the CURRENT embed model. INSERT-only.

    Written the moment the judge gives a True/False verdict (None is never memorized,
    KB 55589), so a cut step loses at most the pair in flight instead of the whole
    night's judging. ``ON CONFLICT DO NOTHING`` keeps this purely additive
    (Invariant 5): a verdict is never updated or deleted, and the memory is
    invalidated only by re-embedding under a new model, which changes the key.
    Returns True when a new row landed.
    """
    lo, hi = (a_id, b_id) if a_id <= b_id else (b_id, a_id)
    with transaction() as conn:
        row = conn.execute(
            """
            INSERT INTO contradiction_judgements (a_id, b_id, embed_model, verdict)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (a_id, b_id, embed_model) DO NOTHING
            RETURNING a_id
            """,
            (lo, hi, settings.kb_embed_model, verdict),
        ).fetchone()
    return row is not None


def active_projects() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT project FROM items WHERE status = 'active' ORDER BY project"
        ).fetchall()
    return [r[0] for r in rows]


def item_ranks(item_ids: list[int]) -> dict[int, tuple[int, datetime, int]]:
    """Canonical-selection key per item: (0 if verified else 1, created_at, id).

    Oldest verified wins; else oldest overall — smallest key is canonical.
    """
    if not item_ids:
        return {}
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT i.id, COALESCE(s.trust, 'verified'), i.created_at
            FROM items i LEFT JOIN sources s ON s.id = i.source_id
            WHERE i.id = ANY(%s)
            """,
            (item_ids,),
        ).fetchall()
    return {r[0]: (0 if r[1] == "verified" else 1, r[2], r[0]) for r in rows}


def tags_needing_summary(since: datetime | None, min_new: int = 3) -> list[tuple[str, str, int]]:
    """(project, tag, new_count) for tags with >= ``min_new`` active items NEWER
    than the tag's own newest summary — need derived from DATA, not a run mark.

    REVIEW #6 (the lossy-deferral bug): the old predicate counted items created
    after ``since`` (the previous consolidation report), but ``_write_report``
    runs unconditionally, so a stood-down summaries step still advanced the mark
    and its unreached tags were silently dropped. The per-tag high-water is now
    the newest ``type='summary'`` item for that (project, topic) — ANY status; a
    superseded summary still proves the pass ran — which no unrelated report can
    advance. A tag with no summary yet counts every active item (the old epoch
    floor, per tag); summary/report items stay excluded from the counted side so
    a summary never feeds its own topic. H8a (review #8): the counted side applies the
    SAME :data:`SUMMARY_EXCLUDING_META` exclusion as :func:`active_items_with_tag`, so an
    all-flagged tag (the ~113 'deferred' residue) is never selected — todo == writable work.
    """
    del since  # review #6: the run mark is no longer the source of truth
    with connect() as conn:
        rows = conn.execute(
            """
            WITH hw AS (
                SELECT project, meta->>'topic' AS tag, max(created_at) AS mark
                  FROM items
                 WHERE type = 'summary' AND meta->>'topic' IS NOT NULL
                 GROUP BY project, meta->>'topic'
            )
            SELECT t.project, t.tag, count(*) AS new_count
              FROM (
                  SELECT i.project, unnest(i.tags) AS tag, i.created_at
                    FROM items i
                   WHERE i.status = 'active' AND i.type NOT IN ('summary', 'report')
                     AND NOT jsonb_exists_any(COALESCE(i.meta, '{}'::jsonb), %(excl)s)
              ) t
              LEFT JOIN hw ON hw.project = t.project AND hw.tag = t.tag
             WHERE hw.mark IS NULL OR t.created_at > hw.mark
             GROUP BY t.project, t.tag
            HAVING count(*) >= %(min)s
             ORDER BY t.project, t.tag
            """,
            {"min": min_new, "excl": list(SUMMARY_EXCLUDING_META)},
        ).fetchall()
    return [(p, tag, int(n)) for p, tag, n in rows]


# Trust ranking for the summary provenance stamp: the WEAKEST source governs.
_TRUST_RANK = {"verified": 2, "unverified": 1}
# meta keys that disqualify an item from FEEDING a summary (audit 2026-07-26).
SUMMARY_EXCLUDING_META = ("unresolved_refs", "aspirational")


def active_items_with_tag(project: str, tag: str) -> list[dict[str, Any]]:
    """Source items (id/title/body/trust) feeding a topic summary — never summaries.

    ``status = 'active'`` already keeps quarantined ('pending') and rejected
    ('archived') extractions out (verified 2026-07-26 — unchanged).

    FIX 5 (audit 2026-07-26) adds a CONTENT exclusion: an item that the pipeline
    itself flagged is not fit to be restated as a topic conclusion. Summaries are
    read as authoritative — one audited summary even ADDED a fabrication on top of
    unverified leaves — so an item carrying ``meta.unresolved_refs`` (invented
    identifiers, kbcore.ref_check) or an aspirational marker never feeds one. The
    item stays fully active and searchable; only its promotion into a synthesized
    conclusion is blocked. ``jsonb_exists`` is used instead of the ``?`` operator
    so the key name rides as a normal parameter (Invariant 2: parameterized SQL).
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT i.id, i.title, i.body, COALESCE(s.trust, 'verified')
            FROM items i LEFT JOIN sources s ON s.id = i.source_id
            WHERE i.project = %s AND i.status = 'active'
              AND i.type NOT IN ('summary', 'report') AND %s = ANY(i.tags)
              AND NOT jsonb_exists_any(COALESCE(i.meta, '{}'::jsonb), %s)
            ORDER BY i.id
            """,
            (project, tag, list(SUMMARY_EXCLUDING_META)),
        ).fetchall()
    return [{"id": r[0], "title": r[1], "body": r[2], "trust": r[3]} for r in rows]


def _min_source_trust(conn: psycopg.Connection, item_ids: list[int]) -> str:
    """The WEAKEST trust across a summary's sources ('unverified' when unknown).

    A summary is only as good as its worst leaf, and the audit found summaries
    built from unverified extractions being read as authoritative. Stamping the
    minimum makes that visible at retrieval time instead of implicit.
    """
    if not item_ids:
        return "unverified"
    rows = conn.execute(
        """
        SELECT COALESCE(s.trust, 'verified') FROM items i
        LEFT JOIN sources s ON s.id = i.source_id
        WHERE i.id = ANY(%s)
        """,
        (item_ids,),
    ).fetchall()
    if not rows:
        return "unverified"
    return min((r[0] for r in rows), key=lambda t: _TRUST_RANK.get(t, 0))


def open_review_count() -> int:
    """Unresolved review-queue rows — a report metric."""
    with connect() as conn:
        row = conn.execute("SELECT count(*) FROM review_queue WHERE resolved = false").fetchone()
    return int(row[0])


def extraction_stats() -> tuple[int, int]:
    """(rejected_total, saved_total) across session extractions, for the rejection rate."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM((detail->>'rejected_count')::int), 0), count(*)
            FROM review_queue WHERE reason = 'unverified_extraction'
            """
        ).fetchone()
    return int(row[0]), int(row[1])


def item_pair_texts(a_id: int, b_id: int) -> tuple[tuple[str, str], tuple[str, str]]:
    """(title, body) for two items — fed to the contradiction judge."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title, body FROM items WHERE id = ANY(%s)", ([a_id, b_id],)
        ).fetchall()
    by_id = {r[0]: (r[1], r[2]) for r in rows}
    if a_id not in by_id or b_id not in by_id:
        raise NotFound(f"item pair {a_id}/{b_id} not found")
    return by_id[a_id], by_id[b_id]


# Below: write service functions (each in its own transaction).
def mark_duplicate(dup_id: int, canonical_id: int) -> bool:
    """Flag ``dup_id`` as a duplicate of ``canonical_id`` (Invariant 5: no delete).

    Adds ``relations(dup -derived_from-> canonical)``. Skips (returns False) if
    the item is already non-active — re-runs must not churn.
    """
    with transaction() as conn:
        row = conn.execute("SELECT status, project FROM items WHERE id = %s", (dup_id,)).fetchone()
        if row is None:
            raise NotFound(f"item {dup_id} does not exist")
        status, project = row
        if status != "active":
            return False
        conn.execute(
            "UPDATE items SET status = 'duplicate', updated_at = now() "
            "WHERE id = %s AND status = 'active'",
            (dup_id,),
        )
        conn.execute(
            """
            INSERT INTO relations (project, src_kind, src_id, rel, dst_kind, dst_id)
            VALUES (%s, 'item', %s, 'derived_from', 'item', %s)
            ON CONFLICT DO NOTHING
            """,
            (project, dup_id, canonical_id),
        )
    return True


def _prior_summaries(project: str, topic: str, exclude: int) -> list[int]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id FROM items
            WHERE project = %s AND type = 'summary' AND status = 'active'
              AND meta->>'topic' = %s AND id <> %s
            ORDER BY id
            """,
            (project, topic, exclude),
        ).fetchall()
    return [r[0] for r in rows]


def create_summary(project: str, topic: str, body: str, source_item_ids: list[int]) -> int:
    """Create a NEW type='summary' item and supersede any prior one for the topic.

    Fresh chunked+embedded item (trust='unverified' — auto-generated), linked
    ``derived_from`` to every source id; a prior active summary for the same
    (project, topic) is superseded, never edited (Invariant 1). The timestamped
    source uri makes the refresh a new item even if the body is byte-identical.

    FIX 5 (audit 2026-07-26): the summary carries its PROVENANCE — ``source_ids``
    and ``source_trust``, the MINIMUM trust across those sources. Summaries built
    from unverified leaves were being read as authoritative (one restated a
    fabricated response contract as "Testing confirmed"), and the summary's own
    ``trust='unverified'`` did not distinguish "auto-generated from verified
    knowledge" from "auto-generated from unreviewed extractions". Now it does.
    """
    ts = datetime.now(UTC)
    # A namespace-only topic ('lang:python', 'tool:docker') titles the summary
    # 'Summary — lang:python', which reads as noise and leaked 213 such items into
    # the corpus (cleaned 2026-08-04). Strip the namespace prefix for the TITLE;
    # the topic itself is unchanged everywhere else it is used.
    _ns, _sep, _rest = topic.partition(":")
    title_topic = _rest if _sep and " " not in _ns else topic
    ids = list(dict.fromkeys(source_item_ids))
    with transaction() as conn:
        new_id = ingest_in(
            conn,
            project,
            "summary",
            f"Summary — {title_topic}",
            body,
            kind="chat",
            uri=f"summary:{project}:{topic}:{ts.isoformat()}",
            trust="unverified",
            tags=[topic],
            meta={
                "topic": topic,
                "consolidation": True,
                "extractor": settings.kb_extract_model,
                "source_ids": ids,
                "source_trust": _min_source_trust(conn, ids),
            },
        )
        if new_id is None:  # unreachable with the timestamped uri, but be explicit
            raise NotFound("summary source collided unexpectedly")
        for sid in ids:
            conn.execute(
                """
                INSERT INTO relations (project, src_kind, src_id, rel, dst_kind, dst_id)
                VALUES (%s, 'item', %s, 'derived_from', 'item', %s)
                ON CONFLICT DO NOTHING
                """,
                (project, new_id, sid),
            )
    # Supersede prior summaries in a follow-up txn (self-healing if interrupted:
    # the next run supersedes any stragglers down to the newest).
    for old_id in _prior_summaries(project, topic, exclude=new_id):
        supersede(old_id, new_item_id=new_id, reason="consolidation refresh")
    return new_id


def archive_stale(before: datetime) -> int:
    """Archive active knowledge items untouched since ``before`` (Invariant 5).

    Stale = created_at AND updated_at predate ``before`` with no access_log entry
    at/after ``before``. Summary/report items are exempt (consolidation output,
    not aging source knowledge). Only ``status`` changes (Invariant 1).
    """
    with transaction() as conn:
        rows = conn.execute(
            """
            UPDATE items SET status = 'archived', updated_at = now()
            WHERE status = 'active'
              AND type NOT IN ('summary', 'report')
              AND created_at < %(before)s AND updated_at < %(before)s
              AND NOT EXISTS (
                  SELECT 1 FROM access_log al
                  WHERE al.item_id = items.id AND al.ts >= %(before)s
              )
            RETURNING id
            """,
            {"before": before},
        ).fetchall()
    return len(rows)


def record_contradiction(a_id: int, b_id: int, evidence: int | None = None) -> bool:
    """Record ``a -contradicts-> b`` + queue it for review. Idempotent (no re-queue).

    Never auto-supersedes (§7.3 step 3). Returns True only when this is a newly
    recorded contradiction; a repeat pair is a no-op so re-runs don't pile up
    review-queue rows.
    """
    with transaction() as conn:
        proj = conn.execute("SELECT project FROM items WHERE id = %s", (a_id,)).fetchone()
        if proj is None:
            raise NotFound(f"item {a_id} does not exist")
        row = conn.execute(
            """
            INSERT INTO relations (project, src_kind, src_id, rel, dst_kind, dst_id, evidence_item)
            VALUES (%s, 'item', %s, 'contradicts', 'item', %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (proj[0], a_id, b_id, evidence),
        ).fetchone()
        if row is None:
            return False  # already recorded — idempotent
        queue_review_in(conn, a_id, "contradiction", {"a_id": a_id, "b_id": b_id})
    return True

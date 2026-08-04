"""kbcore.review_triage — LLM triage of the unverified-extraction backlog (WP-2).

Owner decision 2026-07-18: manual review never drained the queue (~300 open
``unverified_extraction`` rows), so the nightly consolidation's 14B TRIAGES
each pending row — writing a keep / reject_noise / reject_duplicate SUGGESTION
into the row's ``detail`` JSONB (:func:`run_triage` → :func:`store_triage`) —
and the owner applies suggestions in bulk (:func:`apply_triage`). The LLM never
rejects or archives anything by itself: storing a suggestion touches only
``detail``; applying is always an explicit human action, and every apply is a
status flip (Invariant 5 — rows resolve, items flip status, nothing is deleted,
trust upgrades stay human-only via review.promote). The 14B runs only from
kbworker.consolidate or the user-invoked ``kb review triage`` (Invariant 8) —
both call :func:`run_triage`, the single shared loop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from kbcore import cluster_util, consolidation, graph, llm, review
from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.errors import (
    EmbedderUnavailable,
    ExtractionFailed,
    ExtractorUnavailable,
    InvalidOperation,
)

logger = logging.getLogger("kbcore.review_triage")

TRIAGE_SIMILAR_K = 5  # neighbor candidates offered to the judge as dup targets
# Rows between stand-down checks — the bound is the OVERSHOOT (at most this many
# more 14B calls after the deadline passes or the grid drops), not the cost: the
# guard memoizes its answer for a few seconds.
STOP_CHECK_EVERY = 5


def pending_triage(limit: int) -> list[dict[str, Any]]:
    """Open ``unverified_extraction`` rows with NO stored triage — priority-stamped
    rows first (``yield_hygiene.prioritize_judge_failed``; existence check, the stamp
    is an object), then oldest first.

    Each row joins its item (id/project/type/title/body/tags) plus the item's
    access_log count — the judge weighs whether the knowledge was ever recalled.
    One parameterized query; ``limit`` bounds the nightly GPU spend (``<= 0``
    returns nothing — the kill switch rides the batch knob).

    ``unresolved_refs`` (audit 2026-07-26 FIX 2) rides along so the judge sees the
    identifiers the item cites that resolve to NOTHING — the single strongest
    machine-checkable signal that an otherwise-plausible extraction is confabulated.
    """
    if limit <= 0:
        return []
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT rq.id, i.id, i.project, i.type, i.title, i.body, i.tags,
                   (SELECT count(*) FROM access_log al WHERE al.item_id = i.id),
                   COALESCE(i.meta, '{}'::jsonb)
            FROM review_queue rq
            JOIN items i ON i.id = rq.item_id
            WHERE rq.resolved = false
              AND rq.reason = 'unverified_extraction'
              AND NOT (COALESCE(rq.detail, '{}'::jsonb) ? 'triage')
            ORDER BY (COALESCE(rq.detail, '{}'::jsonb) ? 'priority') DESC, rq.id
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "queue_id": r[0],
            "item": {
                "id": r[1],
                "project": r[2],
                "type": r[3],
                "title": r[4],
                "body": r[5],
                "tags": list(r[6] or []),
                "unresolved_refs": list(
                    (r[8] or {}).get("unresolved_refs") or [] if isinstance(r[8], dict) else []
                ),
            },
            "usage_count": int(r[7]),
        }
        for r in rows
    ]


def store_triage(queue_id: int, triage: dict[str, Any]) -> bool:
    """Merge a triage suggestion into an OPEN row's ``detail`` (jsonb ``||``).

    Stamps the extractor model + UTC time for audit. Only ``detail`` changes —
    the row stays open and the item is untouched (the LLM suggests, a human
    applies). A row resolved since the batch was selected is left alone
    (returns False); sibling detail keys are never clobbered (jsonb merge).
    """
    stamp = {
        "triage": {
            **triage,
            "model": settings.kb_extract_model,
            "at": datetime.now(UTC).isoformat(),
        }
    }
    with transaction() as conn:
        row = conn.execute(
            "UPDATE review_queue "
            "SET detail = COALESCE(detail, '{}'::jsonb) || %s "
            "WHERE id = %s AND resolved = false "
            "RETURNING id",
            (Jsonb(stamp), queue_id),
        ).fetchone()
    return row is not None


def run_triage(batch: int, should_stop: Callable[[], str | None] | None = None) -> int:
    """Drain up to ``batch`` pending rows through the 14B judge; store suggestions.

    The SHARED loop behind the nightly ``review_triaged`` step and the explicit
    user-invoked ``kb review triage`` — the two Invariant-8-sanctioned GPU call
    sites. Dup candidates come from the existing :func:`kbcore.graph.similar`
    helper (the ``kb_similar`` primitive; active neighbors only, which is what a
    dup target must be). Per-row taxonomy mirrors the ingest-queue drain:
    ExtractorUnavailable / EmbedderUnavailable stop the batch early (rows stay
    pending for the next night, no penalty); ExtractionFailed skips just that
    row (retried next night — acceptable, no penalty mechanism by design); a
    validation-rejected verdict stores nothing. Returns suggestions stored.

    ``should_stop`` is the caller's stand-down guard (the nightly passes
    :func:`kbworker.consolidate.should_stop`, which answers 'deadline' /
    'battery' / None). It is asked every :data:`STOP_CHECK_EVERY` rows — this is
    a 14B call per row and it used to run unbounded past the drain's deadline —
    and a stop leaves the remaining rows exactly as an outage does: open,
    untriaged, no penalty, re-selected by :func:`pending_triage` next run. The
    default (None) never stops, so an explicitly requested ``kb review triage``
    is not wall-clock-bounded.
    """
    if batch <= 0:
        return 0
    stored = 0
    rows = pending_triage(batch)
    for n, row in enumerate(rows):
        if should_stop is not None and n % STOP_CHECK_EVERY == 0 and (reason := should_stop()):
            logger.warning(
                "triage standing down (%s) after %d row(s): %d left open for the next run",
                reason,
                n,
                len(rows) - n,
            )
            break
        item = row["item"]
        try:
            similar = [
                {"id": h.item_id, "title": h.title, "similarity": h.score, "status": "active"}
                for h in graph.similar(item["id"], k=TRIAGE_SIMILAR_K)
            ]
            verdict = llm.triage_extraction(item, similar, row["usage_count"])
        except (ExtractorUnavailable, EmbedderUnavailable) as e:
            logger.warning(
                "triage stopped early at row %s (model unavailable): %s", row["queue_id"], e
            )
            break
        except ExtractionFailed as e:
            logger.warning("triage of row %s failed (content): %s", row["queue_id"], e)
            continue
        except Exception:  # noqa: BLE001 — one bad row must not abort the batch
            logger.exception("triage of row %s failed", row["queue_id"])
            continue
        if verdict is not None and store_triage(row["queue_id"], verdict):
            stored += 1
    return stored


def _close_row(queue_id: int, kind: str, **extra: Any) -> None:
    """Resolve one open row with an ``auto_resolution`` audit stamp (status flip)."""
    stamp = {"auto_resolution": {"kind": kind, **extra, "at": datetime.now(UTC).isoformat()}}
    with transaction() as conn:
        conn.execute(
            "UPDATE review_queue SET resolved = true, "
            "detail = COALESCE(detail, '{}'::jsonb) || %s "
            "WHERE id = %s AND resolved = false",
            (Jsonb(stamp), queue_id),
        )


def _apply_one(qid: int, item_id: int, action: str) -> str:
    """Apply one keep/reject_noise suggestion; raises to leave the row OPEN."""
    if action == "keep":
        # Item untouched: stays active + unverified (a triage 'keep' is NOT a
        # promotion — trust upgrades remain human-only via review.promote).
        _close_row(qid, "triage_keep")
        return "kept (row closed; item stays active + unverified)"
    review.reject(qid)  # archives the item + resolves the row (status flips)
    return f"rejected item {item_id} (archived)"


def _apply_duplicate_clusters(dup_rows: list[tuple[int, int, dict[str, Any]]]) -> dict[int, str]:
    """Cluster-canonicalize the batch's reject_duplicate rows (KB gotcha 958).

    Pairwise LLM edges are not a consistent DAG (reciprocal pairs / chains), and
    blind per-edge apply retired BOTH ends of a reciprocal pair. So: union-find
    the valid {item, dup_of} edges; canonical per cluster = best ACTIVE member
    by the nightly dedupe's rule (consolidation.item_ranks: oldest verified,
    else oldest — item_ranks alone doesn't fit since it ignores status, so only
    active members are ranked). Non-canonical members retire to the CLUSTER
    canonical even when it differs from a row's stored dup_of (stamped
    note='cluster_canonicalized'); a canonical's own row closes as the cluster
    winner; an all-retired cluster just closes its rows. Per-member isolation:
    a raise stamps that member's rows 'error: …' and leaves them open.
    """
    outcomes: dict[int, str] = {}
    if not dup_rows:
        return outcomes
    ids = {item_id for _, item_id, _ in dup_rows}
    for _, _, triage in dup_rows:
        d = triage.get("dup_of")
        if isinstance(d, int) and not isinstance(d, bool):
            ids.add(d)
    with connect() as conn:
        found = conn.execute(
            "SELECT id, type, status FROM items WHERE id = ANY(%s)", (list(ids),)
        ).fetchall()
    info = {r[0]: (r[1], r[2]) for r in found}
    edges = cluster_util.validate_dup_edges(dup_rows, info, outcomes)
    rows_by_item: dict[int, list[tuple[int, int]]] = {}
    for qid, item_id, dup_of in edges:
        rows_by_item.setdefault(item_id, []).append((qid, dup_of))

    for comp in cluster_util.clusters((i, d) for _, i, d in edges):
        actives = [i for i in comp if info.get(i, ("", ""))[1] == "active"]
        ranks = consolidation.item_ranks(actives)
        canonical = min(actives, key=lambda i: ranks[i]) if actives else None
        for member in sorted(comp):
            member_rows = rows_by_item.get(member, [])
            try:
                if member == canonical:
                    for qid, _ in member_rows:
                        _close_row(qid, "triage_keep", note="cluster_winner")
                        outcomes[qid] = f"item {member} kept as cluster canonical (row closed)"
                elif canonical is not None and consolidation.mark_duplicate(member, canonical):
                    for qid, stored in member_rows:
                        extra = {"note": "cluster_canonicalized"} if stored != canonical else {}
                        _close_row(qid, "triage_duplicate", dup_of=canonical, **extra)
                        outcomes[qid] = f"item {member} marked duplicate of {canonical}"
                else:  # member (or the whole cluster) already retired — the row is moot
                    for qid, _ in member_rows:
                        _close_row(qid, "item_retired")
                        outcomes[qid] = f"item {member} already retired; row closed"
            except Exception as e:  # noqa: BLE001 — per-member isolation; rows stay open
                for qid, _ in member_rows:
                    outcomes.setdefault(qid, f"error: {e}")
    return outcomes


def apply_triage(
    actions: set[str] | None = None,
    project: str | None = None,
    queue_ids: list[int] | None = None,
) -> list[tuple[int, str]]:
    """Apply stored triage suggestions in bulk — the explicit HUMAN step.

    Selects open ``unverified_extraction`` rows WITH a stored triage whose
    action is in ``actions`` (None = all of :data:`llm.TRIAGE_ACTIONS`),
    optionally narrowed by ``project`` and/or explicit ``queue_ids``. 'keep'
    closes the row (item untouched); 'reject_noise' archives the item; all
    'reject_duplicate' rows are canonicalized TOGETHER (never edge-by-edge) via
    :func:`_apply_duplicate_clusters`. An invalid ``dup_of`` — or any raise —
    becomes an ``'error: …'`` outcome and LEAVES THE ROW OPEN for a human
    (per-row isolation). Returns ``[(queue_id, outcome)]`` ordered by queue id.
    """
    if actions is not None:
        unknown = set(actions) - set(llm.TRIAGE_ACTIONS)
        if unknown:
            raise InvalidOperation(
                f"unknown triage action(s) {sorted(unknown)}; "
                f"one of {', '.join(llm.TRIAGE_ACTIONS)}"
            )
    wanted = set(actions) if actions else set(llm.TRIAGE_ACTIONS)
    sql = [
        """
        SELECT rq.id, rq.item_id, rq.detail
        FROM review_queue rq
        JOIN items i ON i.id = rq.item_id
        WHERE rq.resolved = false
          AND rq.reason = 'unverified_extraction'
          AND COALESCE(rq.detail, '{}'::jsonb) ? 'triage'
        """
    ]
    params: list[Any] = []
    if queue_ids:
        sql.append("AND rq.id = ANY(%s)")
        params.append(list(queue_ids))
    if project:
        sql.append("AND i.project = %s")
        params.append(project)
    # Priority-stamped rows (kbcore.yield_hygiene.prioritize_judge_failed marks
    # judge-flagged items 'secondary-judge-failed' that were ingested anyway) sort
    # FIRST so a bounded nightly batch examines the known-suspect ones before the
    # FIFO tail. EXISTENCE check, not a cast: yield_hygiene stamps an OBJECT
    # ({reason, source, at}), and `(detail->'priority')::int` raises "cannot cast
    # jsonb object to type integer" the moment one stamped row is in the batch —
    # which took the whole nightly triage down with it. Key present sorts first;
    # unstamped rows keep their rq.id order among themselves.
    sql.append("ORDER BY (COALESCE(rq.detail, '{}'::jsonb) ? 'priority') DESC, rq.id")
    with connect() as conn:
        rows = conn.execute("\n".join(sql), params).fetchall()

    selected: list[tuple[int, int, str, dict[str, Any]]] = []
    for qid, item_id, detail in rows:
        triage = (detail if isinstance(detail, dict) else {}).get("triage") or {}
        action = triage.get("action")
        if action in wanted:
            selected.append((qid, item_id, action, triage))

    outcomes: dict[int, str] = {}
    for qid, item_id, action, _triage in selected:
        if action == "reject_duplicate":
            continue  # canonicalized cluster-wide below, never edge-by-edge
        try:
            outcomes[qid] = _apply_one(qid, item_id, action)
        except Exception as e:  # noqa: BLE001 — per-row isolation; the row stays open
            outcomes[qid] = f"error: {e}"
    outcomes.update(
        _apply_duplicate_clusters([(q, i, t) for q, i, a, t in selected if a == "reject_duplicate"])
    )
    return [(qid, outcomes[qid]) for qid, _i, _a, _t in selected]  # rows were ORDER BY rq.id

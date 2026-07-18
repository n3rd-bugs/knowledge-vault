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
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from kbcore import consolidation, graph, llm, review
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


def pending_triage(limit: int) -> list[dict[str, Any]]:
    """Open ``unverified_extraction`` rows with NO stored triage, oldest first.

    Each row joins its item (id/project/type/title/body/tags) plus the item's
    access_log count — the judge weighs whether the knowledge was ever recalled.
    One parameterized query; ``limit`` bounds the nightly GPU spend (``<= 0``
    returns nothing — the kill switch rides the batch knob).
    """
    if limit <= 0:
        return []
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT rq.id, i.id, i.project, i.type, i.title, i.body, i.tags,
                   (SELECT count(*) FROM access_log al WHERE al.item_id = i.id)
            FROM review_queue rq
            JOIN items i ON i.id = rq.item_id
            WHERE rq.resolved = false
              AND rq.reason = 'unverified_extraction'
              AND NOT (COALESCE(rq.detail, '{}'::jsonb) ? 'triage')
            ORDER BY rq.id
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


def run_triage(batch: int) -> int:
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
    """
    if batch <= 0:
        return 0
    stored = 0
    for row in pending_triage(batch):
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


def _apply_one(qid: int, item_id: int, action: str, triage: dict[str, Any]) -> str:
    """Apply one stored suggestion; raises to leave the row OPEN for a human."""
    if action == "keep":
        # Item untouched: stays active + unverified (a triage 'keep' is NOT a
        # promotion — trust upgrades remain human-only via review.promote).
        _close_row(qid, "triage_keep")
        return "kept (row closed; item stays active + unverified)"
    if action == "reject_noise":
        review.reject(qid)  # archives the item + resolves the row (status flips)
        return f"rejected item {item_id} (archived)"
    # reject_duplicate: mark_duplicate flips status + records derived_from.
    dup_of = triage.get("dup_of")
    if isinstance(dup_of, bool) or not isinstance(dup_of, int):
        raise InvalidOperation("reject_duplicate suggestion carries no valid dup_of")
    with connect() as conn:
        canon = conn.execute("SELECT id FROM items WHERE id = %s", (dup_of,)).fetchone()
    if canon is None:
        raise InvalidOperation(f"dup_of item {dup_of} does not exist")
    if consolidation.mark_duplicate(item_id, dup_of):
        _close_row(qid, "triage_duplicate", dup_of=dup_of)
        return f"item {item_id} marked duplicate of {dup_of}"
    # Something else already retired the item — the review question is moot.
    _close_row(qid, "item_retired")
    return f"item {item_id} already retired; row closed"


def apply_triage(
    actions: set[str] | None = None,
    project: str | None = None,
    queue_ids: list[int] | None = None,
) -> list[tuple[int, str]]:
    """Apply stored triage suggestions in bulk — the explicit HUMAN step.

    Selects open ``unverified_extraction`` rows WITH a stored triage whose
    action is in ``actions`` (None = all of :data:`llm.TRIAGE_ACTIONS`),
    optionally narrowed by ``project`` and/or explicit ``queue_ids``. Per row:
    'keep' closes the row (item untouched); 'reject_noise' archives the item
    via review.reject; 'reject_duplicate' runs consolidation.mark_duplicate
    against the suggested canonical (already-retired items close the row as
    ``item_retired``). A missing/invalid ``dup_of`` — or any raise — becomes an
    ``'error: …'`` outcome and LEAVES THE ROW OPEN for a human (per-row
    isolation). Returns ``[(queue_id, outcome)]`` ordered by queue id.
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
    sql.append("ORDER BY rq.id")
    with connect() as conn:
        rows = conn.execute("\n".join(sql), params).fetchall()

    outcomes: list[tuple[int, str]] = []
    for qid, item_id, detail in rows:
        triage = (detail if isinstance(detail, dict) else {}).get("triage") or {}
        action = triage.get("action")
        if action not in wanted:
            continue
        try:
            outcomes.append((qid, _apply_one(qid, item_id, action, triage)))
        except Exception as e:  # noqa: BLE001 — per-row isolation; the row stays open
            outcomes.append((qid, f"error: {e}"))
    return outcomes

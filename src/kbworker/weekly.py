"""kbworker.weekly — the weekly trend report (review #4 strategy P1, KB 21332).

Week-scale movement — dial drift, demand-side gaps, promote uptake — was
invisible unless the owner dug through seven nightly report items by hand
(promote adoption sat at ZERO against 347 candidates with the data all there).
This rolls the week into ONE push. NO NEW TIMER: :func:`maybe_weekly` rides the
tail of every nightly digest (:func:`kbworker.digest.push_run_digest`) and
gates itself INSIDE the step — ``kb_weekly_report`` (kill switch) and
``kb_weekly_day`` (mon..sun; malformed fails toward OFF, logged), the topup's
day-logic pattern — and a run that already wrote a weekly item this week skips
(the Sunday boot-catch-up double-fire guard).

READS: the week's nightly consolidation reports (kb-system ``report`` items,
``meta.consolidation`` — the eval sentinel's no-new-table channel), the week's
``search_misses`` (top-10 by count), the promote surface, and the PRIOR weekly
item's meta: promotes have no ``resolved_at`` to window on, so 'promoted this
week' is a CHECKPOINT DIFF against the ``promoted_total`` each weekly persists.
WRITES: one kb-system ``report`` item (full table, ``meta.weekly_report``) and
ONE push through kbcore.notify — pure ASCII, <20 lines, sent with
``title='KB weekly'`` (push_digest keyword override, review #5 ops-1) so the
phone distinguishes it from the nightly at a glance. Search-miss
retention rides along (:func:`kbcore.search_misses.sweep_old`, the migration-018
carve-out). Counts keys are ADDED, never renamed: ``weekly_report_pushed``,
``weekly_report_item_id``, ``search_misses_swept``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from kbcore import notify, review_promote, search_misses
from kbcore.config import settings
from kbcore.db import connect
from kbcore.drainsched import _DAYS  # the ONE mon..sun table (no second copy)
from kbcore.ingest import ingest

# THE ESTABLISHED SEAM (mechanical split, review #5 Wave F): rendering moved to
# kbworker.weekly_render when the promote table + injection fence reached this
# file's 300-line ratchet. Every caller and test keeps reaching the names here
# (``weekly.PUSH_MAX_LINES``, …) — same pattern as kbworker.tail_steps/digest.
from kbworker.weekly_render import (  # noqa: F401
    MISSES_FENCE,
    PROMOTE_TABLE_ROWS,
    PUSH_MAX_LINES,
    PUSH_QUERY_CHARS,
    PUSH_TOP_MISSES,
    REPORT_QUERY_CHARS,
    TOP_MISSES,
    WINDOW_DAYS,
    _ascii,
    _cell,
    _push_lines,
    _report_body,
    _series,
    _sum,
    _trend,
)

logger = logging.getLogger("kbworker.weekly")

PROMOTE_SCAN_LIMIT = 50  # candidate rows scanned to fill the eval-preferred table


def maybe_weekly(counts: dict[str, Any], now: datetime | None = None) -> bool:
    """Run the weekly report iff tonight is the configured day. Never raises.

    Day/knob-gated INSIDE (the topup pattern): most nights this returns False
    having touched nothing and added NO counts key (positive-only, like the
    digest's lines). On the weekly day it records ``weekly_report_pushed`` —
    False there means 'assembled but the push failed or the channel is off',
    which still leaves the report ITEM in the KB. A same-week rerun (boot
    catch-up) skips without recording anything.
    """
    if not settings.kb_weekly_report or not _is_weekly_day(now):
        return False
    try:
        pushed = _run(counts)
    except Exception:  # noqa: BLE001 — the weekly may never fail the nightly that carries it
        logger.exception("weekly report failed; the nightly run is unaffected")
        pushed = False
    if pushed is None:  # already reported this week — not a failure, no counts key
        return False
    counts["weekly_report_pushed"] = pushed
    return pushed


def _is_weekly_day(now: datetime | None = None) -> bool:
    """LOCAL weekday == ``kb_weekly_day``; a malformed knob is OFF, one warning.

    Local time like every other day gate (kbcore.drainsched, the topup cutoff),
    and it fails toward OFF because the failure mode of ON is a bogus weekly
    item minted every single night.
    """
    spec = (settings.kb_weekly_day or "").strip().lower()
    day = _DAYS.get(spec)
    if day is None:
        logger.warning("KB_WEEKLY_DAY %r is not one of mon..sun — the weekly report is OFF", spec)
        return False
    return (now or datetime.now()).weekday() == day


def _run(counts: dict[str, Any]) -> bool | None:
    """Assemble + persist + push one weekly report. None = already done this week."""
    now = datetime.now(UTC)
    prior = _prior_weekly()
    if prior is not None and (now - prior[0]).days < WINDOW_DAYS - 1:
        logger.info("weekly report already written %s — skipping this run", prior[0].date())
        return None
    reports = _nightly_reports()
    miss_stats = search_misses.stats(days=WINDOW_DAYS)
    top = search_misses.top_misses(days=WINDOW_DAYS, limit=TOP_MISSES)
    candidates = review_promote.count_promote_candidates()
    promote_rows = _promote_rows(reports)
    promoted_total = _promoted_total()
    promoted_7d = None
    if prior is not None:
        prev = prior[1].get("promoted_total")
        if isinstance(prev, int) and not isinstance(prev, bool):
            promoted_7d = max(0, promoted_total - prev)
    swept = search_misses.sweep_old()
    counts["search_misses_swept"] = swept

    meta: dict[str, Any] = {
        "weekly_report": True,
        "nights": len(reports),
        "search_misses_7d": miss_stats["total"],
        "search_misses_distinct_7d": miss_stats["distinct"],
        "promote_candidates": candidates,
        "promoted_total": promoted_total,  # next week's checkpoint
        "search_misses_swept": swept,
    }
    if promoted_7d is not None:
        meta["promoted_7d"] = promoted_7d
    body = _report_body(
        now,
        reports,
        miss_stats,
        top,
        candidates,
        promoted_total,
        promoted_7d,
        swept,
        promote_rows,
    )
    item_id = ingest(
        "kb-system",
        "report",
        f"KB weekly report {now.date().isoformat()}",
        body,
        kind="chat",
        uri=f"weekly-report:{now.isoformat()}",
        trust="verified",
        meta=meta,
    )
    counts["weekly_report_item_id"] = item_id
    lines = _push_lines(now, reports, miss_stats, top, candidates, promoted_7d, item_id)
    pushed = bool(notify.push_digest(lines, title="KB weekly"))
    logger.info(
        "weekly report written (item %s), push %s", item_id, "sent" if pushed else "not sent"
    )
    return pushed


def _prior_weekly() -> tuple[datetime, dict[str, Any]] | None:
    """Newest weekly item's ``(created_at, meta)`` — the checkpoint + rerun guard."""
    with connect() as conn:
        row = conn.execute(
            "SELECT created_at, meta FROM items "
            "WHERE project = %s AND type = 'report' AND meta @> %s "
            "ORDER BY created_at DESC LIMIT 1",
            ("kb-system", Jsonb({"weekly_report": True})),
        ).fetchone()
    if row is None:
        return None
    return row[0], (row[1] if isinstance(row[1], dict) else {})


def _nightly_reports(days: int = WINDOW_DAYS) -> list[tuple[datetime, dict[str, Any]]]:
    """The window's nightly consolidation reports, OLDEST first (the trend axis).

    The report item's meta carries the run's whole counts dict (consolidate's
    ``_write_report``), so the dial series — eval metrics, unverified share —
    need no new table.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT created_at, meta FROM items "
            "WHERE project = %s AND type = 'report' AND meta @> %s "
            "  AND created_at >= now() - make_interval(days => %s) "
            "ORDER BY created_at",
            ("kb-system", Jsonb({"consolidation": True}), int(days)),
        ).fetchall()
    return [(r[0], r[1] if isinstance(r[1], dict) else {}) for r in rows]


def _promoted_total() -> int:
    """Extractions whose source is NOW verified — each crossed the owner-only
    promote chokepoint at some point. No ``resolved_at`` exists to window on, so
    the weekly diffs this total against the prior weekly item's checkpoint."""
    with connect() as conn:
        row = conn.execute(
            "SELECT count(DISTINCT rq.item_id) FROM review_queue rq "
            "JOIN items i ON i.id = rq.item_id "
            "JOIN sources s ON s.id = i.source_id "
            "WHERE rq.reason = 'unverified_extraction' AND rq.resolved = true "
            "  AND s.trust = 'verified'"
        ).fetchone()
    return int(row[0])


def _promote_rows(reports: list[tuple[datetime, dict[str, Any]]]) -> list[dict[str, Any]]:
    """The report body's promote-candidate rows, eval-top-5-preferred (strat-3).

    Reads the NEWEST nightly report meta carrying ``eval_top5_item_ids`` (the
    ids the sentinel persists at compute time), then orders the candidate
    surface's page so candidates the eval actually surfaces come first — the
    steering-resistant candidate ORDER within each partition is untouched
    (kbcore.review_promote's owner-days ranking). READ-ONLY; the paste-ready
    command the table ends with is run by the HUMAN (V4 — no new authority).
    """
    top5_ids: set[int] = set()
    for _at, meta in reversed(reports):  # reports are oldest-first
        ids = meta.get("eval_top5_item_ids")
        if isinstance(ids, list):
            top5_ids = {i for i in ids if isinstance(i, int) and not isinstance(i, bool)}
            break
    rows = review_promote.list_promote_candidates(limit=PROMOTE_SCAN_LIMIT)
    preferred = [r for r in rows if r["item_id"] in top5_ids]
    rest = [r for r in rows if r["item_id"] not in top5_ids]
    chosen = (preferred + rest)[:PROMOTE_TABLE_ROWS]
    return [{**r, "in_eval_top5": r["item_id"] in top5_ids} for r in chosen]

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
ONE push through kbcore.notify — pure ASCII, <20 lines, titled by its first
line ('KB weekly …') since the shared channel owns the ntfy header. Search-miss
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

logger = logging.getLogger("kbworker.weekly")

WINDOW_DAYS = 7
TOP_MISSES = 10  # rows tabled in the report item; PUSH_TOP_MISSES of them pushed
PUSH_TOP_MISSES = 3
PUSH_MAX_LINES = 20  # contract: the push stays under this (ntfy to a phone)
PUSH_QUERY_CHARS = 48  # a missed query is previewed on the phone, not quoted whole


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
    item_id = ingest(
        "kb-system",
        "report",
        f"KB weekly report {now.date().isoformat()}",
        _report_body(now, reports, miss_stats, top, candidates, promoted_total, promoted_7d, swept),
        kind="chat",
        uri=f"weekly-report:{now.isoformat()}",
        trust="verified",
        meta=meta,
    )
    counts["weekly_report_item_id"] = item_id
    lines = _push_lines(now, reports, miss_stats, top, candidates, promoted_7d, item_id)
    pushed = bool(notify.push_digest(lines))
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


def _series(reports: list[tuple[datetime, dict[str, Any]]], key: str) -> list[float]:
    """The numeric series a meta key traces across the window's reports."""
    out: list[float] = []
    for _at, meta in reports:
        v = meta.get(key)
        if isinstance(v, int | float) and not isinstance(v, bool):
            out.append(float(v))
    return out


def _trend(vals: list[float]) -> str:
    """``first -> last`` over a series; a single point stands alone; empty = n/a."""
    if not vals:
        return "n/a"
    if len(vals) == 1:
        return f"{vals[0]:.3f}"
    return f"{vals[0]:.3f} -> {vals[-1]:.3f}"


def _sum(reports: list[tuple[datetime, dict[str, Any]]], key: str) -> int:
    total = 0
    for _at, meta in reports:
        v = meta.get(key)
        if isinstance(v, int | float) and not isinstance(v, bool):
            total += int(v)
    return total


def _ascii(text: str, limit: int) -> str:
    """Pure-ASCII single-line preview for the phone push, bounded to ``limit``."""
    flat = " ".join(str(text).split())
    return flat.encode("ascii", "replace").decode("ascii")[:limit]


def _push_lines(
    now: datetime,
    reports: list[tuple[datetime, dict[str, Any]]],
    miss_stats: dict[str, int],
    top: list[tuple[str, int]],
    candidates: int,
    promoted_7d: int | None,
    item_id: int | None,
) -> list[str]:
    """The <20-line, pure-ASCII 'KB weekly' push body."""
    lines = [
        f"KB weekly {now.date().isoformat()}",
        f"nights reported: {len(reports)}",
        (
            f"7d: drained {_sum(reports, 'queued_drained')}, "
            f"saved {_sum(reports, 'queued_items_saved')}, "
            f"triaged {_sum(reports, 'review_triaged')}"
        ),
        f"eval hit@5: {_trend(_series(reports, 'eval_hit_at_5'))}",
        f"eval mrr: {_trend(_series(reports, 'eval_mrr'))}",
        f"top5 unverified: {_trend(_series(reports, 'eval_top5_unverified_share'))}",
        f"search misses: {miss_stats['total']} ({miss_stats['distinct']} distinct)",
    ]
    for q, n in top[:PUSH_TOP_MISSES]:
        lines.append(f"  {n}x {_ascii(q, PUSH_QUERY_CHARS)}")
    if promoted_7d is None:
        lines.append(f"promotes: checkpoint minted; {candidates} candidates open")
    else:
        lines.append(f"promotes: {promoted_7d} in 7d, {candidates} candidates open")
    lines.append(f"full table: kb-system item {item_id}")
    return lines[:PUSH_MAX_LINES]


def _cell(v: Any, floats: bool = False) -> str:
    ok = not isinstance(v, bool) and isinstance(v, int | float)
    return (f"{float(v):.3f}" if floats else str(int(v))) if ok else "-"


def _report_body(
    now: datetime,
    reports: list[tuple[datetime, dict[str, Any]]],
    miss_stats: dict[str, int],
    top: list[tuple[str, int]],
    candidates: int,
    promoted_total: int,
    promoted_7d: int | None,
    swept: int,
) -> str:
    """The full weekly table persisted as the kb-system report item's body."""
    lines = [
        "KB weekly trend report",
        "",
        f"window: {WINDOW_DAYS}d ending {now.date().isoformat()}; nightly reports: {len(reports)}",
        "",
        "night       drained  saved  triaged  hit@5   mrr     unv-top5",
    ]
    for at, meta in reports:
        lines.append(
            f"{at.date().isoformat():<11} "
            f"{_cell(meta.get('queued_drained')):>7}  "
            f"{_cell(meta.get('queued_items_saved')):>5}  "
            f"{_cell(meta.get('review_triaged')):>7}  "
            f"{_cell(meta.get('eval_hit_at_5'), floats=True):>6}  "
            f"{_cell(meta.get('eval_mrr'), floats=True):>6}  "
            f"{_cell(meta.get('eval_top5_unverified_share'), floats=True):>8}"
        )
    lines += [
        "",
        f"search misses ({WINDOW_DAYS}d): {miss_stats['total']} total, "
        f"{miss_stats['distinct']} distinct; top {len(top)}:",
    ]
    for q, n in top:
        lines.append(f"  {n:>3}x  {q}")
    if promoted_7d is None:
        promoted = f"checkpoint minted (total promoted extractions: {promoted_total})"
    else:
        promoted = f"{promoted_7d} promoted in 7d (total {promoted_total})"
    lines += [
        "",
        f"promote uptake: {promoted}; {candidates} candidates open",
        f"search-miss retention: {swept} row(s) older than {search_misses.RETENTION_DAYS}d pruned",
    ]
    return "\n".join(lines)

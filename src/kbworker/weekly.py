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
``search_misses`` (top-10 by count + the calibration percentiles), the promote
surface, the typed-edge totals (:func:`kbcore.save_nudge.typed_edge_counts`,
its recorded first caller — review #8 H9b), and the PRIOR weekly item's meta:
promotes and typed edges have no ``resolved_at``/window to count on, so
'this week' is a CHECKPOINT DIFF against the ``promoted_total`` /
``typed_edge_totals`` each weekly persists.
WRITES: one kb-system ``report`` item (full table, ``meta.weekly_report``) and
ONE push through kbcore.notify — pure ASCII, <20 lines, sent with
``title='KB weekly'`` (push_digest keyword override, review #5 ops-1) so the
phone distinguishes it from the nightly at a glance. Search-miss
retention rides along (:func:`kbcore.search_misses.sweep_old`, the migration-018
carve-out). Counts keys are ADDED, never renamed: ``weekly_report_pushed``,
``weekly_report_item_id``, ``search_misses_swept``. Display dates are LOCAL
(review #8 H9f; Invariant 7 — stored UTC, local only at display).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from psycopg.types.json import Jsonb

from kbcore import notify, review_promote, search_misses
from kbcore.config import settings
from kbcore.db import connect
from kbcore.drainsched import _DAYS  # the ONE mon..sun table (no second copy)
from kbcore.ingest import ingest
from kbworker.digest_lines import window_bounds
from kbworker.weekly_instruments import nights, typed_edge_diff, window_reports

# Review #8 H9 instrument reads (typed-edge totals, calibration percentiles):
# one fail-open query each, in kbworker.weekly_reads so this file stays under
# its cap; re-imported here so ``weekly._calibration_stats`` keeps resolving.
from kbworker.weekly_reads import _calibration_stats, _typed_edge_totals  # noqa: F401

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
    _trend_int,
    _verified_series,
    _verified_top5,
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


def _nights_through(prior: tuple | None) -> date | None:
    """The prior weekly's last reported night (``nights_through`` meta), or None."""
    raw = prior[1].get("nights_through") if prior else None
    try:
        return date.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None


def _run(counts: dict[str, Any]) -> bool | None:
    """Assemble + persist + push one weekly report. None = already done this week."""
    now = datetime.now(UTC)
    prior = _prior_weekly()
    if prior is not None and (now - prior[0]).days < WINDOW_DAYS - 1:
        logger.info(
            "weekly report already written %s (item %s) — skipping this run",
            prior[0].astimezone().date(),  # LOCAL, like the title (review #9a INS-9)
            prior[2],
        )
        return None
    # Review #9a INS-6: fetch one day more than the window and trim to WHOLE
    # COMPLETED local nights, tiling exactly after last week's nights_through —
    # so no run is ever lost or double-counted across consecutive weeklies.
    start_after = _nights_through(prior)  # exact tiling after last week (INS-6 fix)
    reports = window_reports(
        _nightly_reports(WINDOW_DAYS + 1), now, nights_back=WINDOW_DAYS, start_after=start_after
    )
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
    # Review #8 H9b/d: the typed-edge checkpoint (same pattern as promoted_total)
    # and the calibration percentiles — each fail-open to None (rendered n/a).
    typed_totals = _typed_edge_totals()
    typed_diff = typed_edge_diff(typed_totals, prior[1].get("typed_edge_totals") if prior else None)
    calibration = _calibration_stats()
    swept = search_misses.sweep_old()
    counts["search_misses_swept"] = swept

    meta: dict[str, Any] = {
        "weekly_report": True,
        "nights": len(reports),  # the RUN count (the checkpoint diff's axis, unchanged)
        "local_nights": len(nights(reports)),  # H9f: rows the table actually shows
        "search_misses_7d": miss_stats["total"],
        "search_misses_distinct_7d": miss_stats["distinct"],
        "promote_candidates": candidates,
        "promoted_total": promoted_total,  # next week's checkpoint
        "search_misses_swept": swept,
        "nights_through": window_bounds(now, None, WINDOW_DAYS, start_after)[1].isoformat(),
    }
    if promoted_7d is not None:
        meta["promoted_7d"] = promoted_7d
    if typed_totals is not None:
        meta["typed_edge_totals"] = typed_totals  # next week's checkpoint
    if typed_diff is not None:
        meta["typed_edges_7d"] = typed_diff
    if calibration is not None:
        meta["calibration_7d"] = calibration
    local_now = now.astimezone()  # display only: header/title dates are LOCAL (H9f)
    body = _report_body(
        local_now,
        reports,
        miss_stats,
        top,
        candidates,
        promoted_total,
        promoted_7d,
        swept,
        promote_rows,
        typed_totals=typed_totals,
        typed_diff=typed_diff,
        calibration=calibration,
        floor=settings.kb_miss_relevance_floor,
        expire_cap=settings.kb_review_expire_cap,
        start_after=start_after,
    )
    item_id = ingest(
        "kb-system",
        "report",
        f"KB weekly report {local_now.date().isoformat()}",
        body,
        kind="chat",
        uri=f"weekly-report:{now.isoformat()}",  # the UTC instant stays the identity
        trust="verified",
        meta=meta,
    )
    counts["weekly_report_item_id"] = item_id
    lines = _push_lines(
        local_now,
        reports,
        miss_stats,
        top,
        candidates,
        promoted_7d,
        item_id,
        typed_totals=typed_totals,
        typed_diff=typed_diff,
    )
    pushed = bool(notify.push_digest(lines, title="KB weekly"))
    logger.info(
        "weekly report written (item %s), push %s", item_id, "sent" if pushed else "not sent"
    )
    return pushed


def _prior_weekly() -> tuple[datetime, dict[str, Any], int] | None:
    """Newest weekly item's ``(created_at, meta, id)`` — the checkpoint + rerun guard."""
    with connect() as conn:
        row = conn.execute(
            "SELECT created_at, meta, id FROM items "
            "WHERE project = %s AND type = 'report' AND meta @> %s "
            "ORDER BY created_at DESC LIMIT 1",
            ("kb-system", Jsonb({"weekly_report": True})),
        ).fetchone()
    if row is None:
        return None
    return row[0], (row[1] if isinstance(row[1], dict) else {}), int(row[2])


def _nightly_reports(days: int = WINDOW_DAYS) -> list[tuple[datetime, dict[str, Any]]]:
    """The last ``days`` x 24 h of nightly consolidation reports, OLDEST first.

    The report item's meta carries the run's whole counts dict (consolidate's
    ``_write_report``), so the dial series — eval metrics, unverified share —
    need no new table. The caller trims this to whole local nights
    (``weekly_instruments.window_reports``); the SQL cut is only the fetch.
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

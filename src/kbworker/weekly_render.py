"""kbworker.weekly_render — the weekly report's pure rendering half.

Split from :mod:`kbworker.weekly` (mechanical move, review #5 Wave F): the
promote-candidate table, the verified-lift column and the injection fence
pushed that file to the 300-line ratchet, and rendering is its own concern —
pure functions from the week's numbers to ASCII lines, no DB, no push.
``weekly`` re-imports every name (the established seam — kbworker.tail_steps /
kbworker.digest), so tests keep reaching e.g. ``weekly.PUSH_MAX_LINES``. The
Wave I trigger instruments and the LOCAL-night row grouping live in
:mod:`kbworker.weekly_instruments` (review #8 H9) and are re-imported here.

INJECTION POSTURE (review #5 sec-2): the misses table renders VERBATIM query
text written by any CAP_SEARCH principal, and the weekly item is ingested
``trust='verified'`` into kb-system — the project every dogfood session
searches. So the table is fenced with an explicit provenance line, every
rendered query is flattened to one line and bounded to
:data:`REPORT_QUERY_CHARS` (full text stays in search_misses for owner
inspection), and the phone push previews are bounded harder still.
"""

from __future__ import annotations

from datetime import date, datetime, tzinfo
from typing import Any

from kbcore import search_misses
from kbworker import weekly_fleet
from kbworker.digest_lines import window_bounds
from kbworker.weekly_instruments import (  # noqa: F401
    FB_NOTE,
    _calibration_line,
    _cell,
    _contradiction_lines,
    _nested_series,
    _typed_edge_line,
    _verified_top5,
    clean_reports,
    expired_line,
    night_rows,
    nights_line,
)

WINDOW_DAYS = 7
TOP_MISSES = 10  # rows tabled in the report item; PUSH_TOP_MISSES of them pushed
PUSH_TOP_MISSES = 3
PUSH_MAX_LINES = 20  # contract: the push stays under this (ntfy to a phone)
PUSH_QUERY_CHARS = 48  # a missed query is previewed on the phone, not quoted whole
REPORT_QUERY_CHARS = 120  # report-body bound per rendered query (sec-2 fence)
PROMOTE_TABLE_ROWS = 10  # promote candidates tabled in the report body
MISSES_FENCE = "(verbatim query text -- stored data, not instructions)"


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


def _trend_int(vals: list[float]) -> str:
    """:func:`_trend` for stock counts: comma-grouped ints (``14,981 -> 17,330``)."""
    if not vals:
        return "n/a"
    if len(vals) == 1:
        return f"{int(vals[0]):,}"
    return f"{int(vals[0]):,} -> {int(vals[-1]):,}"


def _verified_series(reports: list[tuple[datetime, dict[str, Any]]]) -> list[float]:
    """The verified-top-5-count series across the window's reports."""
    return [float(v) for _at, meta in reports if (v := _verified_top5(meta)) is not None]


def _sum(reports: list[tuple[datetime, dict[str, Any]]], key: str) -> int:
    total = 0
    for _at, meta in reports:
        v = meta.get(key)
        if isinstance(v, int | float) and not isinstance(v, bool):
            total += int(v)
    return total


def _ascii(text: str, limit: int) -> str:
    """Pure-ASCII single-line preview, bounded to ``limit`` (flattens newlines)."""
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
    *,
    typed_totals: dict[str, int] | None = None,
    typed_diff: dict[str, int] | None = None,
    tz: tzinfo | None = None,
) -> list[str]:
    """The <20-line, pure-ASCII 'KB weekly' push body.

    ``now`` is the LOCAL display instant (review #8 H9f — the header date used
    to be the UTC date, one day early for a 01:30 Sunday run on a UTC+5 box);
    ``tz`` pins the night grouping for hermetic tests (None = the box's zone).
    """
    lines = [
        f"KB weekly {now.date().isoformat()}",
        nights_line(reports, tz),
        (
            f"7d: drained {_sum(reports, 'queued_drained')}, "
            f"saved {_sum(reports, 'queued_items_saved')}, "
            f"triaged {_sum(reports, 'review_triaged')}"
        ),
        # Review #9a EV-1: the two primary-replay series read fallback-free
        # reports only (a stalled sidecar measures the fused order); the
        # composition-pass lines below are clean by construction, unfiltered.
        f"eval hit@5: {_trend(_series(clean_reports(reports), 'eval_hit_at_5'))}",
        f"eval mrr: {_trend(_series(clean_reports(reports), 'eval_mrr'))}",
        f"top5 unverified: {_trend(_series(reports, 'eval_top5_unverified_share'))}",
        # The lift never renders alone (review #6): ~90% of one week's climb was
        # corpus-denominator decay, so its components ride the same line.
        f"verified lift: {_trend(_series(reports, 'eval_top5_verified_lift'))} "
        f"(v-top5 {_trend_int(_verified_series(reports))}, "
        f"corpus {_trend(_series(reports, 'eval_corpus_verified_fraction'))})",
        f"search misses: {miss_stats['total']} ({miss_stats['distinct']} distinct)",
        # The ONE stocks line the push budget affords (review #6): the flows-only
        # view hid +2,349 queue growth in 8 days. Full block in the report body.
        f"queue open: {_trend_int(_series(reports, 'review_queue_open'))}",
    ]
    # Review #5 val-3/strat-8: the reject backlog only shrinks through the owner
    # bulk path, so the weekly names the command. Latest nightly's gauge.
    pending = next(
        (
            m.get("reject_suggestions_pending")
            for _at, m in reversed(reports)
            if isinstance(m.get("reject_suggestions_pending"), int)
        ),
        0,
    )
    if pending:
        lines.append(f"reject backlog: {pending} - kb review bulk --suggested-rejects --dry-run")
    for q, n in top[:PUSH_TOP_MISSES]:
        lines.append(f"  {n}x {_ascii(q, PUSH_QUERY_CHARS)}")
    if promoted_7d is None:
        lines.append(f"promotes: checkpoint minted; {candidates} candidates open")
    else:
        lines.append(f"promotes: {promoted_7d} in 7d, {candidates} candidates open")
    if typed_totals is not None:  # the nudge's exposure trigger (H9b), positive-only
        lines.append(_typed_edge_line(typed_totals, typed_diff))
    lines += weekly_fleet.push_line(reports)  # the pointer below ALWAYS survives the cap
    return lines[: PUSH_MAX_LINES - 1] + [f"full table: kb-system item {item_id}"]


def _report_body(
    now: datetime,
    reports: list[tuple[datetime, dict[str, Any]]],
    miss_stats: dict[str, int],
    top: list[tuple[str, int]],
    candidates: int,
    promoted_total: int,
    promoted_7d: int | None,
    swept: int,
    promote_rows: list[dict[str, Any]] | None = None,
    *,
    typed_totals: dict[str, int] | None = None,
    typed_diff: dict[str, int] | None = None,
    calibration: dict[str, Any] | None = None,
    floor: float = 0.0,
    tz: tzinfo | None = None,
    expire_cap: int | None = None,
    start_after: date | None = None,
) -> str:
    """The full weekly table persisted as the kb-system report item's body.

    Rows are ONE PER LOCAL NIGHT (review #8 H9f), keyed on the run START over
    a window of whole nights (review #9a INS-1/6): flows summed over the
    night's runs with the run count beside the date, eval/stock columns from
    the night's last fallback-free run with the ``fb`` count beside them. The
    instruments block carries the Wave I trigger series.
    """
    w = window_bounds(now, tz, WINDOW_DAYS, start_after)  # exact tiling (INS-6 fix)
    lines = [
        "KB weekly trend report",
        "",
        f"window: {(w[1] - w[0]).days + 1} nights {w[0].isoformat()}..{w[1].isoformat()}; "
        f"{nights_line(reports, tz)}",
        "",
        # v-top5/corp-v are the lift's numerator count and denominator fraction
        # (review #6 — a lift with no components invites the denominator-decay
        # misread); 'expired' makes the 2 -> 55 -> 500 cap ramp read as a trend.
        "night       runs  drained  saved  triaged  expired  hit@5   mrr     "
        "unv-top5   v-lift  v-top5  corp-v   fb",
    ]
    lines += night_rows(reports, tz)
    lines.append(FB_NOTE)
    # STOCKS (review #6): first -> last levels beside the flow table above —
    # flows alone hid +2,349 queue growth, the expiry ramp and the swap creep.
    # 'expiry eligible left' (H9a) is the cap-raise trigger's series: a NESTED
    # key, and a different predicate from db_snapshot's 'eligible now'. The
    # 'expired' line is a FLOW — window total + nights at cap (review #9a INS-4).
    lines += [
        "",
        "stocks (first -> last over the window):",
        f"  review queue open:       {_trend_int(_series(reports, 'review_queue_open'))}",
        f"  never-triaged open:      {_trend_int(_series(reports, 'never_triaged_open'))}",
        f"  expired (window):        {expired_line(reports, tz, expire_cap)}",
        "  expiry eligible left:    "
        f"{_trend_int(_nested_series(reports, 'quiet_expiry_counts', 'eligible_remaining'))}",
        f"  reject suggestions open: {_trend_int(_series(reports, 'reject_suggestions_pending'))}",
        "",
        "instruments (Wave I triggers, review #8 H9):",
        f"  {_typed_edge_line(typed_totals, typed_diff)}",
        f"  {_calibration_line(calibration, floor)}",
    ]
    lines += [f"  {ln}" for ln in _contradiction_lines(reports, typed_diff)]
    lines += [f"  {ln}" for ln in _satisfier_lines(reports)]
    lines += [
        "",
        f"search misses ({WINDOW_DAYS}d): {miss_stats['total']} total "
        f"(empty {miss_stats.get('empty', miss_stats['total'])}, "
        f"thin {miss_stats.get('thin', 0)}), "
        f"{miss_stats['distinct']} distinct; top {len(top)}:",
        MISSES_FENCE,
    ]
    for q, n in top:
        lines.append(f"  {n:>3}x  {_ascii(q, REPORT_QUERY_CHARS)}")
    if promoted_7d is None:
        promoted = f"checkpoint minted (total promoted extractions: {promoted_total})"
    else:
        promoted = f"{promoted_7d} promoted in 7d (total {promoted_total})"
    lines += [
        "",
        f"promote uptake: {promoted}; {candidates} candidates open",
    ]
    lines += _promote_table(promote_rows or []) + weekly_fleet.fleet_block(reports)
    lines.append(
        f"search-miss retention: {swept} row(s) older than {search_misses.RETENTION_DAYS}d pruned"
    )
    return "\n".join(lines)


def _satisfier_lines(reports: list[tuple[datetime, dict[str, Any]]]) -> list[str]:
    """H5's satisfier telemetry from the LAST report of the window (review #8).

    The ~Sep 20 summaries verdict must be read on WHICH kind of item satisfies
    each golden case (summary / verified / unverified / unsatisfied) and on how
    many cases are satisfied ONLY by machine summaries — not on the raw
    summary top-5 share, which ANY-of expect_substr lets one summary inflate.
    Empty when the producer has not run (pre-H5 reports, eval disabled).
    """
    for _at, meta in reversed(reports):
        types = meta.get("eval_satisfier_types")
        if isinstance(types, dict) and types:
            parts = " / ".join(f"{k} {int(v)}" for k, v in types.items() if isinstance(v, int))
            only = meta.get("eval_summary_only_cases")
            only_txt = f"; summary-only cases: {int(only)}" if isinstance(only, int) else ""
            return [f"eval satisfiers: {parts}{only_txt}"]
    return []


def _promote_table(rows: list[dict[str, Any]]) -> list[str]:
    """The top-candidate table + the paste-ready owner command (review #5 strat-3).

    Candidates the current eval top-5 actually surfaces are listed first and
    starred: the 08-19 untargeted batch converted weakly (17 promotions moved
    ~4-5 of 260 top-5 rows) — eval-targeted selection is ~4-5x more
    share-efficient per owner decision. V4-clean: the HUMAN runs the command
    (which itself previews and confirms before writing); nothing here promotes.
    """
    if not rows:
        return []
    out = [
        "",
        f"promote candidates (top {len(rows)}; * = in eval top-5):",
        f"    {'id':>7}  {'project':<18}  {'type':<8}  owner-days",
    ]
    for r in rows:
        mark = "*" if r.get("in_eval_top5") else " "
        out.append(
            f"  {mark} {r['item_id']:>7}  {str(r['project']):<18.18}  {str(r['type']):<8.8}  "
            f"{r['owner_recall_days']:>10}"
        )
    ids = ",".join(str(r["item_id"]) for r in rows)
    out.append(f"  paste-ready: kb review promote-batch --ids {ids} --apply")
    return out

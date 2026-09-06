"""kbworker.weekly_instruments — Wave I trigger instruments + LOCAL-night rows.

Review #8 H9 (KB 31857). Pure functions over the week's nightly report metas,
split out of :mod:`kbworker.weekly_render` (254/300) so the instruments the
registry's Wave I triggers read have a home:

* expiry ``eligible_remaining`` first -> last — the expiry cap-raise trigger's
  series (a NESTED key under ``quiet_expiry_counts``; never compare it with
  db_snapshot's 'eligible now', a different predicate);
* the typed-edge CHECKPOINT DIFF (the nudge's <5/wk kill rule; exactly the
  ``promoted_total`` pattern — no ``created_at`` window, a snapshot diffed
  against the prior weekly's persisted totals), widened to ``alternative_to``
  so 'alternative_to linked 7d' is the same diff, not a second query;
* calibration n / p10 / p50 / p90 (the thin-floor re-tune's evidence);
* decisive contradiction suggestions + wall-blocked closed vs alternative_to
  linked (the unlinked cross-project dup class);
* ONE ROW PER LOCAL NIGHT with a runs column: a fragmented night (pause +
  resume, boot catch-up) rendered as two rows and doubled the flows, and a UTC
  date filed every pre-dawn run under the wrong day on this UTC+5 box. Stored
  UTC, local only at display (Invariant 7); the night rule is
  :func:`kbworker.digest_lines.night_of`, shared with the digest's RESUME line,
  keyed on the run's START (:func:`run_start` — review #9a INS-1) over a window
  of whole nights (:func:`window_reports` — INS-6);
* the eval cells of a night come from its last rerank-fallback-free run and an
  ``fb`` column carries the night's fallback count (INS-5 / EV-5), and the
  'expired' stocks line is the window TOTAL plus nights at the cap (INS-4).

Every reader tolerates an absent key — an older report contributes nothing to
a series and a missing instrument renders ``n/a``. Pure ASCII output.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from kbworker.digest_lines import night_of, window_bounds

Report = tuple[datetime, dict[str, Any]]
FB_NOTE = "(fb = rerank fallback cases over the night's runs; eval cells from its last fb==0 run)"

# Render order of the weekly typed-edge diff: the nudge's three
# (kbcore.save_nudge.TYPED_EDGE_RELS) plus the cross-twin linker's edge.
WEEKLY_EDGE_RELS = ("fixes", "depends_on", "implements", "alternative_to")
# The per-night flow columns (summed over a night's runs); the eval/stock
# columns come from the night's last fallback-free run instead (_eval_run).
FLOW_KEYS = ("queued_drained", "queued_items_saved", "review_triaged", "review_expired")


def _num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _cell(v: Any, floats: bool = False) -> str:
    return (f"{float(v):.3f}" if floats else str(int(v))) if _num(v) else "-"


def _verified_top5(meta: dict[str, Any]) -> int | None:
    """The night's verified top-5 hit COUNT — the lift NUMERATOR (review #6).

    Wave-G sentinels persist it as ``eval_top5_verified``; older reports carry
    only the parts (``eval_top5_results`` − ``eval_top5_unverified``), so the
    trend does not start blind. None when the instrument never ran that night.
    """
    v = meta.get("eval_top5_verified")
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    total, unv = meta.get("eval_top5_results"), meta.get("eval_top5_unverified")
    if all(isinstance(x, int) and not isinstance(x, bool) for x in (total, unv)):
        return int(total) - int(unv)
    return None


def _nested_series(reports: list[Report], outer: str, inner: str) -> list[float]:
    """The series a NESTED numeric key (``meta[outer][inner]``) traces; absent = skipped."""
    out: list[float] = []
    for _at, meta in reports:
        block = meta.get(outer)
        v = block.get(inner) if isinstance(block, Mapping) else None
        if _num(v):
            out.append(float(v))
    return out


def _latest(reports: list[Report], key: str) -> int | None:
    """The newest report's numeric ``key`` (a gauge, not a flow), else None."""
    for _at, meta in reversed(reports):
        if _num(meta.get(key)):
            return int(meta[key])
    return None


# --- typed edges: the checkpoint diff ----------------------------------------


def typed_edge_diff(totals: Any, prior: Any) -> dict[str, int] | None:
    """Per-rel growth since the prior weekly's checkpoint; None = checkpoint minted."""
    if not isinstance(totals, Mapping) or not isinstance(prior, Mapping):
        return None
    return {
        rel: max(0, int(totals[rel]) - int(prior[rel]))
        for rel in WEEKLY_EDGE_RELS
        if _num(totals.get(rel)) and _num(prior.get(rel))
    }


def _typed_edge_line(totals: Any, diff: Any) -> str:
    """``typed edges (7d): fixes +N depends_on +N implements +N alternative_to +N``.

    A checkpoint diff exactly like ``promoted_total``: the first weekly after
    this lands mints the checkpoint and says so with the raw totals; a rel the
    prior checkpoint did not carry renders its total as ``(new)``. Nothing is
    excluded yet — no retired edges exist (retirement is Wave I).
    """
    if not isinstance(totals, Mapping):
        return "typed edges (7d): n/a"
    if not isinstance(diff, Mapping):
        raw = " ".join(f"{r} {int(totals[r])}" for r in WEEKLY_EDGE_RELS if _num(totals.get(r)))
        return f"typed edges: checkpoint minted (totals {raw})"
    parts = []
    for rel in WEEKLY_EDGE_RELS:
        if rel in diff:
            parts.append(f"{rel} +{int(diff[rel])}")
        elif _num(totals.get(rel)):
            parts.append(f"{rel} {int(totals[rel])} (new)")
    return f"typed edges (7d): {' '.join(parts)}"


# --- calibration + contradictions -------------------------------------------


def _calibration_line(stats: Any, floor: float) -> str:
    """``calibration (7d): n=N, top-score p10/p50/p90 = a/b/c; thin floor 0.05``."""
    if not isinstance(stats, Mapping) or not _num(stats.get("n")):
        return "calibration (7d): n/a"
    n = int(stats["n"])
    if n == 0:
        return f"calibration (7d): n=0; thin floor {floor:g}"
    pcts = "/".join(
        f"{float(stats[k]):.3f}" if _num(stats.get(k)) else "-" for k in ("p10", "p50", "p90")
    )
    return f"calibration (7d): n={n}, top-score p10/p50/p90 = {pcts}; thin floor {floor:g}"


def _contradiction_lines(reports: list[Report], diff: Any) -> list[str]:
    """The H9e pair derivable from data the reports already carry.

    'decisive suggestions' is the newest night's ``contradiction_suggestions_pending``
    gauge; 'wall-blocked closed 7d' sums the nightly flow; 'alternative_to
    linked 7d' is the typed-edge checkpoint diff. (The open-contradictions-by-
    project breakdown needs a query the reports do not carry — Wave I.)
    """
    decisive = _latest(reports, "contradiction_suggestions_pending")
    wall = sum(
        int(m["wall_blocked_closed"]) for _a, m in reports if _num(m.get("wall_blocked_closed"))
    )
    alt = diff.get("alternative_to") if isinstance(diff, Mapping) else None
    return [
        f"decisive suggestions: {decisive if decisive is not None else 'n/a'}",
        f"wall-blocked closed 7d: {wall} | "
        f"alternative_to linked 7d: {alt if alt is not None else 'n/a'}",
    ]


# --- rows by LOCAL night -----------------------------------------------------


def run_start(at: datetime, meta: Mapping[str, Any]) -> datetime:
    """The run's START: its report instant minus ``run_secs`` (review #9a INS-1).

    ``created_at`` is stamped at the tail (``run_report._write_report``), i.e.
    the FINISH; ``run_secs`` is the H7 wall-clock key. A pre-H7 report carries
    none, so it keys on its finish as before — the only honest fallback.
    """
    secs = meta.get("run_secs")
    if _num(secs) and secs > 0:
        return at - timedelta(seconds=float(secs))
    return at


def nights(reports: list[Report], tz: tzinfo | None = None) -> list[tuple[date, list[Report]]]:
    """Group the window's reports by LOCAL night of their START, oldest first."""
    groups: dict[date, list[Report]] = {}
    for at, meta in reports:
        groups.setdefault(night_of(run_start(at, meta), tz), []).append((at, meta))
    return sorted(groups.items())


def window_reports(
    reports: list[Report],
    now: datetime,
    tz: tzinfo | None = None,
    nights_back: int = 7,
    start_after: date | None = None,
) -> list[Report]:
    """The reports of the COMPLETED local nights ``window_bounds`` names (review
    #9a INS-6): the caller fetches one extra day; this trims to whole nights,
    tiling exactly after the prior weekly when ``start_after`` is given. Order
    is preserved (oldest first)."""
    first, last = window_bounds(now, tz, nights_back, start_after)
    return [(at, m) for at, m in reports if first <= night_of(run_start(at, m), tz) <= last]


def _fallbacks(meta: Mapping[str, Any]) -> int | None:
    """``eval_rerank_tally.fallback`` as an int; None when the report has no tally."""
    tally = meta.get("eval_rerank_tally")
    fb = tally.get("fallback") if isinstance(tally, Mapping) else None
    return int(fb) if _num(fb) else None


def _eval_run(runs: list[Report]) -> dict[str, Any]:
    """The run a night's eval cells come from: its LAST fallback==0 run carrying
    ``eval_mrr`` (a report without the tally counts as clean — pre-H7a), else
    the last run of any kind (review #9a INS-5/EV-5: the mrr of a stalled
    replay must never sit in the row unmarked — the ``fb`` column marks it)."""
    clean = [m for _a, m in runs if not _fallbacks(m) and _num(m.get("eval_mrr"))]
    return clean[-1] if clean else runs[-1][1]


def night_fallbacks(runs: list[Report]) -> int | None:
    """The night's rerank fallback count summed over its runs; None when no run has a tally."""
    vals = [fb for _a, m in runs if (fb := _fallbacks(m)) is not None]
    return sum(vals) if vals else None


def clean_reports(reports: list[Report]) -> list[Report]:
    """The reports whose PRIMARY replay had no rerank fallback (review #9a EV-1).

    The push's eval hit@5 / MRR series read only these — a stalled sidecar
    measures the fused order (0.9423 read as a 'new high' for a day). A report
    with no tally (pre-H7a) stays eligible; the composition-pass numbers are
    never filtered — that replay is described by its own tally, not this one.
    """
    return [(at, m) for at, m in reports if not _fallbacks(m)]


def _expired(meta: Mapping[str, Any]) -> int:
    v = meta.get("review_expired")
    return int(v) if _num(v) else 0


def expired_line(reports: list[Report], tz: tzinfo | None = None, cap: int | None = None) -> str:
    """``N total; M night(s) at cap C`` — the window's expiry FLOW, rendered as one.

    Review #9a INS-4: the old ``expired/night: first -> last`` took a per-run
    flow's first and last values under the 'stocks' header, so a paused
    fragment at the end of the window rendered '500 -> 0' beside a table whose
    column summed 3,291. A night is 'at cap' when any of its runs expired >=
    ``cap`` (``kb_review_expire_cap``); without a cap only the total renders.
    """
    if not any(_num(m.get("review_expired")) for _a, m in reports):
        return "n/a"
    line = f"{sum(_expired(m) for _a, m in reports):,} total"
    if cap:
        at_cap = sum(
            1 for _n, runs in nights(reports, tz) if any(_expired(m) >= cap for _a, m in runs)
        )
        line += f"; {at_cap} night(s) at cap {cap:,}"
    return line


def nights_line(reports: list[Report], tz: tzinfo | None = None) -> str:
    """``nights reported: N (M runs)`` — nights are local, runs are the raw count."""
    return f"nights reported: {len(nights(reports, tz))} ({len(reports)} runs)"


def night_rows(reports: list[Report], tz: tzinfo | None = None) -> list[str]:
    """One table row per LOCAL night: flows SUMMED over its runs, the runs count
    in parentheses, eval/stock columns from :func:`_eval_run`, then ``fb``."""
    rows: list[str] = []
    for night, runs in nights(reports, tz):
        flows: dict[str, int | None] = {}
        for key in FLOW_KEYS:
            vals = [int(m[key]) for _a, m in runs if _num(m.get(key))]
            flows[key] = sum(vals) if vals else None
        last = _eval_run(runs)
        rows.append(
            f"{night.isoformat():<11} "
            f"{f'({len(runs)})':>4}  "
            f"{_cell(flows['queued_drained']):>7}  "
            f"{_cell(flows['queued_items_saved']):>5}  "
            f"{_cell(flows['review_triaged']):>7}  "
            f"{_cell(flows['review_expired']):>7}  "
            f"{_cell(last.get('eval_hit_at_5'), floats=True):>6}  "
            f"{_cell(last.get('eval_mrr'), floats=True):>6}  "
            f"{_cell(last.get('eval_top5_unverified_share'), floats=True):>8}  "
            f"{_cell(last.get('eval_top5_verified_lift'), floats=True):>7}  "
            f"{_cell(_verified_top5(last)):>6}  "
            f"{_cell(last.get('eval_corpus_verified_fraction'), floats=True):>6}  "
            f"{_cell(night_fallbacks(runs)):>3}"
        )
    return rows

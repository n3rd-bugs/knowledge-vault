"""kbworker.eval_trust — verified-lift ratio watch (review #5 val-4 / ret-trust-1).

Mechanizes KB 6495 point 3's recorded watch condition on the top-5 trust
composition. The RAW unverified share conflates corpus growth with ranking
behavior: the 51.4% → 65% climb was denominator drift (the corpus went ~91% →
96.6% unverified) while verified content's OVER-REPRESENTATION actually rose
~5.4x → ~10.8x. So the watched quantity is the LIFT —

    eval_top5_verified_lift = (1 - eval_top5_unverified_share)
                              / (verified fraction of the active corpus)

— how much better the verified minority surfaces than chance. Three trips, any
of which stamps ``eval_trust_status='watch'`` (rendered as a positive-only
digest line by kbworker.digest):

* share ceiling — top-5 unverified share > :data:`SHARE_CEILING` (0.75);
* lift floor — lift < :data:`LIFT_FLOOR` (5x): ranking has stopped preferring
  verified knowledge, whatever the corpus mix is doing;
* week-over-week move — lift moved more than :data:`WOW_MOVE` (30%) against
  the newest nightly report at least :data:`WOW_MIN_AGE_DAYS` old. A golden-set
  edit RESETS this baseline (the prior report's ``eval_queries`` differs, so
  the comparison is skipped — the two lifts measured different query sets).

There is deliberately NO floor on the raw unverified share (a falling share is
the goal), and near-tie nudges (trust 1.02, the collapse tiebreak) are not
expected to move it materially — this instrument watches, it never ranks.
Report-only, like every eval_sentinel instrument: an error here costs the
instrument, never the primary metrics.
"""

from __future__ import annotations

import logging
from typing import Any

from kbcore.db import connect

logger = logging.getLogger("kbworker.eval_trust")

SHARE_CEILING = 0.75  # top-5 unverified share above this is watch-worthy
LIFT_FLOOR = 5.0  # verified lift below this says ranking stopped preferring verified
WOW_MOVE = 0.30  # relative week-over-week lift move that trips the watch
WOW_MIN_AGE_DAYS = 6  # the WoW baseline must be at least this old (a week-ish ago)
WOW_LOOKBACK = 60  # newest-first reports scanned for a baseline carrying the lift


def verified_lift(unverified_share: float, golden_count: int) -> dict[str, Any]:
    """Lift + watch verdict for tonight's composition. Returns ADDED meta keys.

    ``eval_corpus_verified_fraction`` and (when the corpus has any verified
    item) ``eval_top5_verified_lift`` always; ``eval_trust_status='watch'`` and
    the human-readable ``eval_trust_watch`` reasons only when a trip fires, so
    a quiet night leaves the status to the sentinel's own 'ok'.
    """
    out: dict[str, Any] = {}
    reasons: list[str] = []
    frac = _corpus_verified_fraction()
    out["eval_corpus_verified_fraction"] = round(frac, 4)
    lift: float | None = None
    if frac > 0:
        lift = (1.0 - float(unverified_share)) / frac
        out["eval_top5_verified_lift"] = round(lift, 2)
    if unverified_share > SHARE_CEILING:
        reasons.append(f"top5 unverified share {unverified_share} > {SHARE_CEILING} ceiling")
    if lift is not None and lift < LIFT_FLOOR:
        reasons.append(f"verified lift {round(lift, 2)}x < {LIFT_FLOOR:g}x floor")
    prior = _week_ago_lift() if lift is not None else None
    if prior is not None:
        prior_lift, prior_queries = prior
        if prior_queries is not None and prior_queries != golden_count:
            # Golden-set edit: the two lifts measured different query sets, so
            # tonight establishes a fresh baseline instead of a false move.
            logger.info(
                "verified-lift WoW skipped: golden set changed (%s -> %s queries), baseline reset",
                prior_queries,
                golden_count,
            )
        elif prior_lift > 0 and abs(lift - prior_lift) / prior_lift > WOW_MOVE:
            reasons.append(
                f"verified lift moved {abs(lift - prior_lift) / prior_lift:.0%} WoW "
                f"({round(prior_lift, 2)}x -> {round(lift, 2)}x)"
            )
    if reasons:
        out["eval_trust_status"] = "watch"
        out["eval_trust_watch"] = reasons
        logger.warning("TRUST WATCH: %s", "; ".join(reasons))
    return out


def _corpus_verified_fraction() -> float:
    """Fraction of ACTIVE items whose source is verified — the lift denominator.

    Active only, because that is the retrievable corpus the top-5 draws from;
    counting archived/superseded rows would let dead weight fake a low base.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT count(*) FILTER (WHERE s.trust = 'verified'), count(*) "
            "FROM items i JOIN sources s ON s.id = i.source_id "
            "WHERE i.status = 'active'"
        ).fetchone()
    verified, total = int(row[0]), int(row[1])
    return verified / total if total else 0.0


def _week_ago_lift() -> tuple[float, int | None] | None:
    """``(lift, eval_queries)`` from the newest nightly report at least
    :data:`WOW_MIN_AGE_DAYS` old that carries the lift — the WoW baseline.

    Walks back over reports missing the key (instrument errored, pre-Wave-F
    nights, foreign ``type='report'`` items) exactly like
    ``consolidation.previous_eval_metrics`` walks over non-ok nights. None =
    no baseline yet; the first week just establishes one.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT meta FROM items "
            "WHERE project = 'kb-system' AND type = 'report' "
            "  AND created_at <= now() - make_interval(days => %s) "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (WOW_MIN_AGE_DAYS, WOW_LOOKBACK),
        ).fetchall()
    for (meta,) in rows:
        if not isinstance(meta, dict) or not meta.get("consolidation"):
            continue
        lift = meta.get("eval_top5_verified_lift")
        if isinstance(lift, int | float) and not isinstance(lift, bool):
            queries = meta.get("eval_queries")
            ok_q = isinstance(queries, int) and not isinstance(queries, bool)
            return float(lift), (int(queries) if ok_q else None)
    return None

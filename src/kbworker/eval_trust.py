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
* week-over-week move — the top-5 VERIFIED share (the lift NUMERATOR) moved
  more than :data:`WOW_MOVE` (30%) against the newest nightly report at least
  :data:`WOW_MIN_AGE_DAYS` old. The numerator, NOT the composite (review #6,
  see the trap below). A golden-set edit RESETS this baseline (the prior
  report's ``eval_queries`` differs, so the comparison is skipped — the two
  shares measured different query sets).

THE DENOMINATOR-DECAY TRAP (review #6). lift = numerator / denominator, and
the denominator — the corpus verified fraction — FALLS every night the corpus
grows faster than promotes land, which on this deployment is most nights. A
rising lift therefore does NOT mean ranking improved: ~90% of the
08-20..08-27 lift climb was pure denominator decay with the numerator flat.
Consequences wired in: the WoW trip watches the numerator share, and every
surface that renders the lift renders its components beside it
(``eval_top5_verified`` / ``eval_top5_results`` and
``eval_corpus_verified_fraction`` — digest TRUST WATCH line, weekly table and
push). Never read the composite alone.

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
WOW_MOVE = 0.30  # relative WoW move of the top-5 VERIFIED share that trips the watch
WOW_MIN_AGE_DAYS = 6  # the WoW baseline must be at least this old (a week-ish ago)
WOW_LOOKBACK = 60  # newest-first reports scanned for a baseline carrying the share


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
    share_verified = round(1.0 - float(unverified_share), 4)  # the lift NUMERATOR
    lift: float | None = None
    if frac > 0:
        lift = (1.0 - float(unverified_share)) / frac
        out["eval_top5_verified_lift"] = round(lift, 2)
    if unverified_share > SHARE_CEILING:
        reasons.append(f"top5 unverified share {unverified_share} > {SHARE_CEILING} ceiling")
    if lift is not None and lift < LIFT_FLOOR:
        reasons.append(f"verified lift {round(lift, 2)}x < {LIFT_FLOOR:g}x floor")
    # The WoW trip watches the NUMERATOR share, never the composite (review #6:
    # a lift WoW fires on pure denominator decay — the trap in the module doc).
    prior = _week_ago_share()
    if prior is not None:
        prior_share, prior_queries = prior
        if prior_queries is not None and prior_queries != golden_count:
            # Golden-set edit: the two shares measured different query sets, so
            # tonight establishes a fresh baseline instead of a false move.
            logger.info(
                "trust WoW skipped: golden set changed (%s -> %s queries), baseline reset",
                prior_queries,
                golden_count,
            )
        elif prior_share > 0 and abs(share_verified - prior_share) / prior_share > WOW_MOVE:
            reasons.append(
                f"top5 verified share moved "
                f"{abs(share_verified - prior_share) / prior_share:.0%} WoW "
                f"({prior_share} -> {share_verified})"
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


def _week_ago_share() -> tuple[float, int | None] | None:
    """``(top-5 VERIFIED share, eval_queries)`` from the newest nightly report
    at least :data:`WOW_MIN_AGE_DAYS` old carrying the share — the WoW baseline.

    Reads ``eval_top5_unverified_share`` (persisted since Wave E — every report
    that ever carried the lift carries it, so no baseline gap) and returns its
    complement, the lift NUMERATOR (review #6: the composite-lift WoW fired on
    pure denominator decay). Walks back over reports missing the key
    (instrument errored, foreign ``type='report'`` items) exactly like
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
        share = meta.get("eval_top5_unverified_share")
        if isinstance(share, int | float) and not isinstance(share, bool):
            queries = meta.get("eval_queries")
            ok_q = isinstance(queries, int) and not isinstance(queries, bool)
            return round(1.0 - float(share), 4), (int(queries) if ok_q else None)
    return None

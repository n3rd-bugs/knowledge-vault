"""kbworker.eval_regression — the sentinel's regression detector (§8).

Split from :mod:`kbworker.eval_sentinel` (mechanical move, review #5 Wave F):
the verified-lift watch and chain-resolution telemetry pushed that file to the
300-line ratchet, and the detector is its own concern — pure comparison logic
over tonight's metrics, the prior run's baseline and the anchored floors.
``eval_sentinel`` re-imports every name (the established seam every caller and
test already uses, same pattern as kbworker.tail_steps / kbworker.digest), and
the logger keeps the ``kbworker.eval_sentinel`` name these WARNING lines have
always carried in journald.

DETECTOR HYGIENE (review #9a EV-2, KB 31857 follow-up): the baseline and the
recovery anchor read only FALLBACK-FREE prior evals (``kbcore.eval_baseline``),
and a trip measured on a night whose own replay fell back is a MEASUREMENT,
not a corpus regression — labelled as such in the WARNING, recorded under the
ADDED key ``eval_regression_measurement`` and never enqueued as a
``search_regression`` row; nor does such a night auto-resolve an open row
(a contaminated HIGH is not evidence of recovery either). ``eval_regression``
keeps its meaning: True only for a fallback-free trip.

EVAL EPOCH (E1, KB 31857 / KB 32942): the baseline read is confined to reports
measured on tonight's golden set (``counts['eval_golden_hash']``, see
``kbcore.eval_epoch``). The first night of a new epoch therefore has NO prior
— recorded under the ADDED key ``eval_epoch_reset`` when older-epoch reports
were walked over and NO same-epoch report exists yet, so the digest can say
so — and the anchored floors alone gate it; the second night compares against
the first, as ever. A same-epoch night that fell back is walked over as
``skipped_fallback`` (never as an epoch skip: ``read_baseline`` applies the
epoch filter first), so the night after it has no clean prior either — floors
only — but it is NOT the epoch's first night and must not re-print the reset.

E2 (review #9 KB 40402, landed at review #10): FIRST on every night, every open
row whose detail has no / another ``golden_hash`` is resolved 'epoch changed',
oldest first up to tonight's own open row (kbworker.eval_detector_io; a status
flip, Invariant 5 — row 35091's 0.984 anchor was an echo night's; a WARNING
names both hashes), so neither the anchor nor the dedup inherits it;
new rows carry ``golden_hash``, ``cause`` and any ``persistent_cases``. The
floors land in meta every run (they live only in the unbacked .env; floors AT
the code defaults pass every red night, so that is a WARNING +
``eval_floors_at_defaults``). Every trip names its ``cause``, and the WARNING
prints the anchor the resolve test actually reads — the STORED one when a row
is open. The PERSISTENCE trip (kbworker.eval_persistence) trips a night by
itself and so blocks the auto-resolve until the case passes; when tonight's
persistence is UNKNOWN (its read or fold raised) nothing trips on it, the
floors and drops still gate, and no row auto-resolves that night
(``eval_persistence_status='error'``) — a night that cannot see the case cannot
prove it recovered. A drop is compared at the metrics' stored 4-decimal
precision, so a drop of exactly epsilon (or a night exactly at a floor) is no trip.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from kbcore import eval_baseline
from kbcore import review as review_mod
from kbcore.config import Settings, settings

# THE ESTABLISHED SEAM (mechanical split, review #10 fix round): the detector's
# STATE-TOUCHING inputs — the epoch-changed close over the review rows and the
# persistence history read — moved to kbworker.eval_detector_io at this file's
# ratchet; every name keeps resolving here (``eval_regression.REVIEW_REASON``).
from kbworker.eval_detector_io import (  # noqa: F401
    EPOCH_CHANGED,
    EPOCH_CLOSE_MAX,
    REVIEW_REASON,
    _close_other_epoch_rows,
    _persistence,
)

logger = logging.getLogger("kbworker.eval_sentinel")

REGRESSION_EPSILON = 0.05  # absolute hit@5 / MRR drop vs the prior run that trips it
# The Settings model's CODE defaults: floors equal to these mean the .env lost them.
DEFAULT_FLOORS = (
    Settings.model_fields["kb_eval_min_hit5"].default,
    Settings.model_fields["kb_eval_min_mrr"].default,
)


def _tonights_fallbacks(counts: Mapping[str, Any]) -> int:
    """How many of tonight's primary-replay cases fell back to the fused order.

    ``eval_sentinel._run`` snapshots ``counts['eval_rerank_tally']`` BEFORE it
    calls the detector; a run without the tally (rerank off) reads as 0.
    """
    return eval_baseline.rerank_fallbacks(counts)


def _record_floors(counts: dict[str, Any]) -> tuple[float, float]:
    """Tonight's anchored floors, recorded in meta EVERY run (module docstring)."""
    floor_h, floor_m = settings.kb_eval_min_hit5, settings.kb_eval_min_mrr
    counts["eval_floor_hit_at_5"] = floor_h
    counts["eval_floor_mrr"] = floor_m
    if (floor_h, floor_m) == DEFAULT_FLOORS:
        counts["eval_floors_at_defaults"] = True
        logger.warning(
            "eval floors %g/%g are the CODE defaults: KB_EVAL_MIN_HIT5 / KB_EVAL_MIN_MRR are "
            "missing from .env — the anchored floors are not being applied",
            floor_h,
            floor_m,
        )
    return floor_h, floor_m


def _dropped(prior: float, tonight: float) -> bool:
    """A drop beyond epsilon, compared at the stored 4-decimal precision — so a
    decimal drop of exactly epsilon is no trip, whatever the float subtraction says."""
    return round(prior - tonight, 4) > REGRESSION_EPSILON


def _trip_cause(
    hit_at_5: float,
    mrr: float,
    floors: tuple[float, float],
    prior: Mapping[str, Any] | None,
    persistent: list[dict[str, Any]],
) -> list[str]:
    """The trips that fired tonight, sorted (``eval_regression_cause``); [] = none."""
    fired = {
        "floor_hit_at_5": hit_at_5 < floors[0],
        "floor_mrr": mrr < floors[1],
        "drop_hit_at_5": prior is not None and _dropped(prior["hit_at_5"], hit_at_5),
        "drop_mrr": prior is not None and _dropped(prior["mrr"], mrr),
        "persistent": bool(persistent),
    }
    return sorted(name for name, hit in fired.items() if hit)


def _detect_regression(counts: dict[str, Any], hit_at_5: float, mrr: float) -> None:
    golden_hash = counts.get("eval_golden_hash")
    open_row = _close_other_epoch_rows(counts, golden_hash)  # E2: FIRST, every night
    floor_h, floor_m = _record_floors(counts)
    prior, skips = eval_baseline.read_baseline(golden_hash)
    fallbacks = _tonights_fallbacks(counts)
    if prior is None and skips["skipped_epoch"] > 0 and skips["skipped_fallback"] == 0:
        # First night of a new eval epoch (module docstring): every prior eval was
        # measured on a different golden set, so there is no WoW baseline tonight.
        # skipped_fallback > 0 means a SAME-epoch report exists (it fell back), so
        # tonight is a later night of the epoch — no clean prior, but no reset.
        counts["eval_epoch_reset"] = {
            "golden_hash": golden_hash,
            "queries": counts.get("eval_queries"),
            "skipped_epoch": skips["skipped_epoch"],
        }
        logger.info(
            "eval epoch %s (n=%s): first night — %d prior report(s) measured on another golden "
            "set skipped, WoW baseline reset; the anchored floors %.2f/%.2f still apply",
            golden_hash,
            counts.get("eval_queries"),
            skips["skipped_epoch"],
            floor_h,
            floor_m,
        )

    # Independent trips: (1) a drop beyond epsilon vs the previous run, (2) an
    # ANCHORED ABSOLUTE floor, and (3) since E2 a PERSISTENT case. The floor catches a
    # gradual sub-epsilon drift the previous-run comparison can never see (0.90 → 0.0
    # at 0.05/night is silent otherwise), and it fixes the downward-ratcheting
    # auto-resolve baseline (finding). Persistence is never read on a fallback night.
    # ONE comparison per trip (_trip_cause); the measurement's flags read the cause.
    persistent = [] if fallbacks else _persistence(counts, golden_hash)
    cause = _trip_cause(hit_at_5, mrr, (floor_h, floor_m), prior, persistent or [])
    below_floor = "floor_hit_at_5" in cause or "floor_mrr" in cause
    dropped = "drop_hit_at_5" in cause or "drop_mrr" in cause
    if cause and fallbacks:
        # A trip on a fallback night is a MEASUREMENT (module docstring): say so,
        # record it, and leave the queue alone — the next fallback-free night
        # re-measures against the same clean baseline; the floor still backstops.
        counts["eval_regression_measurement"] = {
            "rerank_fallback": fallbacks,
            "hit_at_5": round(hit_at_5, 4),
            "mrr": round(mrr, 4),
            "below_floor": below_floor,
            "dropped": dropped,
            "cause": cause,
        }
        logger.warning(
            "SEARCH-QUALITY measurement (rerank fallback %d): hit@5 %.3f MRR %.3f tripped %s "
            "(prior %s, floor %g/%g, epsilon %.2f) on a replay measured partly on the "
            "fused order — not recorded as a corpus regression",
            fallbacks,
            hit_at_5,
            mrr,
            "+".join(cause),
            f"{prior['hit_at_5']:.3f}/{prior['mrr']:.3f}" if prior else "none",
            floor_h,
            floor_m,
            REGRESSION_EPSILON,
        )
        return
    if cause:
        counts["eval_regression"] = True
        counts["eval_regression_cause"] = cause
        # Recovery ANCHOR = the pre-drift baseline we must climb back to: the max of
        # the prior run and the floor, minted ONCE with the row and never rewritten
        # (dedup) — so a partial rebound cannot self-close a still degraded state.
        # With a row open the auto-resolve below reads ITS stored anchor, so that is
        # the anchor the WARNING prints (pre-E2 it printed a recomputed max no
        # resolve ever read).
        if open_row is not None:
            anchor_h = float(open_row[1].get("baseline_hit_at_5", 0.0))
            anchor_m = float(open_row[1].get("baseline_mrr", 0.0))
        else:
            anchor_h = max(prior["hit_at_5"] if prior else 0.0, floor_h)
            anchor_m = max(prior["mrr"] if prior else 0.0, floor_m)
        logger.warning(
            "SEARCH-QUALITY REGRESSION: %s — hit@5 %.3f MRR %.3f (prior %s, floor %g/%g, "
            "recovery anchor %.3f/%.3f %s, epsilon %.2f)",
            "+".join(cause),
            hit_at_5,
            mrr,
            f"{prior['hit_at_5']:.3f}/{prior['mrr']:.3f}" if prior else "none",
            floor_h,
            floor_m,
            anchor_h,
            anchor_m,
            f"stored on open row {open_row[0]}" if open_row is not None else "for a new row",
            REGRESSION_EPSILON,
        )
        if open_row is None:  # dedup: exactly one open flag at a time
            row: dict[str, Any] = {
                "baseline_hit_at_5": round(anchor_h, 4),
                "baseline_mrr": round(anchor_m, 4),
                "observed_hit_at_5": round(hit_at_5, 4),
                "observed_mrr": round(mrr, 4),
                "floor_hit_at_5": floor_h,
                "floor_mrr": floor_m,
                "epsilon": REGRESSION_EPSILON,
                "golden_hash": golden_hash,
                "cause": cause,
            }
            if persistent:
                row["persistent_cases"] = persistent
            review_mod.queue_review(None, REVIEW_REASON, row)
        return
    # No regression: self-heal a stale flag only on FULL recovery to the SAME
    # anchor (>= baseline, not baseline − epsilon) AND above the floor, so a partial
    # rebound never auto-retracts a still-degraded state and drags the baseline down.
    # Never on a fallback night: a stalled replay's number is not evidence either way.
    # Nor when tonight's persistence is UNKNOWN: a night that cannot see the cases cannot
    # prove the persistent one recovered (fix F2 — a read error used to self-heal the row).
    if open_row is not None and (fallbacks or persistent is None):
        logger.info(
            "search-quality: open regression row %d left as is — %s, so its numbers cannot "
            "prove recovery",
            open_row[0],
            f"tonight's replay fell back on {fallbacks} case(s)"
            if fallbacks
            else "tonight's persistence is unknown",
        )
        return
    if open_row is not None:
        queue_id, detail = open_row
        base_h = float(detail.get("baseline_hit_at_5", 0.0))
        base_m = float(detail.get("baseline_mrr", 0.0))
        if hit_at_5 >= base_h and mrr >= base_m and hit_at_5 >= floor_h and mrr >= floor_m:
            review_mod.resolve_review(queue_id)
            logger.info(
                "search-quality recovered to baseline; auto-resolved review row %d", queue_id
            )

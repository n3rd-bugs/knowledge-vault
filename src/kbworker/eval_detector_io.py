"""kbworker.eval_detector_io — the regression detector's STATE-TOUCHING inputs (E2).

THE ESTABLISHED SEAM (mechanical split, review #10 fix round): moved out of
:mod:`kbworker.eval_regression` when the fix round's bounded close loop and
tri-state persistence pushed that file past its 300-line ratchet. What stays
there is the pure comparison over tonight's metrics, the prior and the floors;
what lives here are the two inputs that READ or FLIP state before it runs.
``eval_regression`` re-imports every name, and the logger keeps the
``kbworker.eval_sentinel`` name these lines have always carried in journald.

* :func:`_close_other_epoch_rows` — FIRST on every night (fallback nights too):
  every open ``search_regression`` row whose detail has no / another
  ``golden_hash`` is resolved 'epoch changed', oldest first, until tonight's
  own (same-epoch) open row is reached — a status flip + detail stamp
  (Invariant 5), with a WARNING naming the row's epoch and tonight's; that
  same-epoch row, if any, is the night's open row.
* :func:`_persistence` — the per-case PERSISTENCE trip's read
  (kbworker.eval_persistence over ``kbcore.eval_baseline.case_history``),
  TRI-STATE: the persistent list, ``[]`` when persistence does not apply, or
  None when it is UNKNOWN tonight (the detector then trips nothing on it and
  auto-resolves nothing).
"""

from __future__ import annotations

import logging
from typing import Any

from kbcore import eval_baseline
from kbcore import review as review_mod
from kbworker import eval_persistence

logger = logging.getLogger("kbworker.eval_sentinel")

REVIEW_REASON = "search_regression"
EPOCH_CHANGED = "epoch changed"  # resolve_review note on a row from another epoch
EPOCH_CLOSE_MAX = 20  # other-epoch rows one night closes at most (the loop is bounded)


def _close_other_epoch_rows(
    counts: dict[str, Any], golden_hash: str | None
) -> tuple[int, dict[str, Any]] | None:
    """The night's open regression row — after closing EVERY open row of ANOTHER epoch.

    ``open_review_by_reason`` reads the OLDEST open row, so this closes and
    re-reads until a same-epoch row (tonight's open row) or none is left — at
    most :data:`EPOCH_CLOSE_MAX` closes; past that the rest wait for the next
    night and tonight has no open row (an other-epoch anchor is never used).
    An other-epoch row NEWER than an open same-epoch row is not reached until
    that row resolves (the read stops at it); it is never the night's row.
    ``eval_regression_epoch_closed`` is the FIRST qid closed and
    ``eval_regression_epoch_closed_count`` the total when it is more than one.
    A hash-less run (an untracked test rig) is not an epoch and closes nothing.
    """
    open_row = review_mod.open_review_by_reason(REVIEW_REASON)
    closed: list[int] = []
    while golden_hash is not None and open_row is not None:
        qid, detail = open_row
        if detail.get("golden_hash") == golden_hash:
            break
        if len(closed) >= EPOCH_CLOSE_MAX:
            logger.warning(
                "search-quality: >%d other-epoch rows open; row %d waits", len(closed), qid
            )
            open_row = None
            break
        review_mod.resolve_review(qid, note=EPOCH_CHANGED)
        closed.append(qid)
        logger.warning(
            "search-quality: regression row %d (epoch %s) resolved 'epoch changed' — tonight's "
            "epoch is %s",
            qid,
            detail.get("golden_hash") or "untracked",
            golden_hash,
        )
        open_row = review_mod.open_review_by_reason(REVIEW_REASON)
    if closed:
        counts["eval_regression_epoch_closed"] = closed[0]
        if len(closed) > 1:
            counts["eval_regression_epoch_closed_count"] = len(closed)
    return open_row


def _persistence(counts: dict[str, Any], golden_hash: str | None) -> list[dict[str, Any]] | None:
    """Tonight's persistent cases (kbworker.eval_persistence); None = UNKNOWN.

    Called only on a fallback-free night. ``[]`` when persistence does not
    apply (an untracked run, a run without verdicts). UNKNOWN when the history
    read or the fold raises — or tonight's verdicts were lost to a replay
    telemetry error — and then no persistence trip and NO auto-resolve
    tonight (module docstring), while the floors and the drop check beside it
    still gate: one WARNING, ``eval_persistence_status='error'``, and no
    ``eval_case_persistent`` / ``eval_case_first_pass`` keys.
    """
    passed = counts.get("eval_case_passed")
    if golden_hash is None or (
        not passed and counts.get("eval_replay_telemetry_status") != "error"
    ):
        return []
    try:
        if not passed:
            raise RuntimeError("tonight's per-case verdicts were lost (replay telemetry error)")
        trips = eval_persistence.case_trips(counts, eval_baseline.case_history(golden_hash))
    except Exception:  # noqa: BLE001 — a new instrument may never cost the floors
        counts["eval_persistence_status"] = "error"
        logger.warning(
            "eval persistence UNKNOWN tonight: no persistence trip and no auto-resolve; the "
            "floors and drops still gate",
            exc_info=True,
        )
        return None
    counts.update(trips)
    if trips.get("eval_case_persistent") or trips.get("eval_case_first_pass"):
        logger.info("eval persistence (golden index + satisfier id): %s", trips)
    return list(trips.get("eval_case_persistent") or [])

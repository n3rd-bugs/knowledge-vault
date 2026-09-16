"""kbcore.eval_baseline — the sentinel's CLEAN eval baseline read (review #9a EV-2).

``kbcore.consolidation.previous_eval_metrics`` returns the newest kb-system
report that completed an eval (``meta.eval_status == 'ok'``) and never looks at
``eval_rerank_tally``. On Sep 4 the rerank sidecar stalled on its first touch
after the drain and 15 of 52 golden cases were measured on the FUSED order:
the night read 0.9423 (+1.6 quanta), became the next night's baseline, and the
next night's 0.9103 (-1.7 quanta, 22 fallbacks) was compared against it. A
contaminated HIGH as the baseline is a poisoned anchor: a clean night can trip
the epsilon against it, and the recovery anchor (max of prior, floor and the
open row's baseline) then ratchets UP to a number no clean replay ever reached.

This module walks the same bounded window and SKIPS any report whose tally
carries ``fallback > 0`` — logging each skip — so the baseline and the anchor
read only fallback-free nights. Reports WITHOUT the tally key (pre-H7a) stay
eligible: an older report is not evidence of contamination. ``consolidation``
keeps its function unchanged (that file sits at its size-ratchet ceiling and
other callers may want the unfiltered read); the regression detector
(``kbworker.eval_regression``) reads THIS one. Read-only, one query.

EVAL EPOCH (E1, KB 31857 / KB 32942): a report is only comparable to tonight
when it was measured on the SAME golden set. When the caller passes tonight's
``golden_hash`` (``kbcore.eval_epoch``), a prior report is eligible only if
its ``meta.eval_golden_hash`` equals it; reports WITHOUT the key predate
epoch tracking and are ineligible too — the E1 reset is deliberate ("reset
the WoW baseline in the same commit"), so the first night of an epoch has
no prior and the anchored floors alone gate it. Without a hash the read is
byte-identical to before (the unfiltered callers and their tests).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from kbcore.consolidation import EVAL_BASELINE_LOOKBACK
from kbcore.db import connect

logger = logging.getLogger("kbcore.eval_baseline")


def rerank_fallbacks(meta: Any) -> int:
    """``meta.eval_rerank_tally.fallback`` as an int — 0 when absent or malformed.

    The ONE definition of 'this eval fell back' the baseline, the anchor and
    the detector share; a report with no tally reads as 0 (eligible).
    """
    if not isinstance(meta, Mapping):
        return 0
    tally = meta.get("eval_rerank_tally")
    fb = tally.get("fallback") if isinstance(tally, Mapping) else None
    return int(fb) if isinstance(fb, int) and not isinstance(fb, bool) and fb > 0 else 0


def previous_eval_metrics(golden_hash: str | None = None) -> dict[str, Any] | None:
    """hit@5/MRR of the newest COMPLETED, FALLBACK-FREE eval in the lookback window.

    Same walk as ``consolidation.previous_eval_metrics`` (non-ok and foreign
    ``type='report'`` items are stepped over, up to
    :data:`~kbcore.consolidation.EVAL_BASELINE_LOOKBACK` reports back), plus
    the fallback skip (module docstring). Returns ``{'hit_at_5', 'mrr',
    'report_id', 'skipped_fallback'}`` — the two metrics the detector compares
    against, the report they came from, and how many fallback nights were
    walked over to reach it — or None when no eligible report exists (a
    genuine first run, or every recent eval fell back), so the first clean eval
    simply establishes the baseline.

    With ``golden_hash`` the walk is confined to tonight's eval EPOCH (module
    docstring) and the dict gains ``'skipped_epoch'`` — the count of
    other-epoch or pre-epoch reports walked over. The key is ADDED only under
    epoch tracking so the untracked read stays exactly what it was.
    """
    prior, skips = read_baseline(golden_hash)
    if prior is not None and golden_hash is not None:
        prior["skipped_epoch"] = skips["skipped_epoch"]
    return prior


def read_baseline(
    golden_hash: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """:func:`previous_eval_metrics`'s walk with the skip tallies beside it.

    ``(prior, {'skipped_fallback', 'skipped_epoch'})`` — the tallies are
    returned separately because the detector needs them precisely when
    ``prior`` is None: "no prior AND epoch skips > 0 AND no fallback skips" is
    the first night of a new epoch (``kbworker.eval_regression`` records
    ``eval_epoch_reset``), whereas "no prior and no skips" is a genuine first
    run. The tallies are DISJOINT and the epoch filter runs FIRST, so under a
    hash ``skipped_fallback`` counts SAME-epoch reports only — that is what
    lets the detector tell the epoch's first night from a later night whose
    same-epoch predecessors all fell back. One query.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, meta FROM items "
            "WHERE project = 'kb-system' AND type = 'report' "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (EVAL_BASELINE_LOOKBACK,),
        ).fetchall()
    skips = {"skipped_fallback": 0, "skipped_epoch": 0}
    for item_id, created_at, meta in rows:
        if not isinstance(meta, dict) or meta.get("eval_status") != "ok":
            continue
        if golden_hash is not None and meta.get("eval_golden_hash") != golden_hash:
            skips["skipped_epoch"] += 1
            logger.info(
                "eval baseline: skipping report %s (%s) — golden epoch %s, tonight is %s",
                item_id,
                created_at,
                meta.get("eval_golden_hash") or "untracked",
                golden_hash,
            )
            continue
        fallbacks = rerank_fallbacks(meta)
        if fallbacks:  # reached only past the epoch filter: a same-epoch report
            skips["skipped_fallback"] += 1
            logger.info(
                "eval baseline: skipping report %s (%s) — rerank fallback %d, measured on the "
                "fused order",
                item_id,
                created_at,
                fallbacks,
            )
            continue
        try:
            prior = {
                "hit_at_5": float(meta["eval_hit_at_5"]),
                "mrr": float(meta["eval_mrr"]),
                "report_id": int(item_id),
                "skipped_fallback": skips["skipped_fallback"],
            }
        except (KeyError, TypeError, ValueError):
            continue
        return prior, skips
    return None, skips

"""kbworker.evidence_step — the nightly's step 3b: evidence refresh + auto-resolution.

MECHANICAL SPLIT (2026-09-13) out of :mod:`kbworker.consolidate`, which sat one
line under its size ratchet. The cap, the streak-reserve divisor and the stop
log stay in ``consolidate`` — tests and knobs patch them THERE — and are read
through the lazy seam at call time (the ``drainloop._seam`` pattern), so a
patched ``consolidate.EVIDENCE_PAIR_CAP`` keeps its exact pre-split meaning.
``consolidate`` re-imports both public names, so call sites are unchanged.
"""

from __future__ import annotations

import logging
from types import ModuleType
from typing import Any

from kbcore import evidence, review_recency
from kbcore.config import settings
from kbcore.errors import AlreadySuperseded, ExtractorUnavailable
from kbcore.review import auto_resolve_contradiction, close_contradiction_rows_for
from kbworker.tail_ops import STOP_CHECK_EVERY, StopCheck

logger = logging.getLogger("kbworker.consolidate")

Row = tuple[int, int, int, dict[str, Any]]

# RECENCY GUARD tallies of the LAST _evidence() pass (kbcore.review_recency), kept
# beside the 3-tuple every caller unpacks — the _CONTRA_STATS pattern: reset per
# pass, read by the nightly into its counts. ``auto_resolve_held`` = rows held
# THIS pass; ``auto_resolve_held_open`` = every open row the guard holds now.
_EVIDENCE_STATS: dict[str, int] = {"auto_resolve_held": 0, "auto_resolve_held_open": 0}
# PRECISION GATE tallies (kbcore.review_judge_gate), merged into the counts the same
# way: rows the gate held THIS pass, every open row it holds now, and resolutions
# left for the next run because the contradiction judge could not be reached.
_GATE_STATS: dict[str, int] = {
    "auto_resolve_judge_held": 0,
    "auto_resolve_judge_held_open": 0,
    "auto_resolve_gate_unavailable": 0,
}


def _seam() -> ModuleType:
    """``kbworker.consolidate`` — the patched test surface (lazy: import cycle)."""
    from kbworker import consolidate

    return consolidate


def _evidence_batch(rows: list[Row]) -> list[Row]:
    """Cap the nightly evidence batch, RESERVING a slice for near-decided rows.

    A row whose last refresh produced a suggestion (``suggestion_streak >= 1``)
    is ONE more visit away from auto-resolution, but it sorts LAST — the query
    puts never-enriched rows first — so a cap smaller than the open set starved
    it forever (12 suggestions stuck at streak=1 against 57 open rows and a cap
    of 20). Up to ``_evidence_pair_cap() // EVIDENCE_STREAK_RESERVE_DIVISOR`` of
    those go to the FRONT of the batch, so the stop guard cannot truncate them
    away either; the rest of the cap keeps the query's never-enriched-first
    order, which is what stops brand-new contradictions being starved in turn.
    Cap and divisor are read at call time so a test (or a future knob) can lower
    the cap and have the reserve follow it.
    """
    seam = _seam()
    cap = seam._evidence_pair_cap()
    reserve = cap // seam.EVIDENCE_STREAK_RESERVE_DIVISOR
    hot = [r for r in rows if r[3].get("suggested_resolution") and r[3].get("suggestion_streak")]
    batch = hot[:reserve]
    picked = {r[0] for r in batch}
    for row in rows:
        if len(batch) >= cap:
            break
        if row[0] not in picked:
            batch.append(row)
    return batch


def _evidence(stop: StopCheck | None = None) -> tuple[int, int, int]:
    """Step 3b: refresh evidence for open contradictions; auto-resolve stable ones.

    Counts accumulate night over night as repo seeds and session flushes add
    corroborating items; never-enriched rows come first (evidence.py ordering)
    so the cap can't starve brand-new contradictions. A decisive suggestion
    that holds for AUTO_RESOLVE_STREAK consecutive refreshes is executed
    (owner V4 refinement; kill switch kb_auto_resolve_evidence). Per-pair
    isolation. ``stop`` (asked every :data:`STOP_CHECK_EVERY` pairs) bounds the
    step by wall clock/power as well as by the pair cap; an unreached row
    is just a row whose evidence is one night older.

    MOOT rows (journald 2026-09-13 05:54:20, first real week of KB 34161): two
    open rows can carry the same stable loser, and the second resolution of
    the pass finds it already superseded. kbcore stays honest — it raises
    :class:`AlreadySuperseded` — and the worker treats exactly that subclass as
    "already done": one WARNING, the row closed through the same kbcore
    chokepoint the doc-refresh sweep uses (the pair IS settled), counted in
    ``auto_resolve_moot``. Every other InvalidOperation (a wrong keep/retire)
    still reaches the per-pair ERROR. Returns ``(refreshed, auto_resolved, moot)``.

    RECENCY GUARD (2026-10-24): an open row whose hold is in force — kbcore
    refused to retire the NEWER item, or the owner restored one — is skipped
    BEFORE batching: no judge calls, no streak churn, no cap slot, no reserve
    slot (a held row carries a streak and would otherwise squat the reserve
    forever). A stable suggestion kbcore holds tonight counts in
    ``_EVIDENCE_STATS`` instead of ``auto_resolved``.

    PRECISION GATE (2026-10-25): with ``kb_auto_resolve_judge_gate`` on, kbcore
    re-judges each stable suggestion with the contradiction seat before retiring;
    a gate hold counts in ``_GATE_STATS`` (and is skipped the same way afterwards),
    an unreachable judge leaves the pair for the next run.
    """
    rows = evidence.open_contradiction_rows()
    refreshed = 0
    auto_resolved = 0
    moot = 0
    held = 0
    gate_down = False  # set by the first unreachable gate judge: skip the rest of the pass
    live = [r for r in rows if not review_recency.hold_in_force(r[3])]
    held_before = sum(1 for r in rows if review_recency.held_newer(r[3]))
    _EVIDENCE_STATS.update(auto_resolve_held=0, auto_resolve_held_open=held_before)
    judge_before = sum(1 for r in rows if review_recency.held_judge(r[3]))
    _GATE_STATS.update(
        auto_resolve_judge_held=0,
        auto_resolve_judge_held_open=judge_before,
        auto_resolve_gate_unavailable=0,
    )
    batch = _evidence_batch(live)
    for n, (queue_id, a_id, b_id, prior) in enumerate(batch):
        if stop is not None and n % STOP_CHECK_EVERY == 0 and (reason := stop()):
            _seam()._note_stop("evidence_refreshed", reason, len(batch) - n)
            break
        try:
            detail = evidence.gather_evidence(a_id, b_id)
            sugg = detail.get("suggested_resolution")
            if sugg:
                prior_sugg = prior.get("suggested_resolution") or {}
                same = prior_sugg.get("keep") == sugg["keep"] and prior_sugg.get(
                    "retire"
                ) == sugg.get("retire")
                detail["suggestion_streak"] = prior.get("suggestion_streak", 0) + 1 if same else 1
            else:
                detail["suggestion_streak"] = 0  # overwrite any stale streak
            if evidence.update_review_detail(queue_id, detail):
                refreshed += 1
            if (
                sugg
                and settings.kb_auto_resolve_evidence
                and detail["suggestion_streak"] >= evidence.AUTO_RESOLVE_STREAK
            ):
                if gate_down:  # one unreachable judge per pass, never a timeout per pair
                    _GATE_STATS["auto_resolve_gate_unavailable"] += 1
                    continue
                try:
                    retired = auto_resolve_contradiction(queue_id, sugg["keep"], sugg["retire"])
                except AlreadySuperseded as exc:
                    moot += 1
                    closed = close_contradiction_rows_for(sugg["keep"], sugg["retire"])
                    logger.warning(
                        "contradiction row %s moot: %s "
                        "(an earlier resolution already superseded it; %d row(s) closed)",
                        queue_id,
                        exc,
                        closed,
                    )
                    continue
                except ExtractorUnavailable as exc:  # the precision gate's judge is gone
                    gate_down = True
                    _GATE_STATS["auto_resolve_gate_unavailable"] += 1
                    logger.warning(
                        "contradiction row %s: precision-gate judge unreachable (%s); "
                        "nothing retired, retried next run",
                        queue_id,
                        exc,
                    )
                    continue
                if retired is None and (
                    review_recency.hold_kind(queue_id) == review_recency.HOLD_JUDGE
                ):  # the gate's judge found no conflict: held for the owner
                    _GATE_STATS["auto_resolve_judge_held"] += 1
                    _GATE_STATS["auto_resolve_judge_held_open"] += 1
                    continue
                if retired is None:  # held: kbcore stamped why; the row stays pending
                    held += 1
                    _EVIDENCE_STATS.update(
                        auto_resolve_held=held, auto_resolve_held_open=held_before + held
                    )
                    continue
                auto_resolved += 1
                logger.info(
                    "auto-resolved contradiction row %s: keep %s, retire %s (%s, streak %d)",
                    queue_id,
                    sugg["keep"],
                    sugg["retire"],
                    sugg["rule"],
                    detail["suggestion_streak"],
                )
        except Exception:  # noqa: BLE001 — one bad pair must not abort the step
            logger.exception("evidence refresh for queue row %s failed", queue_id)
    if len(live) > len(batch):
        logger.info(
            "evidence refresh capped at %d pairs; %d open pairs deferred to the next run",
            len(batch),
            len(live) - len(batch),
        )
    return refreshed, auto_resolved, moot

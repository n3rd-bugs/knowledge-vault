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
from kbcore.review import close_contradiction_rows_for
from kbworker.tail_ops import STOP_CHECK_EVERY, StopCheck

logger = logging.getLogger("kbworker.consolidate")

Row = tuple[int, int, int, dict[str, Any]]

# RECENCY GUARD tallies of the LAST _evidence() pass (kbcore.review_recency), kept
# beside the 3-tuple every caller unpacks — the _CONTRA_STATS pattern: reset per
# pass, read by the nightly into its counts. ``auto_resolve_held`` = rows held
# THIS pass; ``auto_resolve_held_open`` = every open row the guard holds now.
_EVIDENCE_STATS: dict[str, int] = {"auto_resolve_held": 0, "auto_resolve_held_open": 0}
# PRECISION GATE tallies (kbcore.review_judge_gate), merged into the counts the same
# way: rows the gate held THIS pass ("no conflict" / no verdict on the pair), every
# open row it holds now, and rows deferred because the judge could not be reached.
_GATE_STATS: dict[str, int] = {
    "auto_resolve_judge_held": 0,
    "auto_resolve_judge_failed": 0,
    "auto_resolve_judge_held_open": 0,
    "auto_resolve_judge_failed_open": 0,
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
    re-judges each stable suggestion (strict three-state parse) before retiring; a
    gate hold counts in ``_GATE_STATS`` (and is skipped the same way afterwards).
    Resolutions run AFTER every refresh (two phases) so the judge — another model
    on a one-slot card — loads once per pass; once it is unreachable the rest of
    the pass defers what needs it while the recency guard and moot path still act.
    """
    rows = evidence.open_contradiction_rows()
    refreshed = 0
    auto_resolved = 0
    moot = 0
    held = 0
    gate_down = False  # the first unreachable gate judge: defer what needs it, all pass
    live = [r for r in rows if not review_recency.hold_in_force(r[3])]
    held_before = sum(1 for r in rows if review_recency.held_newer(r[3]))
    _EVIDENCE_STATS.update(auto_resolve_held=0, auto_resolve_held_open=held_before)
    _GATE_STATS.update(
        auto_resolve_judge_held=0,
        auto_resolve_judge_failed=0,
        auto_resolve_judge_held_open=sum(
            1 for r in rows if review_recency.held_judge(r[3], review_recency.HOLD_JUDGE)
        ),
        auto_resolve_judge_failed_open=sum(
            1 for r in rows if review_recency.held_judge(r[3], review_recency.HOLD_FAILED)
        ),
        auto_resolve_gate_unavailable=0,
    )
    batch = _evidence_batch(live)
    # Phase 1 refreshes every row (evidence_side calls on the default model); phase 2
    # resolves the rows whose suggestion is stable. Split so the precision gate's
    # judge (another model on a ONE-slot card) loads once per pass, not twice per row.
    candidates: list[tuple[int, dict[str, Any], int]] = []
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
            still_open = evidence.update_review_detail(queue_id, detail)
            if still_open:
                refreshed += 1
            if (
                still_open  # a row closed meanwhile is never resolved from its snapshot
                and sugg
                and settings.kb_auto_resolve_evidence
                and detail["suggestion_streak"] >= evidence.AUTO_RESOLVE_STREAK
            ):
                candidates.append((queue_id, sugg, detail["suggestion_streak"]))
        except Exception:  # noqa: BLE001 — one bad pair must not abort the step
            logger.exception("evidence refresh for queue row %s failed", queue_id)
    gated = bool(settings.kb_auto_resolve_judge_gate)
    no_verdict_run = 0  # consecutive per-pair judge failures: 2 trip the pass breaker
    held_tonight: list[tuple[int, int, int]] = []  # (queue_id, keep, retire) held this pass
    for n, (queue_id, sugg, streak) in enumerate(candidates):
        # Only a BATTERY stand-down stops phase 2: on a deadline the collected rows still
        # resolve (the reserve's promise; at most ~13 s of judge per candidate).
        if gated and stop is not None and (reason := stop()) == "battery":
            _seam()._note_stop("evidence_refreshed", reason, len(candidates) - n)
            break
        try:
            # judge=False once the judge is gone: the recency guard and the moot check
            # still settle what needs no judge; the rest is deferred, never retired.
            out = review_recency.resolve_contradiction(
                queue_id, sugg["keep"], sugg["retire"], judge=not gate_down
            )
        except AlreadySuperseded as exc:
            moot += 1
            try:
                closed = close_contradiction_rows_for(sugg["keep"], sugg["retire"])
            except Exception:  # noqa: BLE001 — one bad pair must not abort the step
                logger.exception("closing moot contradiction row %s failed", queue_id)
                continue
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
                "nothing retired, the rest of the pass deferred",
                queue_id,
                exc,
            )
            continue
        except Exception:  # noqa: BLE001 — one bad pair must not abort the step
            logger.exception("auto-resolution of queue row %s failed", queue_id)
            continue
        if out.hold == review_recency.GATE_DEFERRED:
            _GATE_STATS["auto_resolve_gate_unavailable"] += 1
            continue
        if out.hold is not None and out.hold != review_recency.GATE_DEFERRED:
            held_tonight.append((queue_id, sugg["keep"], sugg["retire"]))
        if out.hold in (review_recency.GATE_RETRY, review_recency.HOLD_FAILED):
            _GATE_STATS["auto_resolve_judge_failed"] += 1
            if out.hold == review_recency.HOLD_FAILED:  # the third: final, sticky
                _GATE_STATS["auto_resolve_judge_failed_open"] += 1
            no_verdict_run += 1
            if no_verdict_run >= 2 and not gate_down:
                gate_down = True
                logger.warning("precision gate: two no-verdicts in a row; judge treated as down")
            continue
        no_verdict_run = 0
        if out.hold == review_recency.HOLD_JUDGE:
            _GATE_STATS["auto_resolve_judge_held"] += 1
            _GATE_STATS["auto_resolve_judge_held_open"] += 1
            continue
        if out.hold == review_recency.HOLD_NEWER:
            held += 1
            _EVIDENCE_STATS.update(
                auto_resolve_held=held, auto_resolve_held_open=held_before + held
            )
            continue
        if out.retired is None:  # nothing stamped (the owner closed it meanwhile)
            continue
        auto_resolved += 1
        logger.info(
            "auto-resolved contradiction row %s: keep %s, retire %s (%s, streak %d)",
            queue_id,
            sugg["keep"],
            sugg["retire"],
            sugg["rule"],
            streak,
        )
    # A row held earlier in phase 2 whose loser a LATER row then retired is moot: close
    # it, or it lingers open while open_contradiction_rows (both items active) hides it.
    for queue_id, keep, retire in held_tonight:
        try:
            if review_recency.superseded(retire):
                moot += 1
                closed = close_contradiction_rows_for(keep, retire)
                logger.warning(
                    "contradiction row %s moot after its hold (%d closed)", queue_id, closed
                )
        except Exception:  # noqa: BLE001 — bookkeeping must not abort the step
            logger.exception("post-pass moot check for row %s failed", queue_id)
    if len(live) > len(batch):
        logger.info(
            "evidence refresh capped at %d pairs; %d open pairs deferred to the next run",
            len(batch),
            len(live) - len(batch),
        )
    return refreshed, auto_resolved, moot

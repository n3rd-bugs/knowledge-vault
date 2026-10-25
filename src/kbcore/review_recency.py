"""kbcore.review_recency — the RECENCY GUARD on nightly evidence auto-resolution.

THE DEFECT (owner-verified on live data, 2026-10-24). The nightly evidence pass
retires the side of a contradiction pair with weaker trust-weighted
corroboration once the suggestion holds for ``AUTO_RESOLVE_STREAK`` refreshes.
Corroboration favours whatever has been in the KB LONGER — an old snapshot
accumulates neighbours, a fresh correction has none yet. In the 60 days to
2026-10-22 the step auto-resolved 92 pairs and 46 of them retired the NEWER
item; KB 50850 (2026-10-16, the accurate "hit@5 0.9508 below the 0.97 floor")
was retired in favour of KB 39395 (2026-09-20, a stale "no regression").

THE RULE. Automation never retires an item created more than
``settings.kb_auto_resolve_newer_gap_days`` AFTER the item it would keep. Such a
pair is HELD: the row stays open with its suggestion visible and a
``detail.auto_resolve_hold`` stamp saying why; only the owner decides
(``kb review apply`` / ``supersede`` / ``dismiss``). Retiring the OLDER item is
unchanged, and so is a contemporaneous disagreement inside the window.

THE DEFAULT, 0.5 days (12 h), from the 46 newer-retired pairs of that export,
by signed ``created_at`` gap (retired minus kept). They fall in two modes with
an EMPTY band between 7.4 h and 23.99 h:

* 14 pairs at <= 7.4 h — ten within 2-5 SECONDS (two extractions of one
  ingest batch), one at 55 min, three at 1.8-7.4 h (the same working
  session). These are contemporaneous: evidence may still settle them.
* 32 pairs at >= 23.99 h, up to 66 days — the change-over-time shape (a
  5% vs 25% sleep threshold six days apart; a diagnosis revised a week on;
  the eval snapshot above, 27 days apart). These must reach the owner.

The boundary has to sit inside that empty band, and NOT at 1 day: the
smallest member of the second mode is two git docs from consecutive nightly
repo scans 86398 s (23h59m58s) apart, the newer README saying the server it
documents was retired — a 1.0-day window would have let that one through.
0.5 days is the middle of the band: 32 held, 14 still auto-resolve. 0 holds
every strictly-newer retire (even the seconds-apart batch twins).

IDEMPOTENT HOLD. A held row is SKIPPED by the nightly refresh while its hold is
in force (:func:`hold_in_force`), so there is no nightly LLM spend on it, no
streak churn, no rewritten detail and never a second row. The stamp records
the gap it was taken at; raising the knob past that gap releases the row back
into the refresh (the knob is the owner's policy, read at call time). An
``owner_restore`` hold (``kbcore.review_restore``) is an unconditional veto.

PRECISION GATE (2026-10-25, :mod:`kbcore.review_judge_gate`): a retire the recency
guard lets through is re-judged by the contradiction seat first; "no conflict"
holds the row the same way (kind ``judge_no_conflict``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, NamedTuple

from psycopg.types.json import Jsonb

from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.supersede import supersede

logger = logging.getLogger("kbcore.review_recency")

AUTO_RESOLVE_REASON = "auto: evidence margin stable across nightly refreshes"
#: ``review_queue.detail`` key of the hold stamp (its own key: never clobbered by
#: the evidence merge, which writes only gather_evidence's keys).
HOLD_KEY = "auto_resolve_hold"
HOLD_NEWER = "newer_retire"  # the recency guard held it
HOLD_RESTORED = "owner_restore"  # the owner restored the retired side: veto
HOLD_JUDGE = "judge_no_conflict"  # the precision gate's judge found no conflict
HOLD_FAILED = "judge_failed"  # the gate's judge gave no verdict on THIS pair (timeout, 4xx)
GATE_DEFERRED = "gate_deferred"  # the pair needs the gate's judge and the pass has none
#: ``detail.auto_resolution.kind`` stamped on a row an auto-resolution closed, so
#: an owner restore can find exactly the row that retired the item.
RESOLVED_KIND = "evidence_supersede"


def newer_gap_days(keep: int, retire: int) -> float | None:
    """Days by which ``retire`` was created AFTER ``keep`` (negative: it is older).

    ``None`` when either item is missing — the caller's supersede then raises its
    own honest NotFound, so the guard never masks one.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT EXTRACT(EPOCH FROM (r.created_at - k.created_at)) / 86400.0 "
            "FROM items k, items r WHERE k.id = %s AND r.id = %s",
            (keep, retire),
        ).fetchone()
    return None if row is None or row[0] is None else float(row[0])


def window_days() -> float:
    """The contemporaneous window, from the knob at call time (negative clamps to 0)."""
    return max(float(settings.kb_auto_resolve_newer_gap_days), 0.0)


def would_hold(gap_days: float | None) -> bool:
    """True when retiring an item ``gap_days`` newer than the keeper must wait for
    the owner: strictly beyond the window (so 0 holds every strictly-newer retire)."""
    return gap_days is not None and gap_days > window_days()


def hold_reason(gap_days: float) -> str:
    """The ASCII reason a held row carries in ``detail.auto_resolve_hold.reason``."""
    return f"held: would retire the newer item (+{gap_days:.1f} d) - owner decision"


def hold_in_force(detail: Mapping[str, Any] | None) -> bool:
    """Whether the nightly refresh must SKIP this open row (pure; no DB).

    * ``owner_restore`` — always (the owner reversed this pair's resolution).
    * ``newer_retire`` — while the stored suggestion is still the one that was
      held AND its gap is still beyond the CURRENT window: a raised knob
      releases the row, a changed suggestion means the hold is stale.
    * ``judge_no_conflict`` / ``judge_failed`` — while the suggestion is unchanged: STICKY like the
      owner veto (a held row is never refreshed, so in practice until the owner
      acts). Releasing it on a gate-off or seat rollback would retire the pair
      on the very next pass — its streak is already past the threshold.
    """
    hold = (detail or {}).get(HOLD_KEY)
    if not isinstance(hold, Mapping):
        return False
    if hold.get("kind") == HOLD_RESTORED:
        return True
    if hold.get("kind") not in (HOLD_NEWER, HOLD_JUDGE, HOLD_FAILED):
        return False
    sugg = (detail or {}).get("suggested_resolution") or {}
    if (sugg.get("keep"), sugg.get("retire")) != (hold.get("keep"), hold.get("retire")):
        return False
    if hold.get("kind") in (HOLD_JUDGE, HOLD_FAILED):
        return True
    gap = hold.get("gap_days")
    return isinstance(gap, int | float) and not isinstance(gap, bool) and would_hold(float(gap))


def held_newer(detail: Mapping[str, Any] | None) -> bool:
    """A row held by the recency guard (not an owner veto) — the digest's count."""
    hold = (detail or {}).get(HOLD_KEY)
    return hold_in_force(detail) and isinstance(hold, Mapping) and hold.get("kind") == HOLD_NEWER


def held_judge(detail: Mapping[str, Any] | None) -> bool:
    """A row held by the precision gate (either kind) — the digest's second count."""
    hold = (detail or {}).get(HOLD_KEY)
    gate_kinds = (HOLD_JUDGE, HOLD_FAILED)
    return hold_in_force(detail) and isinstance(hold, Mapping) and hold.get("kind") in gate_kinds


def hold_row(queue_id: int, keep: int, retire: int, gap_days: float) -> bool:
    """Stamp the recency hold on an OPEN row; the row stays pending. True iff new.

    Idempotent: a row already held for the same ``(keep, retire)`` is left
    byte-identical (its first ``at`` stands), and a resolved row is never
    touched. A JSONB merge into the row's own key — never a new row.
    """
    stamp = {
        "kind": HOLD_NEWER,
        "keep": keep,
        "retire": retire,
        # UNROUNDED: hold_in_force re-tests it against the window every night, and
        # a rounded gap just past the window would release + re-hold it nightly.
        "gap_days": gap_days,
        "window_days": window_days(),
        "reason": hold_reason(gap_days),
        "at": datetime.now(UTC).isoformat(),
    }
    probe = {"kind": HOLD_NEWER, "keep": keep, "retire": retire}
    with transaction() as conn:
        row = conn.execute(
            "UPDATE review_queue SET detail = COALESCE(detail, '{}'::jsonb) || %s "
            "WHERE id = %s AND resolved = false "
            "AND NOT COALESCE(detail->'auto_resolve_hold' @> %s, false) "
            # never overwrite an owner's restore veto with a recency hold
            "AND COALESCE(detail->'auto_resolve_hold'->>'kind', '') <> %s RETURNING id",
            (Jsonb({HOLD_KEY: stamp}), queue_id, Jsonb(probe), HOLD_RESTORED),
        ).fetchone()
    return row is not None


class Outcome(NamedTuple):
    """What one auto-resolution attempt did: ``retired`` = the superseded id when
    automation acted; ``hold`` = the hold kind stamped THIS call (HOLD_NEWER,
    HOLD_JUDGE, HOLD_FAILED), GATE_DEFERRED, or None (nothing stamped — e.g. the
    owner closed the row meanwhile). Counts come from this, never a re-query."""

    retired: int | None
    hold: str | None


def resolve_contradiction(queue_id: int, keep: int, retire: int, *, judge: bool = True) -> Outcome:
    """Auto-resolve a contradiction whose evidence held for the stability streak.

    Owner V4 refinement (2026-07): supersede the evidence-losing side with the
    winner and close the row. Invoked ONLY by the nightly worker after the same
    suggestion persisted for AUTO_RESOLVE_STREAK consecutive refreshes; fully
    audited (supersede reason + relation + an ``auto_resolution`` stamp on the
    row + report count + log line) and reversible (``kb review restore``).

    RECENCY GUARD (module docstring): when ``retire`` is newer than ``keep`` by
    more than the window, NOTHING is superseded — the row is held (stamped,
    still open). PRECISION GATE (when on, :mod:`kbcore.review_judge_gate`): an
    already retired loser raises ``AlreadySuperseded`` before any judge call (the
    moot path); a row queued by another judge is re-judged by the contradiction
    seat; "no conflict" or no verdict holds the row; ``judge=False`` (the pass has
    lost its judge) defers such a row untouched; an unreachable judge raises
    ``ExtractorUnavailable`` with nothing changed.
    """
    gap = newer_gap_days(keep, retire)
    if gap is not None and would_hold(gap):
        stamped = hold_row(queue_id, keep, retire, gap)
        if stamped:
            logger.info("contradiction row %s held: %s", queue_id, hold_reason(gap))
        return Outcome(None, HOLD_NEWER if stamped else None)
    if settings.kb_auto_resolve_judge_gate:
        from kbcore import review_judge_gate  # lazy: it imports this module's hold kinds

        why = review_judge_gate.gate(queue_id, keep, retire, judge=judge)
        if why is not review_judge_gate.CONFIRMED:
            return Outcome(None, why)
    supersede(retire, new_item_id=keep, reason=AUTO_RESOLVE_REASON)
    stamp = {
        "auto_resolution": {
            "kind": RESOLVED_KIND,
            "keep": keep,
            "retire": retire,
            "at": datetime.now(UTC).isoformat(),
        }
    }
    with transaction() as conn:
        # An owner restore landing between supersede()'s commit and this one has
        # reopened + vetoed the row: never close it again behind the owner's back.
        conn.execute(
            "UPDATE review_queue SET resolved = true, "
            "detail = COALESCE(detail, '{}'::jsonb) || %s WHERE id = %s "
            "AND COALESCE(detail->'auto_resolve_hold'->>'kind', '') <> %s",
            (Jsonb(stamp), queue_id, HOLD_RESTORED),
        )
    return Outcome(retire, None)


def auto_resolve_contradiction(queue_id: int, keep: int, retire: int) -> int | None:
    """:func:`resolve_contradiction`'s retired id (None: held / nothing done) — the
    original chokepoint signature, re-exported by :mod:`kbcore.review`."""
    return resolve_contradiction(queue_id, keep, retire).retired

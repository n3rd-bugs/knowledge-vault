"""kbcore.twin_repair — owner-fired repair for CROSS-PROJECT capture twins.

Context (the 2026-07-27 double-capture era): a gateway box and this box both
captured the same Claude Code sessions, so a slice of project ``gateway`` is a
verbatim copy of project ``data-server``. The capture defect is FIXED; the data
is not. Both copies surface in every global search, and neither is reachable by
the nightly dedupe, whose join is SAME-PROJECT by settled design — that join is
NOT widened here and must not be widened elsewhere (ADVERSARIAL_REVIEW). This
module is the narrow, manual alternative: enumerate the cross-project twins,
then retire the GATEWAY copy of each confirmed pair.

* :func:`enumerate_twins` is READ-ONLY — lead-chunk (``chunks.ord = 0``) cosine
  DISTANCE (pgvector ``<=>``) between the two projects, closest first, carrying
  both titles, creation instants and redaction fingerprints so a pair can be
  eyeballed before anything is fired.
* :func:`repair` is OWNER-ONLY (``scope.require_owner``) and defaults to
  ``dry_run=True``. It invents NO transition: it calls
  :func:`kbcore.consolidation.mark_duplicate` — the SAME primitive the nightly
  dedupe uses — which flips ``status`` to ``'duplicate'`` and records a
  ``derived_from`` edge to the canonical. Nothing is deleted (Invariant 5), no
  body is touched (Invariant 1), the data-server original never moves.
* Three refusals, each a hard per-pair skip: the gateway copy is ``verified`` (a
  human already judged it), it was READ in the last :data:`ACCESS_WINDOW_DAYS`
  days (someone is actively using that id), or it is ITSELF the canonical of
  other duplicates — retiring it would strand a chain behind a non-active
  canonical, which is the lesson the auto-apply race taught.

Every retirement is its OWN transaction (``mark_duplicate``'s), so an
interrupted run leaves committed pairs done and the rest untouched, and a re-run
is a no-op over what it already retired.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg

from kbcore import scope
from kbcore.config import settings
from kbcore.consolidation import mark_duplicate
from kbcore.db import connect
from kbcore.errors import InvalidOperation, NotFound

logger = logging.getLogger("kbcore.twin_repair")

#: The double-capture pair this instrument was built for (both overridable).
GATEWAY_PROJECT = "gateway"
ORIGINAL_PROJECT = "data-server"
#: Confirmed twin band: lead-chunk cosine distance below this is a verbatim copy.
DEFAULT_MAX_DIST = 0.02
#: A gateway copy read this recently is in active use — never retired.
ACCESS_WINDOW_DAYS = 30
#: Consolidation OUTPUT is not source knowledge; excluded on both sides exactly
#: as ``consolidation._lead_pairs`` excludes it from the nightly dedupe.
EXCLUDED_TYPES = ("summary", "report")

OK = "ok"
REFUSE_INACTIVE = "not_active"
REFUSE_VERIFIED = "verified"
REFUSE_RECENT_ACCESS = f"accessed_within_{ACCESS_WINDOW_DAYS}d"
REFUSE_CHAIN = "canonical_for_other_duplicates"
REFUSE_CANONICAL_INACTIVE = "canonical_not_active"
REFUSE_ALREADY_HANDLED = "already_retired_in_this_run"


@dataclass(frozen=True)
class TwinSide:
    """One end of a candidate pair — enough context to judge it without a lookup."""

    item_id: int
    project: str
    type: str
    title: str
    created_at: datetime
    status: str
    trust: str
    redactions: dict[str, int] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Compact redaction fingerprint (``github_token:1 jwt:2``), ``-`` when clean.

        Two copies of the same capture were scrubbed by the same rules, so a
        MISMATCH here is the cheapest signal that a "twin" is not actually one.
        """
        if not self.redactions:
            return "-"
        return " ".join(f"{k}:{v}" for k, v in sorted(self.redactions.items()))


@dataclass(frozen=True)
class TwinPair:
    """A cross-project candidate: ``retire`` (gateway) vs ``keep`` (the original)."""

    distance: float
    retire: TwinSide
    keep: TwinSide


@dataclass(frozen=True)
class RepairOutcome:
    """What ``repair`` decided for one pair. ``retired`` is never True on a dry run."""

    pair: TwinPair
    eligible: bool
    retired: bool
    reason: str


_PAIR_SQL = """
SELECT (ca.embedding <=> cb.embedding) AS dist,
       ia.id, ia.project, ia.type, ia.title, ia.created_at, ia.status,
       COALESCE(sa.trust, 'verified'),
       COALESCE(ia.meta -> 'redactions', '{}'::jsonb),
       ib.id, ib.project, ib.type, ib.title, ib.created_at, ib.status,
       COALESCE(sb.trust, 'verified'),
       COALESCE(ib.meta -> 'redactions', '{}'::jsonb)
FROM items ia
JOIN chunks ca ON ca.item_id = ia.id AND ca.ord = 0
LEFT JOIN sources sa ON sa.id = ia.source_id
JOIN items ib ON ib.project = %(project_b)s AND ib.status = 'active'
             AND ib.type <> ALL(%(skip_types)s)
JOIN chunks cb ON cb.item_id = ib.id AND cb.ord = 0
LEFT JOIN sources sb ON sb.id = ib.source_id
WHERE ia.project = %(project_a)s AND ia.status = 'active'
  AND ia.type <> ALL(%(skip_types)s)
  AND ca.embedding IS NOT NULL AND cb.embedding IS NOT NULL
  AND ca.embed_model = %(model)s AND cb.embed_model = %(model)s
  AND (ca.embedding <=> cb.embedding) <= %(max_dist)s
ORDER BY dist, ia.id, ib.id
"""


def _side(row: Sequence[Any], off: int) -> TwinSide:
    return TwinSide(
        item_id=int(row[off]),
        project=row[off + 1],
        type=row[off + 2],
        title=row[off + 3],
        created_at=row[off + 4],
        status=row[off + 5],
        trust=row[off + 6],
        redactions=dict(row[off + 7] or {}),
    )


def enumerate_twins(
    project_a: str = GATEWAY_PROJECT,
    project_b: str = ORIGINAL_PROJECT,
    max_dist: float = DEFAULT_MAX_DIST,
) -> list[TwinPair]:
    """READ-ONLY candidate pairs, closest first. Writes NOTHING.

    ACTIVE items of ``project_a`` whose LEAD CHUNK is within ``max_dist`` cosine
    distance of an ACTIVE ``project_b`` lead chunk, both embedded by the CURRENT
    model (a cross-model comparison is meaningless — DATA-3). Summaries/reports
    are excluded on both sides. Many-to-many by construction: one copy may match
    several originals, and the distance ordering is what makes :func:`repair`
    keep the CLOSEST one as that copy's canonical.
    """
    scope.require_owner("twin repair")
    if project_a == project_b:
        raise InvalidOperation(
            "twin repair is CROSS-project by design; project_a and project_b must differ"
        )
    if max_dist < 0:
        raise InvalidOperation("max_dist must be >= 0")
    with connect() as conn:
        rows = conn.execute(
            _PAIR_SQL,
            {
                "project_a": project_a,
                "project_b": project_b,
                "skip_types": list(EXCLUDED_TYPES),
                "model": settings.kb_embed_model,
                "max_dist": float(max_dist),
            },
        ).fetchall()
    return [TwinPair(distance=float(r[0]), retire=_side(r, 1), keep=_side(r, 9)) for r in rows]


_GUARD_SQL = """
SELECT i.status,
       COALESCE(s.trust, 'verified'),
       EXISTS (
           SELECT 1 FROM access_log al
           WHERE al.item_id = i.id AND al.ts >= now() - make_interval(days => %(days)s)
       ),
       EXISTS (
           SELECT 1 FROM relations r JOIN items d ON d.id = r.src_id
           WHERE r.rel = 'derived_from' AND r.src_kind = 'item' AND r.dst_kind = 'item'
             AND r.dst_id = i.id AND d.status = 'duplicate'
       )
FROM items i LEFT JOIN sources s ON s.id = i.source_id
WHERE i.id = %(id)s
"""


def _refusal(conn: psycopg.Connection, pair: TwinPair) -> str | None:
    """The refusal code for this pair, or None when it is safe to retire.

    ``COALESCE(s.trust, 'verified')`` matches ``consolidation.item_ranks``: a
    sourceless item counts as VERIFIED, so the conservative branch is taken for
    anything whose provenance is unknown.
    """
    row = conn.execute(
        _GUARD_SQL, {"id": pair.retire.item_id, "days": ACCESS_WINDOW_DAYS}
    ).fetchone()
    if row is None:
        raise NotFound(f"item {pair.retire.item_id} does not exist")
    status, trust, accessed_recently, is_canonical = row
    if status != "active":
        return REFUSE_INACTIVE
    if trust == "verified":
        return REFUSE_VERIFIED
    if accessed_recently:
        return REFUSE_RECENT_ACCESS
    if is_canonical:
        return REFUSE_CHAIN
    keep = conn.execute("SELECT status FROM items WHERE id = %s", (pair.keep.item_id,)).fetchone()
    if keep is None:
        raise NotFound(f"item {pair.keep.item_id} does not exist")
    if keep[0] != "active":
        return REFUSE_CANONICAL_INACTIVE
    return None


def repair(pairs: Sequence[TwinPair], *, dry_run: bool = True) -> list[RepairOutcome]:
    """Retire the GATEWAY copy of each confirmed pair. OWNER-ONLY.

    ``dry_run=True`` (the default) evaluates every guard and reports the exact
    decisions WITHOUT opening a single write transaction. ``dry_run=False``
    performs each retirement through :func:`kbcore.consolidation.mark_duplicate`
    — one transaction per pair, ``status='duplicate'`` plus a ``derived_from``
    edge to the canonical, no body edit, no delete.

    Pairs are consumed in the order given (``enumerate_twins`` yields closest
    first), so the FIRST — nearest — original becomes a copy's canonical and any
    later pair for the same copy is reported ``already_retired_in_this_run``. A
    dry run marks the same copies handled, so its output is exactly what an
    ``--apply`` would do.

    Guard and write are not ONE transaction: ``mark_duplicate`` re-checks
    ``status = 'active'`` atomically in its own UPDATE, so the worst a racing
    writer can produce is a skipped pair (``not_active``), never a double
    retirement. The CLI additionally holds the consolidation advisory lock, so
    the nightly dedupe cannot interleave at all.
    """
    scope.require_owner("twin repair")
    outcomes: list[RepairOutcome] = []
    handled: set[int] = set()
    for pair in pairs:
        if pair.retire.project == pair.keep.project:
            # The dedupe/contradiction joins are SAME-project by settled design;
            # this instrument must never become a back door around that.
            raise InvalidOperation(
                "twin repair never retires a SAME-project pair "
                f"({pair.retire.project!r}); that join is settled design"
            )
        if pair.retire.item_id == pair.keep.item_id:
            raise InvalidOperation("an item cannot be its own duplicate")
        if pair.retire.item_id in handled:
            outcomes.append(RepairOutcome(pair, False, False, REFUSE_ALREADY_HANDLED))
            continue
        with connect() as conn:
            reason = _refusal(conn, pair)
        if reason is not None:
            outcomes.append(RepairOutcome(pair, False, False, reason))
            continue
        handled.add(pair.retire.item_id)
        if dry_run:
            outcomes.append(RepairOutcome(pair, True, False, OK))
            continue
        retired = mark_duplicate(pair.retire.item_id, pair.keep.item_id)
        if not retired:  # raced to non-active between the guard read and the write
            logger.warning(
                "twin repair: item %s went non-active before retirement; skipped",
                pair.retire.item_id,
            )
        outcomes.append(RepairOutcome(pair, True, retired, OK if retired else REFUSE_INACTIVE))
    return outcomes


def summarize(outcomes: Sequence[RepairOutcome]) -> dict[str, int]:
    """Counts by decision — ``eligible``/``retired`` plus one key per refusal code."""
    counts: dict[str, int] = {"pairs": len(outcomes), "eligible": 0, "retired": 0}
    for o in outcomes:
        counts["eligible"] += int(o.eligible)
        counts["retired"] += int(o.retired)
        if o.reason != OK:
            counts[o.reason] = counts.get(o.reason, 0) + 1
    return counts

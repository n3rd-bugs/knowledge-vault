"""kbcore.twin_crossproject — owner-fired CROSS-PROJECT twin detector (KB 13061).

The systemic finding: the SAME gotcha captured in TWO projects (golden misses
124/858 and 383/488) leaves BOTH copies active, the newer richer twin outranks the
original, and rank is diluted. The nightly dedupe never folds them — its join is
SAME-project by settled design (a scoping verdict; project walls) that is NOT
changed here and must not be changed elsewhere (ADVERSARIAL_REVIEW).

The narrow owner-invoked cross-project pass KB 13061 called for, CORRECTED for
scope-safety — DISPOSITION IS LINK, NOT RETIRE:
* :func:`enumerate_candidates` (READ-ONLY): cross-project lead-chunk cosine
  SIMILARITY (``1 - (a <=> b)``) strictly above ``kb_xtwin_threshold``, each
  unordered pair once (``ia.id < ib.id``), both leads on the CURRENT model,
  summaries/reports excluded, closest first, bounded by ``kb_xtwin_cap`` (0 = kill).
* :func:`plan` confirms same-fact with the shared 14B judge (:mod:`kbcore.twin_judge`:
  strict boolean verdict, nonce-fenced bodies, **fail-closed**). Writes NOTHING.
* :func:`apply_links` is the ONLY writer: :func:`graph.link` ``(a, 'alternative_to',
  b)`` — it flips NO status, so nothing leaves any project's surface (Invariant 5)
  and there is NO cross-project knowledge hole; the rank-time collapse (search
  package) folds the pair only when both land in one result set, and the edge is
  reversible. ``mark_duplicate`` is deliberately NOT called — that is
  ``twin_repair``'s in-project transition and would remove a copy entirely.
Owner-only everywhere (``scope.require_owner``); ``graph.link`` re-imposes the kbcore
write guards (Invariant 2). ``alternative_to`` is in ``models.RELATIONS`` and
``graph.link`` is on the A11-3 web allowlist.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from kbcore import graph, scope, twin_judge
from kbcore.config import settings
from kbcore.db import connect
from kbcore.errors import InvalidOperation

#: The disposition — a symmetric "these two say the same thing" edge (models.RELATIONS).
ALTERNATIVE_TO = "alternative_to"
#: Consolidation OUTPUT is not source knowledge; excluded exactly as the dedupe excludes it.
EXCLUDED_TYPES = ("summary", "report")

OK = "ok"
REFUSE_ALREADY_LINKED = "already_linked"
REFUSE_DIFFERENT_FACT = "different_fact"
REFUSE_JUDGE_UNAVAILABLE = "judge_unavailable"


@dataclass(frozen=True)
class TwinSide:
    """One end of a candidate pair — enough to eyeball it without a lookup."""

    item_id: int
    project: str
    type: str
    title: str
    created_at: datetime


@dataclass(frozen=True)
class CrossTwinPair:
    """A cross-project candidate. ``a.item_id < b.item_id`` (each pair enumerated once)."""

    similarity: float
    a: TwinSide
    b: TwinSide


@dataclass(frozen=True)
class CrossTwinOutcome:
    """One decision. ``linked`` is never True on a dry ``plan``; ``reason`` is the
    disposition code (:data:`OK` / a ``REFUSE_*``); ``rationale`` is the judge's
    sentence (or unavailability message)."""

    pair: CrossTwinPair
    eligible: bool
    linked: bool
    reason: str
    rationale: str


# Each unordered cross-project pair ONCE (ia.id < ib.id); different projects only
# (same-project is dedupe's job). Cosine SIMILARITY strictly above the threshold;
# both leads on the CURRENT model. {proj_filter} is a constant fragment; values ride params.
_CAND_SQL = """
SELECT (1 - (ca.embedding <=> cb.embedding)) AS sim,
       ia.id, ia.project, ia.type, ia.title, ia.created_at,
       ib.id, ib.project, ib.type, ib.title, ib.created_at
FROM items ia
JOIN chunks ca ON ca.item_id = ia.id AND ca.ord = 0
JOIN items ib ON ib.id > ia.id
             AND ib.project <> ia.project
             AND ib.status = 'active'
             AND ib.type <> ALL(%(skip_types)s)
JOIN chunks cb ON cb.item_id = ib.id AND cb.ord = 0
WHERE ia.status = 'active'
  AND ia.type <> ALL(%(skip_types)s)
  AND ca.embedding IS NOT NULL AND cb.embedding IS NOT NULL
  AND ca.embed_model = %(model)s AND cb.embed_model = %(model)s
  AND (1 - (ca.embedding <=> cb.embedding)) > %(threshold)s
  {proj_filter}
ORDER BY sim DESC, ia.id, ib.id
LIMIT %(cap)s
"""

_LINK_EXISTS_SQL = """
SELECT 1 FROM relations
WHERE rel = %(rel)s AND src_kind = 'item' AND dst_kind = 'item'
  AND ((src_id = %(a)s AND dst_id = %(b)s) OR (src_id = %(b)s AND dst_id = %(a)s))
LIMIT 1
"""


def _side(row: Sequence[Any], off: int) -> TwinSide:
    return TwinSide(
        item_id=int(row[off]),
        project=row[off + 1],
        type=row[off + 2],
        title=row[off + 3],
        created_at=row[off + 4],
    )


def _proj_filter(params: dict[str, Any], project_a: str | None, project_b: str | None) -> str:
    """Constant SQL fragment restricting the pair to the requested project(s): both
    given ⇒ that unordered pair; one ⇒ pairs touching it; neither ⇒ all cross-project
    pairs. Names ride ``params`` (never the fragment)."""
    if project_a is not None and project_b is not None:
        params["pa"], params["pb"] = project_a, project_b
        return (
            "AND ((ia.project = %(pa)s AND ib.project = %(pb)s) "
            "OR (ia.project = %(pb)s AND ib.project = %(pa)s))"
        )
    if project_a is not None:
        params["pa"] = project_a
        return "AND (ia.project = %(pa)s OR ib.project = %(pa)s)"
    if project_b is not None:
        params["pb"] = project_b
        return "AND (ia.project = %(pb)s OR ib.project = %(pb)s)"
    return ""


def enumerate_candidates(
    project_a: str | None = None,
    project_b: str | None = None,
    *,
    threshold: float | None = None,
    cap: int | None = None,
) -> list[CrossTwinPair]:
    """READ-ONLY cross-project candidate pairs, closest first. Writes NOTHING.
    ``threshold``/``cap`` default to ``kb_xtwin_threshold`` / ``kb_xtwin_cap``; ``cap
    <= 0`` is the KILL SWITCH (returns ``[]`` untouched). ``project_a``/``project_b``,
    when both given, must differ (cross-project only)."""
    scope.require_owner("cross-project twin detection")
    thr = settings.kb_xtwin_threshold if threshold is None else float(threshold)
    lim = settings.kb_xtwin_cap if cap is None else int(cap)
    if not 0.0 <= thr <= 1.0:
        raise InvalidOperation("threshold is a cosine similarity in [0, 1]")
    if project_a is not None and project_a == project_b:
        raise InvalidOperation(
            "cross-project twin detection is CROSS-project by design; "
            "project_a and project_b must differ"
        )
    if lim <= 0:  # kill switch — nothing enumerated, so nothing judged or linked
        return []
    params: dict[str, Any] = {
        "skip_types": list(EXCLUDED_TYPES),
        "model": settings.kb_embed_model,
        "threshold": thr,
        "cap": lim,
    }
    sql = _CAND_SQL.format(proj_filter=_proj_filter(params, project_a, project_b))
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()  # noqa: S608 — proj_filter is constant
    return [CrossTwinPair(similarity=float(r[0]), a=_side(r, 1), b=_side(r, 6)) for r in rows]


def _judge_same_fact(pair: CrossTwinPair) -> tuple[bool, bool, str]:
    """(same_fact, judged, rationale) — the STRICT, fenced judge in
    :mod:`kbcore.twin_judge` (prompt, nonce delimiters, and why a non-boolean
    verdict is no verdict). Unavailable/unparseable/non-boolean ⇒ ``judged=False``."""
    return twin_judge.judge_same_fact(
        pair.a.item_id, pair.a.project, pair.b.item_id, pair.b.project
    )


def _already_linked(a_id: int, b_id: int) -> bool:
    with connect() as conn:
        p = {"rel": ALTERNATIVE_TO, "a": a_id, "b": b_id}
        return conn.execute(_LINK_EXISTS_SQL, p).fetchone() is not None


def _reject_same_project(pair: CrossTwinPair) -> None:
    """Same-project pairs are dedupe's by settled design (mirrors twin_repair)."""
    if pair.a.project == pair.b.project:
        raise InvalidOperation(
            "cross-project twin detection never links a SAME-project pair "
            f"({pair.a.project!r}); that join is dedupe's by settled design"
        )
    if pair.a.item_id == pair.b.item_id:
        raise InvalidOperation("an item cannot be a twin of itself")


def plan(pairs: Sequence[CrossTwinPair]) -> list[CrossTwinOutcome]:
    """Judge each candidate for same-fact. OWNER-ONLY. Writes NOTHING (the dry pass).
    An already-linked pair is reported ``already_linked`` and NOT re-judged (idempotent
    + saves a 14B call). Same-fact ⇒ ELIGIBLE; else a refusal :func:`apply_links` ignores."""
    scope.require_owner("cross-project twin detection")
    outcomes: list[CrossTwinOutcome] = []
    for pair in pairs:
        _reject_same_project(pair)
        if _already_linked(pair.a.item_id, pair.b.item_id):
            outcomes.append(
                CrossTwinOutcome(pair, False, False, REFUSE_ALREADY_LINKED, "already linked")
            )
            continue
        same, judged, rationale = _judge_same_fact(pair)
        if not judged:
            outcomes.append(
                CrossTwinOutcome(pair, False, False, REFUSE_JUDGE_UNAVAILABLE, rationale)
            )
        elif same:
            outcomes.append(CrossTwinOutcome(pair, True, False, OK, rationale))
        else:
            outcomes.append(CrossTwinOutcome(pair, False, False, REFUSE_DIFFERENT_FACT, rationale))
    return outcomes


def apply_links(outcomes: Sequence[CrossTwinOutcome]) -> list[CrossTwinOutcome]:
    """Link each ELIGIBLE pair ``a -alternative_to-> b``. OWNER-ONLY. The ONLY writer.
    It flips NO status (Invariant 5): both items stay on their projects' surfaces.
    :func:`graph.link` is idempotent and re-imposes the kbcore write guards; non-eligible
    outcomes pass through untouched."""
    scope.require_owner("cross-project twin detection")
    applied: list[CrossTwinOutcome] = []
    for o in outcomes:
        if not o.eligible:
            applied.append(o)
            continue
        _reject_same_project(o.pair)
        graph.link(o.pair.a.item_id, ALTERNATIVE_TO, o.pair.b.item_id)
        applied.append(replace(o, linked=True))
    return applied


def summarize(outcomes: Sequence[CrossTwinOutcome]) -> dict[str, int]:
    """Counts by decision — ``eligible``/``linked`` plus one key per refusal code."""
    counts: dict[str, int] = {"pairs": len(outcomes), "eligible": 0, "linked": 0}
    for o in outcomes:
        counts["eligible"] += int(o.eligible)
        counts["linked"] += int(o.linked)
        if o.reason != OK:
            counts[o.reason] = counts.get(o.reason, 0) + 1
    return counts

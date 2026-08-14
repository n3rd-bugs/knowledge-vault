"""kbcore.twin_crossproject — owner-fired CROSS-PROJECT twin detector (KB 13061).

The systemic finding: the SAME gotcha captured in TWO projects (golden misses
124/858 and 383/488) leaves BOTH copies active, the newer richer twin outranks the
original, and rank is diluted. The nightly dedupe never folds them — its join is
SAME-project by settled design (a scoping verdict; project walls) that is NOT
changed here and must not be changed elsewhere (ADVERSARIAL_REVIEW).

The narrow owner-invoked cross-project pass KB 13061 called for, CORRECTED for
scope-safety — DISPOSITION IS LINK, NOT RETIRE:
* :func:`probe_candidates` / :func:`enumerate_candidates` (READ-ONLY, re-exported
  from :mod:`kbcore.twin_probe` — split for the 300-line ratchet, same as
  :mod:`kbcore.twin_judge`): a BOUNDED, INDEX-ASSISTED scan in place of the old
  O(N^2) self-join (review #3 P2, KB 19663 — unrunnable at 19.6k items). Each
  probed item takes one pgvector KNN probe filtered to OTHER projects; pairs are
  cosine-SIMILARITY strictly above ``kb_xtwin_threshold``, each unordered pair
  once, both leads CURRENT model, summaries/reports excluded, closest first,
  truncated to ``kb_xtwin_cap`` (0 = kill). ``probe_candidates`` also returns a
  :class:`ProbeCoverage` (N probed of M eligible) the CLI must print, so a
  bounded run never masquerades as exhaustive (no-silent-caps).
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

from kbcore import graph, scope, twin_judge
from kbcore.db import connect
from kbcore.errors import InvalidOperation

# THE ESTABLISHED SEAM (same pattern as ingest_queue's checkpoint re-exports):
# enumeration was split into twin_probe for the ratchet, and is re-imported HERE
# because this module owns the feature — every caller, CLI and test reaches it
# as ``twin_crossproject.<name>`` and the split must not be visible to them.
from kbcore.twin_probe import (  # noqa: F401
    EXCLUDED_TYPES,
    CrossTwinPair,
    ProbeCoverage,
    TwinSide,
    enumerate_candidates,
    probe_candidates,
)

#: The disposition — a symmetric "these two say the same thing" edge (models.RELATIONS).
ALTERNATIVE_TO = "alternative_to"

OK = "ok"
REFUSE_ALREADY_LINKED = "already_linked"
REFUSE_DIFFERENT_FACT = "different_fact"
REFUSE_JUDGE_UNAVAILABLE = "judge_unavailable"


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


_LINK_EXISTS_SQL = """
SELECT 1 FROM relations
WHERE rel = %(rel)s AND src_kind = 'item' AND dst_kind = 'item'
  AND ((src_id = %(a)s AND dst_id = %(b)s) OR (src_id = %(b)s AND dst_id = %(a)s))
LIMIT 1
"""


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

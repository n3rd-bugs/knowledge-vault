"""kbcore.twin_audit — READ-ONLY re-judge of every item-item ``alternative_to`` edge.

WHY (review #8, KB 31855 / 31857). The cross-project twin linker writes exactly one
kind of edge — ``item -alternative_to-> item`` — on a same-fact verdict from the
pair judge. A verdict can be wrong in either direction, and a wrong LINK is the
expensive one: ``search_rank.collapse_alternatives`` folds a linked pair whenever
both land in one result set, so a mislabeled twin hides a real answer. This module
asks the CURRENT pair judge (the seat behind ``KB_PAIR_JUDGE_MODEL``, reached only
through :func:`kbcore.twin_judge.judge_same_fact`) about every such edge and
REPORTS what it says.

IT NEVER WRITES. No unlink, no status flip, no review-queue row, no relation
touched — the review's guards say so outright ("do not route audit-sweep
proposals into the review queue"; "do not hand-DELETE any relation row"). An edge
the judge rejects is listed for the OWNER to read; reversal is an owner decision
and its tooling is Wave I. The 297<->294 disposition is KEEP regardless of any
verdict here (KB 31855): it is a manual 2026-07-03 ADR-alternatives edge in the
schema's own sense (001_init.sql: "bug<->fix, ADR alternatives"), which is why
every edge created before :data:`LINKER_EPOCH` is EXEMPT — flagged, not judged —
the linker did not exist yet, so those edges cannot be its output. Same-project
edges are exempt for the same reason: the linker refuses same-project pairs, so
such an edge is hand-made ADR knowledge (302->294).

Owner-only (the judge is the owner's GPU, Invariant 8) and explicitly user-invoked
(rule 8: ``kb repair twins-audit``). A mechanical test forbids this module from
importing any write path.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from kbcore import scope, twin_judge
from kbcore.db import connect

#: Edges created before this instant predate the cross-project linker's first run
#: (2026-08-09) and cannot be its output: hand-made ADR-alternatives edges in the
#: schema's sense — 297->294 and 302->294 among them (KB 31855). Exempt by default.
LINKER_EPOCH = datetime(2026, 8, 9, tzinfo=UTC)

#: Verdicts.
SAME = "same"
REJECTED = "rejected"
UNJUDGED = "unjudged"
EXEMPT = "exempt"

#: Exemption reasons (an exempt edge is flagged, never judged unless asked).
EXEMPT_MANUAL = "manual"
EXEMPT_SAME_PROJECT = "same_project"

#: ``(a_id, a_project, b_id, b_project) -> (same_fact, judged, rationale)`` — the
#: shape of :func:`kbcore.twin_judge.judge_same_fact`, injectable for tests.
Judge = Callable[[int, str, int, str], tuple[bool, bool, str]]


@dataclass(frozen=True)
class AuditEdge:
    """One item-item ``alternative_to`` relation with both endpoints resolved."""

    relation_id: int
    a_id: int
    a_project: str
    a_title: str
    a_status: str
    b_id: int
    b_project: str
    b_title: str
    b_status: str
    created_at: datetime

    @property
    def exempt_reason(self) -> str | None:
        """Why this edge is not linker output — or None when it could be."""
        if self.a_project == self.b_project:
            return EXEMPT_SAME_PROJECT
        if self.created_at < LINKER_EPOCH:
            return EXEMPT_MANUAL
        return None


@dataclass(frozen=True)
class AuditOutcome:
    """One verdict. ``exempt`` stays set even when an exempt edge was judged for
    reading (``judge_exempt``), so a table can show both facts."""

    edge: AuditEdge
    verdict: str
    rationale: str
    exempt: str | None


# LIMIT NULL is PostgreSQL for "no limit", so one parameterized statement serves both.
_EDGES_SQL = """
SELECT r.id, r.src_id, a.project, a.title, a.status,
       r.dst_id, b.project, b.title, b.status, r.created_at
  FROM relations r
  JOIN items a ON a.id = r.src_id
  JOIN items b ON b.id = r.dst_id
 WHERE r.rel = 'alternative_to' AND r.src_kind = 'item' AND r.dst_kind = 'item'
 ORDER BY r.created_at, r.id
 LIMIT %s
"""


def list_edges(limit: int | None = None) -> list[AuditEdge]:
    """Every item-item ``alternative_to`` edge, oldest first. OWNER-ONLY, read-only.

    Whole-table by design: the sweep's population is "every edge the linker could
    have written", not a sample, and the table is tiny (36 edges at review #8).
    """
    scope.require_owner("the alternative_to audit")
    with connect() as conn:
        rows = conn.execute(_EDGES_SQL, (int(limit) if limit is not None else None,)).fetchall()
    return [
        AuditEdge(
            relation_id=int(r[0]),
            a_id=int(r[1]),
            a_project=str(r[2]),
            a_title=str(r[3]),
            a_status=str(r[4]),
            b_id=int(r[5]),
            b_project=str(r[6]),
            b_title=str(r[7]),
            b_status=str(r[8]),
            created_at=r[9],
        )
        for r in rows
    ]


def classify(edge: AuditEdge, judge: Judge, *, judge_exempt: bool = False) -> AuditOutcome:
    """One edge -> one outcome. Exempt edges are NOT judged unless ``judge_exempt``
    (a GPU call spent on an edge whose disposition is already settled), and stay
    flagged exempt when they are."""
    exempt = edge.exempt_reason
    if exempt is not None and not judge_exempt:
        if exempt == EXEMPT_SAME_PROJECT:
            why = "same-project edge: never linker output (dedupe's domain); ADR-sense"
        else:
            why = (
                f"created {edge.created_at.date().isoformat()} < linker epoch "
                f"{LINKER_EPOCH.date().isoformat()}: manual ADR-alternatives edge (KB 31855)"
            )
        return AuditOutcome(edge, EXEMPT, why, exempt)
    same, judged, rationale = judge(edge.a_id, edge.a_project, edge.b_id, edge.b_project)
    if not judged:
        return AuditOutcome(edge, UNJUDGED, rationale, exempt)
    return AuditOutcome(edge, SAME if same else REJECTED, rationale, exempt)


def audit(
    edges: Sequence[AuditEdge],
    judge: Judge | None = None,
    *,
    judge_exempt: bool = False,
) -> list[AuditOutcome]:
    """Ask the pair judge about each edge. OWNER-ONLY. Writes NOTHING.

    ``judge`` defaults to :func:`kbcore.twin_judge.judge_same_fact` — the very
    function the linker uses, so this re-asks the linker's own question through the
    current pair-judge seat (fail-closed: an unavailable or non-boolean verdict is
    :data:`UNJUDGED`, never a rejection). Outcomes come back in input order.
    """
    scope.require_owner("the alternative_to audit")
    ask: Judge = judge or twin_judge.judge_same_fact
    return [classify(edge, ask, judge_exempt=judge_exempt) for edge in edges]


def summarize(outcomes: Sequence[AuditOutcome]) -> dict[str, int]:
    """Counts by verdict, plus ``exempt_judged`` (exempt edges judged for reading)."""
    counts: dict[str, int] = {
        "edges": len(outcomes),
        SAME: 0,
        REJECTED: 0,
        UNJUDGED: 0,
        EXEMPT: 0,
        "exempt_judged": 0,
    }
    for o in outcomes:
        counts[o.verdict] += 1
        if o.exempt is not None and o.verdict != EXEMPT:
            counts["exempt_judged"] += 1
    return counts

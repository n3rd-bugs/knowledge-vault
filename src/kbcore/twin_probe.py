"""kbcore.twin_probe — bounded cross-project twin ENUMERATION (KB 13061 / KB 19663).

Split out of :mod:`kbcore.twin_crossproject` (300-line ratchet — the same split
that produced :mod:`kbcore.twin_judge`); every name here is re-exported there, so
callers and tests keep reaching it as ``twin_crossproject.<name>``.

Review #3 P2 (KB 19663): the original enumeration was a cross-project lead-chunk
SELF-JOIN — O(N^2) distance computations, unrunnable at corpus size (19.6k items)
— which blocked the whole twin program. It is now a bounded, INDEX-ASSISTED scan:
each probed item (newest first, bounded by ``kb_xtwin_probe_limit``; 0 = every
eligible item) takes ONE pgvector KNN probe (``ORDER BY embedding <=> ... LIMIT
kb_xtwin_knn``, the existing hnsw index) filtered to OTHER projects; neighbours
with cosine SIMILARITY (``1 - distance``) strictly above the threshold become
pairs, each unordered pair once, closest first, truncated to ``kb_xtwin_cap``.
The scan returns a :class:`ProbeCoverage` (N items probed of M eligible) so a
bounded run can never masquerade as an exhaustive one (the no-silent-caps rule);
the CLI prints it. READ-ONLY throughout — dispositions live in twin_crossproject.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kbcore import scope
from kbcore.config import settings
from kbcore.db import connect
from kbcore.errors import InvalidOperation

#: Consolidation OUTPUT is not source knowledge; excluded exactly as the dedupe excludes it.
EXCLUDED_TYPES = ("summary", "report")


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
class ProbeCoverage:
    """How much of the eligible corpus a probe pass actually visited (KB 19663)."""

    probed: int
    eligible: int

    @property
    def exhaustive(self) -> bool:
        return self.probed >= self.eligible

    def render(self) -> str:
        tag = "exhaustive" if self.exhaustive else "BOUNDED — raise --probe-limit to widen"
        return f"coverage: probed {self.probed} of {self.eligible} eligible item(s) ({tag})"


# Probe-side eligibility — shared by the coverage count and the probe list:
# active, not consolidation output, lead chunk embedded on the CURRENT model.
# {probe_filter} is a constant fragment; values ride params.
_ELIGIBLE_FROM = """
FROM items ia
JOIN chunks ca ON ca.item_id = ia.id AND ca.ord = 0
WHERE ia.status = 'active'
  AND ia.type <> ALL(%(skip_types)s)
  AND ca.embedding IS NOT NULL AND ca.embed_model = %(model)s
  {probe_filter}
"""

# ONE KNN probe: nearest OTHER-project current-model leads to %(v)s, walked via
# the hnsw index (ORDER BY distance LIMIT k). The similarity floor is applied by
# the CALLER so the scan stays a plain index walk; {nb_filter} is constant.
_KNN_SQL = """
SELECT ib.id, ib.project, ib.type, ib.title, ib.created_at,
       (1 - (cb.embedding <=> %(v)s::vector)) AS sim
FROM chunks cb
JOIN items ib ON ib.id = cb.item_id
WHERE cb.ord = 0 AND cb.embed_model = %(model)s AND cb.embedding IS NOT NULL
  AND ib.status = 'active' AND ib.type <> ALL(%(skip_types)s)
  AND ib.project <> %(pproj)s
  {nb_filter}
ORDER BY cb.embedding <=> %(v)s::vector
LIMIT %(knn)s
"""


def _side(row: Sequence[Any], off: int) -> TwinSide:
    return TwinSide(
        item_id=int(row[off]),
        project=row[off + 1],
        type=row[off + 2],
        title=row[off + 3],
        created_at=row[off + 4],
    )


def _probe_filter(params: dict[str, Any], project_a: str | None, project_b: str | None) -> str:
    """Constant SQL fragment restricting PROBED items: both given ⇒ probe both sides
    of that unordered pair (the KNN side is then pinned to the pair too); one ⇒
    probe only its items — every pair touches it by construction, the KNN side
    supplies the other project; neither ⇒ probe everything. Names ride ``params``."""
    if project_a is not None and project_b is not None:
        params["pa"], params["pb"] = project_a, project_b
        return "AND ia.project IN (%(pa)s, %(pb)s)"
    if project_a is not None or project_b is not None:
        params["pa"] = project_a if project_a is not None else project_b
        return "AND ia.project = %(pa)s"
    return ""


def probe_candidates(
    project_a: str | None = None,
    project_b: str | None = None,
    *,
    threshold: float | None = None,
    cap: int | None = None,
    probe_limit: int | None = None,
    knn: int | None = None,
) -> tuple[list[CrossTwinPair], ProbeCoverage]:
    """READ-ONLY bounded enumeration (module docstring). Writes NOTHING.

    Defaults ride the ``kb_xtwin_*`` knobs. ``cap <= 0`` is the KILL SWITCH
    (nothing probed, judged or linked); ``cap`` otherwise truncates to the CLOSEST
    pairs AFTER the scan — it bounds the judge budget, never which items are
    probed. ``probe_limit <= 0`` probes EVERY eligible item; a positive value
    probes the newest ``probe_limit`` and the returned :class:`ProbeCoverage` says
    so. ``knn`` bounds neighbours fetched per probe (a recall floor per item, not
    a cap on total pairs). ``project_a``/``project_b``, when both given, must
    differ (cross-project only)."""
    scope.require_owner("cross-project twin detection")
    thr = settings.kb_xtwin_threshold if threshold is None else float(threshold)
    lim = settings.kb_xtwin_cap if cap is None else int(cap)
    plim = settings.kb_xtwin_probe_limit if probe_limit is None else int(probe_limit)
    kk = max(1, settings.kb_xtwin_knn if knn is None else int(knn))
    if not 0.0 <= thr <= 1.0:
        raise InvalidOperation("threshold is a cosine similarity in [0, 1]")
    if project_a is not None and project_a == project_b:
        raise InvalidOperation(
            "cross-project twin detection is CROSS-project by design; "
            "project_a and project_b must differ"
        )
    if lim <= 0:  # kill switch — nothing probed, so nothing judged or linked
        return [], ProbeCoverage(probed=0, eligible=0)
    params: dict[str, Any] = {"skip_types": list(EXCLUDED_TYPES), "model": settings.kb_embed_model}
    from_sql = _ELIGIBLE_FROM.format(probe_filter=_probe_filter(params, project_a, project_b))
    nb_filter = "AND ib.project IN (%(pa)s, %(pb)s)" if "pb" in params else ""
    knn_sql = _KNN_SQL.format(nb_filter=nb_filter)
    best: dict[tuple[int, int], CrossTwinPair] = {}
    with connect() as conn:
        # S608 noqa throughout: every interpolated fragment above is a constant.
        eligible = int(
            conn.execute("SELECT count(*) " + from_sql, params).fetchone()[0]  # noqa: S608
        )
        probes = conn.execute(
            "SELECT ia.id, ia.project, ia.type, ia.title, ia.created_at "  # noqa: S608
            + from_sql
            + " ORDER BY ia.id DESC LIMIT %(plim)s::bigint",  # NULL = probe everything
            {**params, "plim": plim if plim > 0 else None},
        ).fetchall()
        for row in probes:
            side = _side(row, 0)
            vec = conn.execute(
                "SELECT embedding::text FROM chunks "
                "WHERE item_id = %s AND ord = 0 AND embed_model = %s",
                (side.item_id, settings.kb_embed_model),
            ).fetchone()[0]
            nb_rows = conn.execute(
                knn_sql, {**params, "v": vec, "pproj": side.project, "knn": kk}
            ).fetchall()
            for nb in nb_rows:
                sim = float(nb[5])
                if sim <= thr:  # strictly ABOVE the floor, exactly as the join was
                    continue
                other = _side(nb, 0)
                a, b = (side, other) if side.item_id < other.item_id else (other, side)
                key = (a.item_id, b.item_id)
                if key not in best or sim > best[key].similarity:
                    best[key] = CrossTwinPair(similarity=sim, a=a, b=b)
    pairs = sorted(best.values(), key=lambda p: (-p.similarity, p.a.item_id, p.b.item_id))
    return pairs[:lim], ProbeCoverage(probed=len(probes), eligible=eligible)


def enumerate_candidates(
    project_a: str | None = None,
    project_b: str | None = None,
    *,
    threshold: float | None = None,
    cap: int | None = None,
    probe_limit: int | None = None,
    knn: int | None = None,
) -> list[CrossTwinPair]:
    """Coverage-less :func:`probe_candidates`, kept for callers that only want the
    pairs (closest first). Surfaces that REPORT a run must use
    :func:`probe_candidates` and show its coverage (no-silent-caps, KB 19663)."""
    return probe_candidates(
        project_a, project_b, threshold=threshold, cap=cap, probe_limit=probe_limit, knn=knn
    )[0]

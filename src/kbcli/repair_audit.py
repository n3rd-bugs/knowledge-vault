"""kbcli.repair twins-audit — REPORT-ONLY re-judge of the ``alternative_to`` edges.

Thin shell over :mod:`kbcore.twin_audit` (no SQL, Invariant 2). Unlike its
siblings this command has NO ``--apply``: the sweep's job is to make the
mislabeled-twin class VISIBLE (edges the current pair judge rejects), and the
disposition of any edge is an owner decision taken elsewhere (KB 31855 — 297<->294
stays; the owner unlink tool is Wave I). It refuses to run while a consolidation
is in flight, because the judge shares the nightly's single model slot — a
TRY-AND-RELEASE probe of the consolidation lock, never held past the check.
"""

from __future__ import annotations

from collections.abc import Sequence

import typer

from kbcli.repair_shared import _clip, _fail, _guarded, repair_app
from kbcore import twin_audit
from kbcore.errors import KBError

_HEADER = (
    f"{'edge':>6}  {'a':>7}  {'a-project':14}  {'b':>7}  {'b-project':14}  "
    f"{'created':10}  {'verdict':30}  rationale"
)

_NOTE = """
report only — NOTHING was changed
  No edge was unlinked, no status flipped, no review row queued (KB 31855). An edge the
  judge REJECTED is the mislabeled-twin class: read the pair (kb get <a> / <b>, kb trace <a>)
  and decide as the owner; the reversal tool lands in Wave I. Manual pre-linker and
  same-project edges are ADR-sense alternatives and stay exempt (flagged, not judged,
  unless --judge-exempt). Re-run at every review: rejections above 10% of linker edges
  are the linker threshold re-tune trigger.""".strip()


def _label(o: twin_audit.AuditOutcome) -> str:
    if o.verdict == twin_audit.EXEMPT:
        return f"exempt:{o.exempt}"
    shown = o.verdict.upper() if o.verdict == twin_audit.REJECTED else o.verdict
    return f"{shown} (exempt:{o.exempt})" if o.exempt else shown


def _table(outcomes: Sequence[twin_audit.AuditOutcome]) -> str:
    lines = [_HEADER, "-" * len(_HEADER)]
    for o in outcomes:
        e = o.edge
        lines.append(
            f"{'#' + str(e.relation_id):>6}  {'#' + str(e.a_id):>7}  {_clip(e.a_project, 14):14}  "
            f"{'#' + str(e.b_id):>7}  {_clip(e.b_project, 14):14}  "
            f"{e.created_at.date().isoformat():10}  {_clip(_label(o), 30):30}  "
            f"{_clip(o.rationale, 48)}"
        )
    return "\n".join(lines)


def _tally(outcomes: Sequence[twin_audit.AuditOutcome]) -> str:
    c = twin_audit.summarize(outcomes)
    tail = f" ({c['exempt_judged']} exempt judged for reading)" if c["exempt_judged"] else ""
    return (
        f"{c['edges']} alternative_to edge(s): {c[twin_audit.SAME]} same, "
        f"{c[twin_audit.REJECTED]} REJECTED, {c[twin_audit.UNJUDGED]} unjudged, "
        f"{c[twin_audit.EXEMPT]} exempt{tail}"
    )


def _refuse_while_nightly_runs() -> None:
    """TRY-AND-RELEASE probe of the consolidation lock — acquired and dropped in
    one breath, never HELD (holding it would make the nightly skip its whole
    night). The key is schema-scoped inside ``advisory_lock``; a literal
    ``pg_try_advisory_lock(42)`` would never see the nightly's lock."""
    from kbcore.db import advisory_lock
    from kbworker.consolidate import ADVISORY_KEY  # the single source of the key

    with advisory_lock(ADVISORY_KEY) as acquired:
        held_elsewhere = not acquired
    if held_elsewhere:
        _fail(
            KBError(
                f"consolidation lock {ADVISORY_KEY} is held (a run is in flight) — the judge "
                "shares its model slot; nothing read, nothing written. Retry when it finishes."
            )
        )


@repair_app.command("twins-audit")
def repair_twins_audit(
    limit: int | None = typer.Option(None, "--limit", help="only the oldest N edges"),
    judge_exempt: bool = typer.Option(
        False,
        "--judge-exempt",
        help="also ask the judge about exempt edges (manual pre-linker / same-project) "
        "for reading; they stay flagged exempt",
    ),
) -> None:
    """Re-judge every item-item `alternative_to` edge with the CURRENT pair judge and
    REPORT. Never unlinks, flips no status, queues nothing (KB 31855). Run on an idle
    GPU slot — it refuses while a consolidation run holds the lock."""
    _refuse_while_nightly_runs()
    edges = _guarded(lambda: twin_audit.list_edges(limit))
    if not edges:
        typer.echo("no item-item alternative_to edges to audit")
        return
    outcomes = _guarded(lambda: twin_audit.audit(edges, judge_exempt=judge_exempt))
    typer.echo(_table(outcomes))
    typer.echo("")
    typer.echo(_tally(outcomes))
    rejected = [o for o in outcomes if o.verdict == twin_audit.REJECTED]
    if rejected:
        typer.echo("")
        typer.echo("rejected by the pair judge (owner reading list; nothing was changed):")
        for o in rejected:
            e = o.edge
            typer.echo(
                f"  #{e.a_id} ({e.a_project}) alternative_to #{e.b_id} ({e.b_project}) "
                f"— {_clip(o.rationale, 90)}"
            )
    typer.echo("")
    typer.echo(_NOTE)

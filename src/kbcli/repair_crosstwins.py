"""kbcli.repair cross-twins — link the SAME knowledge captured in two projects.

Split from :mod:`kbcli.repair_cmds` (2026-08-09, ratchet). The cross-project
twin pass (design review #2 / KB 13061): nightly dedupe is per-project by
settled design, so the same gotcha captured under two projects leaves both
copies active and dilutes rank. This command LINKS such a pair with an
``alternative_to`` edge — it flips no status and retires nothing (Inv 5); the
rank-time collapse folds the pair only when both land in one result set. Thin
shell over :mod:`kbcore.twin_crossproject`; no SQL (Invariant 2).
"""

from __future__ import annotations

import typer

from kbcli.repair_shared import _clip, _flow, repair_app
from kbcore import twin_crossproject

_X_HEADER = (
    f"{'sim':>6}  {'a':>8}  {'a-project':16}  {'b':>8}  {'b-project':16}  "
    f"{'decision':16}  judge rationale"
)

_X_UNDO = """
undo / audit trail
  Nothing left any project's surface (Inv 5): each confirmed pair gained ONE
  `alternative_to` edge; NO status flipped, nothing deleted, nothing retired. The
  rank-time collapse folds a linked pair only when both land in one result set.
    inspect  kb get <a-id> / <b-id>    walk  kb trace <a-id>    re-check  kb repair cross-twins
  Reverse a pair by removing its `alternative_to` relation (a deliberate owner action).""".strip()


def _x_table(outcomes: list[twin_crossproject.CrossTwinOutcome], applied: bool) -> str:
    lines = [_X_HEADER, "-" * len(_X_HEADER)]
    for o in outcomes:
        if o.linked:
            decision = "LINKED"
        elif o.eligible:
            decision = "LINK FAILED" if applied else "would link"
        else:
            decision = f"skip: {o.reason}"
        p = o.pair
        lines.append(
            f"{p.similarity:>6.3f}  {'#' + str(p.a.item_id):>8}  {_clip(p.a.project, 16):16}  "
            f"{'#' + str(p.b.item_id):>8}  {_clip(p.b.project, 16):16}  {decision:16}  "
            f"{_clip(o.rationale, 40)}"
        )
    return "\n".join(lines)


def _x_tally(outcomes: list[twin_crossproject.CrossTwinOutcome]) -> str:
    c = twin_crossproject.summarize(outcomes)
    skips = {k: v for k, v in c.items() if k not in ("pairs", "eligible", "linked")}
    tail = "; " + ", ".join(f"{k}={v}" for k, v in sorted(skips.items())) if skips else ""
    return f"{c['pairs']} candidate pair(s), {c['eligible']} eligible, {c['linked']} linked{tail}"


@repair_app.command("cross-twins")
def repair_cross_twins(
    threshold: float | None = typer.Option(
        None,
        "--threshold",
        help="cross-project cosine similarity floor (default: kb_xtwin_threshold)",
    ),
    project_a: str | None = typer.Option(
        None, "--project-a", help="restrict to twins touching this project"
    ),
    project_b: str | None = typer.Option(
        None, "--project-b", help="pair --project-a only against this project (must differ)"
    ),
    cap: int | None = typer.Option(
        None, "--cap", help="max candidate pairs enumerated + judged (0 = kill switch)"
    ),
    probe_limit: int | None = typer.Option(
        None,
        "--probe-limit",
        help="max items KNN-probed, newest first (default: kb_xtwin_probe_limit; 0 = all)",
    ),
    apply: bool = typer.Option(
        False, "--apply", help="create the links (default: dry run, writes nothing)"
    ),
) -> None:
    """Link cross-project twins (the SAME knowledge captured in two projects) with an
    `alternative_to` edge — confirmed same-fact by the 14B judge (fail-closed). NEVER
    retires and flips no status: nothing leaves any project's search surface (Inv 5)."""

    def _dry() -> list:
        pairs, coverage = twin_crossproject.probe_candidates(
            project_a, project_b, threshold=threshold, cap=cap, probe_limit=probe_limit
        )
        # No silent caps (review #3, KB 19663): a bounded scan must SAY it was bounded.
        typer.echo(coverage.render())
        return twin_crossproject.plan(pairs)

    _flow(
        apply=apply,
        dry_fn=_dry,
        apply_fn=twin_crossproject.apply_links,
        render=_x_table,
        tally=_x_tally,
        eligible_pred=lambda o: o.eligible,
        empty="no cross-project twins above the similarity floor",
        dry_hint="dry run — nothing written. Re-run with --apply to link the pairs above.",
        none_hint="nothing eligible to link — no candidate was judged same-fact.",
        prompt=lambda n: f"Link {n} cross-project twin pair(s) with 'alternative_to'?",
        undo=_X_UNDO,
    )

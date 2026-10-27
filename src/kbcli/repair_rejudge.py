"""kbcli.repair rejudge-oversized — re-ask the contradiction seat about pairs judged blind.

KB 55589: until the pair-judge prompt was fitted to the window (:mod:`kbcore.judge_fit`),
a pair whose prompt overflowed the context was judged with its system prompt cut away,
and the reply, read as "no conflict", was memorized for good. This command lists those
memorized pairs (dry run, the default) and, with ``--apply``, asks the seat again with
the fitted prompt — the GPU, an explicit owner run on an idle slot — queueing every
"yes" for review the way the nightly does. Thin shell over
:mod:`kbcore.contradiction_rejudge`; no SQL (Invariant 2).
"""

from __future__ import annotations

import typer

from kbcli.repair_shared import _clip, _flow, repair_app
from kbcore import contradiction_rejudge, judge_fit
from kbcore.config import settings

_R_HEADER = f"{'a':>8}  {'b':>8}  {'project':16}  {'tokens':>7}  {'verdict':12}  titles"

_R_UNDO = """
undo / audit trail
  Nothing was retired, flipped or deleted (Inv 5). Each conflict found is ONE new
  `contradicts` edge plus an open contradiction review row — an ordinary one: the nightly
  evidence step may auto-resolve it after a stable two-night suggestion (the precision
  gate, when on, re-asks this same request, so it confirms rather than re-checks). To keep
  a pair for yourself, resolve or dismiss its row before then.
    inspect  kb review list    close one  kb review dismiss <qid>
  The memorized verdict rows are untouched; they stay the nightly's exclusion key.""".strip()


def _verdict(r: contradiction_rejudge.Rejudge) -> str:
    if r.error.startswith("closed"):
        return "skipped"
    if r.error.startswith("judge failed"):
        return "judge failed"
    if r.error:
        return "STOPPED"
    if not r.judged:
        return "re-ask"
    if r.verdict is None:
        return "no verdict"
    if r.verdict:
        return "QUEUED" if r.queued else "conflict"
    return "no conflict"


def _r_table(rows: list[contradiction_rejudge.Rejudge], _applied: bool) -> str:
    lines = [_R_HEADER, "-" * len(_R_HEADER)]
    for r in rows:
        lines.append(
            f"{'#' + str(r.a_id):>8}  {'#' + str(r.b_id):>8}  {_clip(r.project, 16):16}  "
            f"{r.tokens:>7}  {_verdict(r):12}  "
            f"{_clip(r.a_title, 30)} / {_clip(r.b_title, 30)}"
        )
    return "\n".join(lines)


def _r_tally(rows: list[contradiction_rejudge.Rejudge]) -> str:
    head = (
        f"{len(rows)} 'no conflict' pair(s) memorized before the fitted judge, over the "
        f"{judge_fit.LEGACY_CEILING}-token ceiling"
    )
    if not any(r.judged or r.error for r in rows):
        return head
    kinds = [_verdict(r) for r in rows]
    tally = f"{head}: {kinds.count('QUEUED')} queued as conflicts, "
    tally += f"{kinds.count('no conflict')} no conflict, {kinds.count('no verdict')} no verdict"
    for kind, words in (
        ("conflict", "already recorded"),
        ("judge failed", "judge failed on the pair"),
        ("skipped", "closed meanwhile"),
    ):
        if kinds.count(kind):
            tally += f", {kinds.count(kind)} {words}"
    if "STOPPED" in kinds:
        stop = next(r.error for r in rows if _verdict(r) == "STOPPED")
        tally += f"; {kinds.count('re-ask') + 1} not judged ({stop})"
    return tally


@repair_app.command("rejudge-oversized")
def repair_rejudge_oversized(
    apply: bool = typer.Option(
        False,
        "--apply",
        help="re-judge on the contradiction seat (GPU) and queue each conflict found "
        "(default: dry run, nothing judged or written)",
    ),
) -> None:
    """Re-judge memorized 'no conflict' pairs whose prompt overflowed the judge (KB 55589)."""

    def progress(r: contradiction_rejudge.Rejudge) -> None:
        typer.echo(f"  #{r.a_id} / #{r.b_id}: {_verdict(r)}")

    ceiling = judge_fit.LEGACY_CEILING
    _flow(
        apply=apply,
        dry_fn=contradiction_rejudge.candidates,
        apply_fn=lambda eligible: contradiction_rejudge.rejudge(eligible, on_progress=progress),
        render=_r_table,
        tally=_r_tally,
        eligible_pred=lambda _r: True,
        empty=f"no pre-fit 'no conflict' pair is over the {ceiling}-token ceiling: nothing to do",
        dry_hint="dry run — nothing judged or written. Re-run with --apply (GPU, idle slot).",
        none_hint="nothing to re-judge.",
        prompt=lambda n: (
            f"Re-judge {n} pair(s) on {settings.contradiction_judge_model} (GPU) "
            "and queue each conflict found?"
        ),
        undo=_R_UNDO,
    )

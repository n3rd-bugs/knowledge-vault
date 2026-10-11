"""kbcli.repair resend-dups --replayed — the tier-3 dry run / apply / undo (wave C.2).

Split from :mod:`kbcli.repair_resend` (ratchet). Thin shell over
:mod:`kbcore.resend_repair_replay` (no SQL, Invariant 2): the byte-range repair of
ONE session's same-session re-send (KB 49064). Dry run by default; ``--apply``
writes under the consolidation advisory lock; ``--undo`` (same selector) reverses
exactly that run. Owner-only — kbcore checks it again on every call.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import typer

from kbcli.repair_shared import _clip, _guarded, _locked_apply
from kbcore.resend_repair_models import RepairSelector, ReplayPlan
from kbcore.resend_repair_replay_gate import prefix_note
from kbcore.resend_repair_replay_rows import PREFIX_TOLERANCE

_HEAD = (
    f"{'project':16}  {'cand':>6}  {'archive':>7}  {'recalled':>8}  {'no-quote':>8}  "
    f"{'new-content':>11}  {'successor':>9}  reprocess"
)
LEGEND = (
    "archive = candidates - recalled - no-quote - new-content - successor - reprocess "
    "('recalled' includes automated brief/turn-start recall hits -- kept: the safe direction)"
)


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB"


def _counts(d: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(d.items())) or "none"


def _when(t: datetime | None) -> str:
    return t.astimezone(UTC).isoformat() if t else "-"


def replay_report(p: ReplayPlan) -> str:
    """U, --since, C, the gate, R, W0/W1, NEW, the row marks, the refusals, then the
    legend and one line per project."""
    gap = "none" if not p.gaps else f"{p.gaps} gap(s) / {_mb(p.gap_bytes)} below C"
    lines = [
        f"session  {p.uri}   --since {p.since.isoformat()}",
        f"covered  C = {p.covered_end:,} bytes ({_mb(p.covered_end)}) by {p.pre_stamped} "
        f"stamped row(s); first stamp at {p.first_stamped_offset:,}; unstamped prefix "
        f"{p.pre_unstamped_rows} row(s) / {_mb(p.pre_unstamped_bytes)}; stamp gaps: {gap}",
        f"gate     {p.pre_rows} row(s) before --since: {_counts(p.pre_status)} -- ok",
        f"re-sent  R = {len(p.r_ids)} row(s) / {_mb(p.r_bytes)} inside [0, C) "
        f"({_counts(p.r_status)}); W0 (first R row) {_when(p.w0)}; R rows done "
        f"{_when(p.first_done)} .. W1 {_when(p.w1)} (no candidate after W1)",
        f"new      NEW = {len(p.new_ids)} row(s) / {_mb(p.new_bytes)} processed at/after W0, "
        f"undrained, or gap -> NEW {p.r_in_gap} -- an item whose quote is in NEW is kept",
        f"rows     mark {len(p.mark_ids)} R row(s) with meta.repair (status kept); "
        f"{p.marked_before} already marked; {p.other_marker} carry another repair (left alone)",
    ]
    if p.prefix_short:
        lines.append(
            f"CHECK    unstamped prefix {_mb(p.pre_unstamped_bytes)} is more than "
            f"{PREFIX_TOLERANCE:.0%} short of the first stamp at {p.first_stamped_offset:,}: "
            "bytes below it may never have been sent -- --apply refused"
        )
    if note := prefix_note(p):
        lines.append(note)
    if p.r_undrained:
        lines.append(
            f"HOLD     {p.r_undrained} R row(s) still pending or retryable -- --apply refused: "
            "re-run after the drain"
        )
    lines += ["", LEGEND, _HEAD, "-" * len(_HEAD)]
    for name, pp in sorted(p.projects.items()):
        lines.append(
            f"{_clip(name, 16):16}  {pp.candidates:>6}  {len(pp.archive_ids):>7}  "
            f"{pp.keep_recalled:>8}  {pp.keep_no_quote:>8}  {pp.keep_new_content:>11}  "
            f"{pp.keep_successor:>9}  {pp.keep_reprocess}"
            + (f"   ARCHIVED {pp.archived}" if pp.archived else "")
        )
    if not p.projects:
        lines.append("(no candidate items)")
    return "\n".join(lines)


def _command(sel: RepairSelector, p: ReplayPlan, tail: str) -> str:
    since = p.since.isoformat()
    return f"kb repair resend-dups --session {sel.session} --replayed --since {since} {tail}"


def run_replay(
    sel: RepairSelector, *, apply: bool, undo: bool, undo_flow: Callable[..., None]
) -> None:
    """Dry run -> (``--apply``) confirm -> apply under the lock -> the undo trail."""
    from kbcore import resend_repair_replay as replay

    if undo:
        undo_flow(sel, apply, replay.undo, rows="marker removed (status kept)")
        return
    plan = _guarded(lambda: replay.plan(sel))
    typer.echo(replay_report(plan))
    refusals = plan.apply_refusals
    if not apply:
        if refusals:
            typer.echo("\ndry run — nothing written. --apply is refused: " + "; ".join(refusals))
        else:
            typer.echo(
                f"\ndry run — nothing written. To apply:\n  {_command(sel, plan, '--apply')}"
            )
        return
    _guarded(lambda: replay.check_apply(plan))
    if not plan.eligible:
        typer.echo("\nnothing to do — no item to archive and no re-sent row to mark.")
        return
    typer.confirm(
        f"Archive {plan.archive_count} item(s) and mark {len(plan.mark_ids)} row(s)?", abort=True
    )
    done = _locked_apply(lambda: replay.apply(plan))
    typer.echo("")
    typer.echo(replay_report(done))
    archived = sum(pp.archived for pp in done.projects.values())
    typer.echo(
        f"\nmarked {done.rows_marked} row(s); archived {archived} item(s) "
        f"(reason {replay.REPLAY_REASON}); their open review rows resolved. Nothing deleted.\n"
        f"  reverse   {_command(sel, done, '--undo [--apply]')}"
    )

"""kbcli.review_cmds — the `kb review` sub-app (PLAN.md §7.4).

Non-interactive so it is scriptable and testable: list open queue entries, then
promote / reject / supersede one at a time. All writes go through kbcore.review;
domain errors become clean exit messages.
"""

from __future__ import annotations

from datetime import datetime

import typer

from kbcli import bulk_cmds, expire_cmds, promote_cmds
from kbcli.repair_shared import _locked_apply
from kbcore import render, review_apply, review_restore, review_triage
from kbcore import review as review_mod
from kbcore.config import settings
from kbcore.errors import KBError

review_app = typer.Typer(help="Review queue: promote/reject/supersede unverified items")

# The quiet-expiry pair lives in kbcli.expire_cmds (this file is at the 300-line
# ratchet) but is part of THIS sub-app: `kb review expire` / `kb review
# undo-expiry`. Registering the functions here is exactly what the decorator
# does, so the two commands behave like any other in the group.
review_app.command("expire")(expire_cmds.review_expire_cmd)
review_app.command("undo-expiry")(expire_cmds.review_undo_expiry)
# The PROMOTE PROGRAM pair (review #3 P1, KB 19663) lives in kbcli.promote_cmds
# for the same ratchet reason: `kb review promote-candidates` / `promote-batch`.
review_app.command("promote-candidates")(promote_cmds.review_promote_candidates)
review_app.command("promote-batch")(promote_cmds.review_promote_batch)
review_app.command("promote-run")(promote_cmds.review_promote_run)
# `kb review promote-runs` (review #8 H13c): the read-only listing that tells the
# owner WHICH run key to paste into promote-run — same module, same seam.
review_app.command("promote-runs")(promote_cmds.review_promote_runs)
# `kb review bulk` moved to kbcli.bulk_cmds (review #5 strat-8) when it grew the
# --suggested-rejects dry-run/confirm mode; same ratchet seam as the two above.
review_app.command("bulk")(bulk_cmds.review_bulk)


def _fail(err: Exception) -> None:
    typer.secho(f"error: {err}", fg="red", err=True)
    raise typer.Exit(code=1)


@review_app.command("list")
def review_list() -> None:
    """List open review-queue entries (unverified extractions, contradictions)."""
    # Delegates to render_review so CLI/MCP output stays identical (incl. the
    # contradiction evidence-count lines).
    typer.echo(render.render_review(review_mod.list_open_review()))


@review_app.command("promote")
def review_promote(queue_id: int = typer.Argument(...)) -> None:
    """Promote the queued item to trust='verified' and resolve the row."""
    try:
        item_id = review_mod.promote(queue_id)
    except KBError as e:
        _fail(e)
    typer.echo(f"promoted item {item_id} to verified (queue {queue_id} resolved)")


@review_app.command("reject")
def review_reject(queue_id: int = typer.Argument(...)) -> None:
    """Archive the queued item (never deleted) and resolve the row."""
    try:
        item_id = review_mod.reject(queue_id)
    except KBError as e:
        _fail(e)
    typer.echo(f"archived item {item_id} (queue {queue_id} resolved)")


@review_app.command("supersede")
def review_supersede(
    queue_id: int = typer.Argument(...),
    new_item: int = typer.Option(..., "--new-item", help="id of the winning item"),
) -> None:
    """Supersede the flagged item with an existing one (contradictions) and resolve."""
    try:
        old_id = review_mod.resolve_via_supersede(queue_id, new_item)
    except KBError as e:
        _fail(e)
    typer.echo(f"item {old_id} superseded by {new_item} (queue {queue_id} resolved)")


@review_app.command("dismiss")
def review_dismiss(queue_id: int = typer.Argument(...)) -> None:
    """Close a queue row without touching its item (works for item-less rows too)."""
    try:
        review_mod.dismiss(queue_id)
    except KBError as e:
        _fail(e)
    typer.echo(f"queue {queue_id} dismissed (item untouched)")


@review_app.command("triage")
def review_triage_run(
    batch: int | None = typer.Option(
        None, "--batch", help="max rows to triage this run (default: KB_REVIEW_TRIAGE_BATCH)"
    ),
) -> None:
    """Ask the 14B for keep/reject suggestions on pending unverified extractions.

    Explicit user-invoked GPU run — the Invariant-8 carve-out, sharing
    review_triage.run_triage with the nightly ``review_triaged`` step. Stores
    SUGGESTIONS only (owner decision 2026-07-18): nothing is archived until a
    human runs ``kb review apply-triage``.
    """
    try:
        stored = review_triage.run_triage(
            settings.kb_review_triage_batch if batch is None else batch
        )
    except KBError as e:
        _fail(e)
    typer.echo(f"stored {stored} triage suggestion(s)")


@review_app.command("apply-triage")
def review_apply_triage(
    actions: str = typer.Option(
        "",
        "--actions",
        help='filter suggestion kinds, e.g. "keep,reject_noise" (default: all three)',
    ),
    project: str | None = typer.Option(None, "--project", help="only this project's rows"),
    ids: str = typer.Option("", "--ids", help='comma-separated queue ids, e.g. "1,2,3"'),
) -> None:
    """Apply stored LLM triage suggestions in bulk (the explicit human step).

    keep → row closes, item untouched; reject_noise → item archived;
    reject_duplicate → item marked duplicate of the suggested canonical. All
    status flips (Invariant 5); a row with a bad suggestion stays open.
    """
    try:
        queue_ids = [int(x) for x in ids.split(",") if x.strip()] or None
    except ValueError:
        _fail(KBError(f"--ids must be comma-separated integers, got {ids!r}"))
    action_set = {a.strip() for a in actions.split(",") if a.strip()} or None
    try:
        results = review_triage.apply_triage(
            actions=action_set, project=project, queue_ids=queue_ids
        )
    except KBError as e:
        _fail(e)
    for qid, outcome in results:
        typer.echo(f"[{qid}] {outcome}")
    errs = sum(1 for _, o in results if o.startswith("error"))
    typer.echo(
        f"apply-triage: {len(results) - errs} applied, {errs} error(s), {len(results)} row(s)"
    )


@review_app.command("auto-apply")
def review_auto_apply(
    cap: int | None = typer.Option(
        None, "--cap", help="max rows to apply (default: KB_AUTO_APPLY_CAP)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="report only; write nothing"),
) -> None:
    """Apply aged reject_noise/reject_duplicate triage suggestions automatically.

    The same entry point the nightly worker calls (owner decision 2026-08-02).
    Only reject_* suggestions past the cooling-off window, never recalled in 30
    days, with a live SAME-PROJECT dup target the twin linker has not already
    ruled on — 'keep' is never touched and trust is never raised. Every effect,
    retirement and cluster-winner close alike, is stamped and reversible via
    ``undo-auto``, and both share the one --cap.
    """
    try:
        counts = review_apply.auto_apply_rejects(cap, dry_run=dry_run)
    except KBError as e:
        _fail(e)
    prefix = "would apply" if dry_run else "applied"
    typer.echo(
        f"{prefix}: {counts['applied_noise']} noise, {counts['applied_duplicate']} duplicate"
    )
    typer.echo(
        f"skipped: {counts['skipped_cooling']} cooling, "
        f"{counts['skipped_accessed']} accessed, {counts['skipped_target_gone']} target gone, "
        f"{counts['skipped_unresolvable_target']} unresolvable, "
        f"{counts['skipped_cross_project']} cross-project, "
        f"{counts['skipped_linked_alternative']} linked"
    )
    typer.echo(
        f"cluster winners: {counts['skipped_cluster_winner']} "
        f"({counts['cluster_winners_closed']} closed, "
        f"{counts['cluster_winner_remaining']} left for the next run)"
    )
    typer.echo(f"eligible remaining: {counts['eligible_remaining']}")


@review_app.command("undo-auto")
def review_undo_auto(
    since: str = typer.Option(..., "--since", help="ISO timestamp, e.g. 2026-08-02T00:00:00+00:00"),
    dry_run: bool = typer.Option(False, "--dry-run", help="report only; write nothing"),
) -> None:
    """Reverse auto-applied rejects stamped at/after --since (owner escape hatch).

    Items go back to active (only from the status the automation set), rows
    reopen, and each reversed row is permanently vetoed against re-application.
    """
    try:
        when = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        _fail(KBError(f"--since must be an ISO timestamp, got {since!r}"))
    if when.tzinfo is None:
        _fail(KBError("--since must carry a timezone offset (UTC), e.g. ...T00:00:00+00:00"))
    try:
        qids = review_apply.undo_auto_applied(when, dry_run=dry_run)
    except KBError as e:
        _fail(e)
    verb = "would reverse" if dry_run else "reversed"
    typer.echo(f"{verb} {len(qids)} auto-applied row(s): {', '.join(str(q) for q in qids) or '-'}")


@review_app.command("apply")
def review_apply_suggestion(
    queue_id: int | None = typer.Argument(None, help="queue row whose suggestion to apply"),
    apply_all: bool = typer.Option(False, "--all", help="apply every open suggestion"),
) -> None:
    """Apply the suggested resolution(s) computed by the nightly evidence pass."""
    try:
        if apply_all:
            applied = 0
            for e in review_mod.list_open_review():
                if e.detail.get("suggested_resolution"):
                    retired = review_mod.apply_suggestion(e.queue_id)
                    typer.echo(f"[{e.queue_id}] applied: item {retired} superseded")
                    applied += 1
            typer.echo(f"applied {applied} suggestion(s)")
        elif queue_id is not None:
            retired = review_mod.apply_suggestion(queue_id)
            typer.echo(f"applied: item {retired} superseded")
        else:
            _fail(KBError("pass a queue id or --all"))
    except KBError as e:
        _fail(e)


@review_app.command("restore")
def review_restore_cmd(
    retired_id: int = typer.Argument(..., help="item an evidence resolution retired"),
    swap: bool = typer.Option(
        False, "--swap", help="also supersede the kept item with the restored one"
    ),
    apply: bool = typer.Option(False, "--apply", help="write (default: dry run, writes nothing)"),
    note: str | None = typer.Option(None, "--note", help="why; recorded in the item's meta"),
) -> None:
    """Undo an evidence auto-resolution: reactivate the retired item (owner only).

    Default re-opens the pair for owner review with a veto so the nightly cannot
    re-retire it; --swap instead supersedes the previously kept item with the
    restored one. Dry run unless --apply. items.body is never touched.
    """

    def _run() -> review_restore.RestorePlan:
        return review_restore.restore_auto_resolved(retired_id, swap=swap, apply=apply, note=note)

    try:
        # --apply runs under the consolidation lock (refused while a nightly is in
        # flight): the evidence pass batches from a snapshot of hold state, so a
        # restore interleaved with it could be re-retired from a cached streak.
        plan = _locked_apply(_run) if apply else _run()
    except KBError as e:
        _fail(e)
    verb = "restored" if plan.applied else "would restore"
    typer.echo(
        f"{verb} item {plan.retired_id} (was superseded by {plan.kept_id}, "
        f"{plan.kept_status}; reason {plan.supersede_reason!r})"
    )
    done = plan.applied
    if plan.reopen_window:
        typer.echo(f"  validity window {'reopened' if done else 'would reopen'} (valid_to cleared)")
    row = f"queue {plan.queue_id}" if plan.queue_id is not None else "a new queue row"
    if swap:
        act = "superseded by" if done else "would be superseded by"
        typer.echo(f"  swap: item {plan.kept_id} {act} {plan.retired_id}; {row} closed")
    else:
        act = "re-opened" if done else "would re-open"
        typer.echo(f"  pair {act} for owner review: {row} (nightly auto-resolve vetoed)")
    if plan.twin_rows:
        typer.echo(f"  + {plan.twin_rows} other open row(s) for the same pair, same treatment")
    if not plan.applied:
        typer.echo("dry run: nothing written (pass --apply)")

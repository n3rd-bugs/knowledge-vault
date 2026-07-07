"""kbcli.review_cmds — the `kb review` sub-app (PLAN.md §7.4).

Non-interactive so it is scriptable and testable: list open queue entries, then
promote / reject / supersede one at a time. All writes go through kbcore.review;
domain errors become clean exit messages.
"""

from __future__ import annotations

import typer

from kbcore import render
from kbcore import review as review_mod
from kbcore.errors import KBError

review_app = typer.Typer(help="Review queue: promote/reject/supersede unverified items")


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


@review_app.command("apply")
def review_apply(
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

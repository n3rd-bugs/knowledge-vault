"""kbcli.bulk_cmds — `kb review bulk`: batch-drain + the --suggested-rejects path.

Moved out of :mod:`kbcli.review_cmds` (that file sits against the 300-line
ratchet — the promote_cmds/expire_cmds seam) when the ``--suggested-rejects``
mode landed (review #5 strat-8): the pending reject backlog (2,153 rows with no
action surface) gets an owner-speed path that DEFAULTS TO A DRY RUN, prints
exactly what it would archive, and asks for confirmation before writing — the
promote_cmds pattern. The legacy selector mode (``--action`` + ``--ids`` /
``--project``) is byte-identical to what review_cmds shipped: it writes
immediately, as existing scripts rely on. Thin shell over kbcore (Invariant 2):
selection is :func:`kbcore.review_rejects.list_suggested_rejects`, every write
rides :func:`kbcore.review_auto.bulk_resolve` -> ``review.reject`` per row.
"""

from __future__ import annotations

import typer

from kbcore import review_auto, review_rejects
from kbcore.errors import KBError

#: Rows shown in full in the dry-run/apply preview; the rest are summarized.
PREVIEW_ROWS = 20


def _fail(err: Exception) -> None:
    typer.secho(f"error: {err}", fg="red", err=True)
    raise typer.Exit(code=1)


def review_bulk(
    action: str | None = typer.Option(None, "--action", help="promote | reject | dismiss"),
    ids: str = typer.Option("", "--ids", help='comma-separated queue ids, e.g. "1,2,3"'),
    project: str | None = typer.Option(
        None, "--project", help="drain this project's open rows / filter the backlog"
    ),
    suggested_rejects: bool = typer.Option(
        False,
        "--suggested-rejects",
        help="archive the items the LLM triage suggested rejecting (dry run by default)",
    ),
    limit: int = typer.Option(
        0, "--limit", help="bound the --suggested-rejects selection (0 = all)"
    ),
    apply: bool = typer.Option(
        False, "--apply", help="--suggested-rejects only: write after confirming"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="--suggested-rejects only: explicit dry run (the default)"
    ),
) -> None:
    """Batch-drain open review rows (owner tool).

    Two modes. ``--action`` + ``--ids``/``--project`` applies one action
    immediately (WP-1 behavior, unchanged). ``--suggested-rejects`` selects the
    open rows whose stored triage verdict is reject_* and ARCHIVES their items
    through the same per-row owner primitives — dry run unless ``--apply``, and
    ``--apply`` asks for confirmation first (Invariant-5 status flips only).
    """
    if suggested_rejects:
        if action is not None or ids:
            _fail(KBError("--suggested-rejects picks its own rows; drop --action/--ids"))
        _suggested_rejects(project, limit, write=apply and not dry_run)
        return
    if apply or dry_run:
        _fail(KBError("--apply/--dry-run apply to --suggested-rejects mode only"))
    if action is None:
        _fail(KBError("pass --action promote|reject|dismiss, or --suggested-rejects"))
    try:
        queue_ids = [int(x) for x in ids.split(",") if x.strip()] or None
    except ValueError:
        _fail(KBError(f"--ids must be comma-separated integers, got {ids!r}"))
    try:
        results = review_auto.bulk_resolve(action, queue_ids=queue_ids, project=project)
    except KBError as e:
        _fail(e)
    for qid, outcome in results:
        typer.echo(f"[{qid}] {outcome}")
    errs = sum(1 for _, o in results if o.startswith("error"))
    typer.echo(
        f"bulk {action}: {len(results) - errs} resolved, {errs} error(s), {len(results)} row(s)"
    )


def _suggested_rejects(project: str | None, limit: int, *, write: bool) -> None:
    """Preview (always) then optionally apply the pending reject suggestions."""
    try:
        rows = review_rejects.list_suggested_rejects(project=project, limit=limit or None)
    except KBError as e:
        _fail(e)
    if not rows:
        typer.echo("no pending reject suggestions")
        return
    for r in rows[:PREVIEW_ROWS]:
        typer.echo(
            f"[{r['queue_id']}] item {r['item_id']} {r['project']} {r['action']}: {r['title']}"
        )
    if len(rows) > PREVIEW_ROWS:
        typer.echo(f"... and {len(rows) - PREVIEW_ROWS} more row(s)")
    if not write:
        typer.echo(f"dry run: {len(rows)} item(s) would be archived - re-run with --apply to write")
        return
    typer.confirm(f"archive {len(rows)} item(s) (status flip, reversible)?", abort=True)
    try:
        results = review_auto.bulk_resolve("reject", queue_ids=[r["queue_id"] for r in rows])
    except KBError as e:
        _fail(e)
    errs = [(qid, o) for qid, o in results if o.startswith("error")]
    for qid, outcome in errs:
        typer.echo(f"[{qid}] {outcome}")
    typer.echo(
        f"bulk reject: {len(results) - len(errs)} archived, {len(errs)} error(s), "
        f"{len(results)} row(s)"
    )

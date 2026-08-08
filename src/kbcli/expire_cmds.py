"""kbcli.expire_cmds — the two owner-facing quiet-expiry commands.

They belong to the ``kb review`` sub-app (registered by
:mod:`kbcli.review_cmds`), and live in their own module only because that file
sits against the 300-line ratchet. Shape follows :mod:`kbcli.repair_cmds`: the
mutating path DEFAULTS TO A DRY RUN, prints what it would touch, then asks for
confirmation before writing — a sweep whose first real firing faced ~6,600 rows
should be impossible to run blind. Thin shells over
:mod:`kbcore.review_expire`; no SQL here (Invariant 2).
"""

from __future__ import annotations

from datetime import datetime

import typer

from kbcore import review_expire
from kbcore.config import settings
from kbcore.errors import KBError


def _fail(err: Exception) -> None:
    typer.secho(f"error: {err}", fg="red", err=True)
    raise typer.Exit(code=1)


def _ids(qids: list[int]) -> str:
    return ", ".join(str(q) for q in qids) or "-"


def review_expire_cmd(
    days: int | None = typer.Option(
        None, "--days", help="age floor (default: KB_REVIEW_EXPIRE_DAYS)"
    ),
    cap: int | None = typer.Option(
        None, "--cap", help="max rows this run (default: KB_REVIEW_EXPIRE_CAP; 0 = uncapped)"
    ),
    apply: bool = typer.Option(False, "--apply", help="close the rows (default: dry run)"),
) -> None:
    """Close aged, never-recalled, TRIAGED review rows - DRY RUN unless --apply.

    The same sweep the nightly runs. Only the ROW closes: the item stays active,
    unverified and searchable. An untriaged row is never touched (it is queue
    depth, not noise), nor is one carrying a reject_* suggestion. Reversible with
    ``kb review undo-expiry``.
    """
    older = settings.kb_review_expire_days if days is None else days
    try:
        preview = review_expire.expire_quiet_rows(older, dry_run=True, cap=cap)
    except KBError as e:
        _fail(e)
    typer.echo(
        f"eligible: {preview['eligible']}, would expire now: {preview['expired']}, "
        f"left for the next run: {preview['eligible_remaining']}"
    )
    for row in preview["sample"]:
        typer.echo(f"  [{row['id']}] item {row['item_id']}  {row['age_days']}d  {row['project']}")
    if not apply:
        typer.echo("dry run: nothing written - re-run with --apply to close these rows")
        return
    if not preview["expired"]:
        typer.echo("nothing to expire")
        return
    typer.confirm(f"close {preview['expired']} review row(s)?", abort=True)
    try:
        result = review_expire.expire_quiet_rows(older, cap=cap)
    except KBError as e:
        _fail(e)
    typer.echo(f"expired {result['expired']} row(s); undo: kb review undo-expiry --since <ISO>")


def review_undo_expiry(
    since: str = typer.Option(..., "--since", help="ISO timestamp, e.g. 2026-08-19T00:00:00+00:00"),
    dry_run: bool = typer.Option(False, "--dry-run", help="report only; write nothing"),
) -> None:
    """Reopen rows quiet-expired at/after --since (owner escape hatch).

    Items are untouched - quiet expiry never retired one. Each reopened row is
    permanently vetoed against re-expiry, so tonight's sweep cannot undo the undo.
    """
    try:
        when = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        _fail(KBError(f"--since must be an ISO timestamp, got {since!r}"))
    if when.tzinfo is None:
        _fail(KBError("--since must carry a timezone offset (UTC), e.g. ...T00:00:00+00:00"))
    try:
        pending = review_expire.undo_quiet_expiry(when, dry_run=True)
        if dry_run:
            typer.echo(f"would reopen {len(pending)} quiet-expired row(s): {_ids(pending)}")
            return
        if not pending:
            typer.echo("nothing to reopen")
            return
        typer.confirm(f"reopen {len(pending)} quiet-expired row(s)?", abort=True)
        qids = review_expire.undo_quiet_expiry(when)
    except KBError as e:
        _fail(e)
    typer.echo(f"reopened {len(qids)} quiet-expired row(s): {_ids(qids)}")

"""kbcli.promote_cmds — the owner-facing PROMOTE PROGRAM commands.

They belong to the ``kb review`` sub-app (registered by
:mod:`kbcli.review_cmds`) and live in their own module because that file sits
against the 300-line ratchet — the same seam as :mod:`kbcli.expire_cmds`, and
the same shape: the mutating path DEFAULTS TO A DRY RUN, prints exactly what it
would touch, then asks for confirmation before writing. Thin shells over
:mod:`kbcore.review_promote`; no SQL here (Invariant 2), and every trust flip
happens inside ``kbcore.review.promote`` (V4 — the human stays the decider).
"""

from __future__ import annotations

import typer

from kbcore import review_promote
from kbcore.config import settings
from kbcore.errors import KBError


def _fail(err: Exception) -> None:
    typer.secho(f"error: {err}", fg="red", err=True)
    raise typer.Exit(code=1)


def _table(rows: list[dict]) -> list[str]:
    lines = [
        f"{'rank':>4}  {'id':>7}  {'project':<18}  {'type':<8}  {'recalls':>7}  {'age':>5}  title"
    ]
    for rank, r in enumerate(rows, start=1):
        lines.append(
            f"{rank:>4}  {r['item_id']:>7}  {r['project']:<18.18}  {r['type']:<8.8}  "
            f"{r['recalls']:>7}  {r['age_days']:>4}d  {r['title']}"
        )
    return lines


def review_promote_candidates(
    project: str | None = typer.Option(None, "--project", help="only this project"),
    type_: str | None = typer.Option(None, "--type", help="only this item type"),
    min_recalls: int | None = typer.Option(
        None, "--min-recalls", help="recall floor (default: KB_PROMOTE_CANDIDATE_MIN_RECALLS)"
    ),
    limit: int = typer.Option(20, "--limit", help="max rows shown"),
    offset: int = typer.Option(0, "--offset", help="pagination offset (next page)"),
) -> None:
    """Rank the unverified items most WORTH promoting - READ-ONLY, writes nothing.

    Active, unverified, recall-proven items, most-recalled first then oldest;
    summaries/reports (consolidation output) never appear. Promote from here
    with ``kb review promote-batch``.
    """
    try:
        rows = review_promote.list_promote_candidates(
            min_recalls, project=project, item_type=type_, limit=limit, offset=offset
        )
    except KBError as e:
        _fail(e)
    floor = settings.kb_promote_candidate_min_recalls if min_recalls is None else min_recalls
    if not rows:
        typer.echo(f"no promote candidates (recall floor {floor})")
        return
    for line in _table(rows):
        typer.echo(line)
    typer.echo(f"{len(rows)} candidate(s); next: kb review promote-batch [--ids ...] --apply")


def review_promote_batch(
    project: str | None = typer.Option(None, "--project", help="only this project"),
    type_: str | None = typer.Option(None, "--type", help="only this item type"),
    min_recalls: int | None = typer.Option(
        None, "--min-recalls", help="recall floor (default: KB_PROMOTE_CANDIDATE_MIN_RECALLS)"
    ),
    limit: int = typer.Option(20, "--limit", help="max candidates this batch"),
    ids: str = typer.Option(
        "", "--ids", help='explicit ITEM ids, e.g. "1,2,3" (skips candidate enumeration)'
    ),
    apply: bool = typer.Option(
        False, "--apply", help="promote after confirming (default: dry run)"
    ),
) -> None:
    """Batch-promote recall-proven unverified items - DRY RUN unless --apply.

    Selection is the candidate surface under the same filters as
    ``promote-candidates`` (or exactly ``--ids`` when given). Every promotion
    goes through the singular owner chokepoint and closes the item's open
    review row exactly as ``kb review promote <qid>`` does today.
    """
    try:
        explicit = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        _fail(KBError(f"--ids must be comma-separated integers, got {ids!r}"))
    try:
        if explicit:
            item_ids = explicit
        else:
            item_ids = [
                r["item_id"]
                for r in review_promote.list_promote_candidates(
                    min_recalls, project=project, item_type=type_, limit=limit
                )
            ]
        preview = review_promote.promote_batch(item_ids, dry_run=True)
    except KBError as e:
        _fail(e)
    for iid, outcome in preview:
        typer.echo(f"[{iid}] {outcome}")
    would = sum(1 for _, o in preview if o.startswith("would promote"))
    if not apply:
        typer.echo(f"dry run: {would} item(s) would promote - re-run with --apply to write")
        return
    if not would:
        typer.echo("nothing to promote")
        return
    typer.confirm(f"promote {would} item(s) to verified?", abort=True)
    try:
        results = review_promote.promote_batch(item_ids)
    except KBError as e:
        _fail(e)
    for iid, outcome in results:
        typer.echo(f"[{iid}] {outcome}")
    promoted = sum(1 for _, o in results if o.startswith("promoted"))
    errs = sum(1 for _, o in results if o.startswith("error"))
    typer.echo(f"promote-batch: {promoted} promoted, {errs} error(s), {len(results)} item(s)")


def review_promote_run(
    run_key: str = typer.Argument(..., help="the run's sources.uri (kind='session')"),
    apply: bool = typer.Option(
        False, "--apply", help="promote after confirming (default: dry run)"
    ),
) -> None:
    """Promote every unverified item of ONE session run - DRY RUN unless --apply.

    The run key is the ``sources.uri`` all of a session run's extraction items
    share (e.g. ``session:<id>`` from the flush hook). The dry run prints each
    item's id, TYPE and TITLE so the owner vets content, not a count; every
    promotion then goes through the singular owner chokepoint (V4), exactly as
    ``kb review promote-batch`` does.
    """
    try:
        preview = review_promote.promote_run(run_key)
    except KBError as e:
        _fail(e)
    if not preview:
        typer.echo(f"no active unverified items for run {run_key!r}")
        return
    for r in preview:
        typer.echo(f"[{r['item_id']}] {r['type']:<8.8}  {r['title']} -- {r['outcome']}")
    would = sum(1 for r in preview if r["outcome"].startswith("would promote"))
    if not apply:
        typer.echo(f"dry run: {would} item(s) would promote - re-run with --apply to write")
        return
    if not would:
        typer.echo("nothing to promote")
        return
    typer.confirm(f"promote {would} item(s) from this run to verified?", abort=True)
    try:
        results = review_promote.promote_run(run_key, dry_run=False)
    except KBError as e:
        _fail(e)
    for r in results:
        typer.echo(f"[{r['item_id']}] {r['outcome']}")
    promoted = sum(1 for r in results if r["outcome"].startswith("promoted"))
    errs = sum(1 for r in results if r["outcome"].startswith("error"))
    typer.echo(f"promote-run: {promoted} promoted, {errs} error(s), {len(results)} item(s)")

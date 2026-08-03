"""kbcli.repair_cmds — `kb repair twins`, the owner-fired cross-project twin repair.

Thin shell over :mod:`kbcore.twin_repair` (Invariant 2 — no SQL here). Default is
a DRY RUN that prints a table of what WOULD happen and writes nothing; ``--apply``
asks for an interactive y/N confirmation, takes the consolidation advisory lock so
the nightly dedupe cannot interleave, retires the gateway copies, and prints the
undo/audit trail.
"""

from __future__ import annotations

import typer

from kbcore import twin_repair
from kbcore.errors import KBError

repair_app = typer.Typer(
    help="Owner-fired one-time repairs (never automated)", no_args_is_help=True
)

TITLE_WIDTH = 46
_HEADER = (
    f"{'dist':>7}  {'retire':>8}  {'keep':>8}  {'created':10}  "
    f"{'redactions':12}  {'decision':22}  title"
)


def _fail(err: Exception) -> None:
    typer.secho(f"error: {err}", fg="red", err=True)
    raise typer.Exit(code=1)


def _clip(text: str, width: int = TITLE_WIDTH) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _decision(outcome: twin_repair.RepairOutcome, *, applied: bool) -> str:
    if outcome.retired:
        return "RETIRED"
    if outcome.eligible:
        return "would retire" if not applied else "RETIRE FAILED"
    return f"skip: {outcome.reason}"


def _table(outcomes: list[twin_repair.RepairOutcome], *, applied: bool) -> str:
    lines = [_HEADER, "-" * len(_HEADER)]
    for o in outcomes:
        p = o.pair
        lines.append(
            f"{p.distance:>7.4f}  {'#' + str(p.retire.item_id):>8}  "
            f"{'#' + str(p.keep.item_id):>8}  {p.retire.created_at.date().isoformat():10}  "
            f"{_clip(p.retire.fingerprint(), 12):12}  {_decision(o, applied=applied):22}  "
            f"{_clip(p.retire.title)}"
        )
    return "\n".join(lines)


def _tally(counts: dict[str, int]) -> str:
    skips = {k: v for k, v in counts.items() if k not in ("pairs", "eligible", "retired")}
    tail = "; " + ", ".join(f"{k}={v}" for k, v in sorted(skips.items())) if skips else ""
    return (
        f"{counts['pairs']} candidate pair(s), {counts['eligible']} eligible, "
        f"{counts['retired']} retired{tail}"
    )


_UNDO = """
undo / audit trail
  Nothing was deleted (Invariant 5). Each retired copy keeps its body, tags and
  relations; only `status` flipped active -> duplicate, and a `derived_from` edge
  to the canonical was recorded — the same trail the nightly dedupe leaves.
    inspect a retired copy   kb get <retire-id>
    walk the pair            kb trace <keep-id>
    re-check what is left    kb repair twins
  A retired copy is out of search and out of the contradiction band. Restoring one
  is a deliberate owner action on that row; it is intentionally NOT automated, and
  re-running this command never un-retires anything.
""".strip()


@repair_app.command("twins")
def repair_twins(
    max_dist: float = typer.Option(
        twin_repair.DEFAULT_MAX_DIST, "--max-dist", help="lead-chunk cosine distance ceiling"
    ),
    gateway: str = typer.Option(
        twin_repair.GATEWAY_PROJECT, "--gateway", help="project whose copies get retired"
    ),
    original: str = typer.Option(
        twin_repair.ORIGINAL_PROJECT, "--original", help="project that stays canonical"
    ),
    limit: int | None = typer.Option(None, "--limit", help="only the closest N pairs"),
    apply: bool = typer.Option(
        False, "--apply", help="perform the retirements (default: dry run, writes nothing)"
    ),
) -> None:
    """Retire GATEWAY copies of data-server originals left by the double-capture era.

    Read-only by default. Refuses to retire a copy that is verified, was read in
    the last 30 days, or is itself the canonical of other duplicates.
    """
    from kbcore.db import advisory_lock
    from kbworker.consolidate import ADVISORY_KEY  # single source of the dedupe key

    try:
        pairs = twin_repair.enumerate_twins(gateway, original, max_dist)
        if limit is not None:
            pairs = pairs[: max(0, limit)]
        outcomes = twin_repair.repair(pairs, dry_run=True)
    except KBError as e:
        _fail(e)
    if not pairs:
        typer.echo(f"no {gateway}/{original} twins within distance {max_dist}")
        return

    typer.echo(_table(outcomes, applied=False))
    typer.echo("")
    typer.echo(_tally(twin_repair.summarize(outcomes)))
    eligible = [o.pair for o in outcomes if o.eligible]
    if not apply:
        typer.echo("dry run — nothing written. Re-run with --apply to retire the copies above.")
        return
    if not eligible:
        typer.echo("nothing eligible to retire — every candidate was refused by a guard.")
        return

    typer.confirm(
        f"Retire {len(eligible)} {gateway} copy/copies as duplicates of {original}?",
        abort=True,
    )
    # The nightly dedupe is the only other writer that mints duplicate chains;
    # holding its lock makes the guard->write window unreachable by it.
    with advisory_lock(ADVISORY_KEY) as acquired:
        if not acquired:
            _fail(
                KBError(
                    f"consolidation lock {ADVISORY_KEY} is held (a consolidation run is in "
                    "flight) — nothing written; retry when it finishes"
                )
            )
        try:
            applied_outcomes = twin_repair.repair(eligible, dry_run=False)
        except KBError as e:
            _fail(e)
    typer.echo("")
    typer.echo(_table(applied_outcomes, applied=True))
    typer.echo("")
    typer.echo(_tally(twin_repair.summarize(applied_outcomes)))
    typer.echo("")
    typer.echo(_UNDO)

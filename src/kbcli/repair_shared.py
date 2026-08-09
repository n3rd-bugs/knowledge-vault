"""kbcli.repair shared machinery — the Typer app and the dry-run→confirm→apply flow.

Extracted from :mod:`kbcli.repair_cmds` (2026-08-09) when a fourth owner-repair
command (``cross-twins``) pushed that file past the 300-line ratchet: the repo's
answer to a file that must grow is to split, not to bump the baseline. The
command groups now live beside this module — ``twins``/``summaries``/
``judge-flagged`` in :mod:`kbcli.repair_cmds`, ``cross-twins`` in
:mod:`kbcli.repair_crosstwins` — and each registers its commands onto the one
``repair_app`` defined here. No SQL anywhere (Invariant 2).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import typer

from kbcore.errors import KBError

repair_app = typer.Typer(
    help="Owner-fired one-time repairs (never automated)", no_args_is_help=True
)

TITLE_WIDTH = 46


def _fail(err: Exception) -> None:
    typer.secho(f"error: {err}", fg="red", err=True)
    raise typer.Exit(code=1)


def _clip(text: str, width: int = TITLE_WIDTH) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _guarded(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except KBError as e:
        _fail(e)


def _locked_apply(fn: Callable[[], Any]) -> Any:
    """Run ``fn`` while holding the consolidation advisory lock — ``twins``/``summaries``
    mint duplicate/supersede chains the nightly dedupe also mints, so its lock closes the
    guard->write window. (``judge-flagged`` only stamps ``detail`` and skips this.)"""
    from kbcore.db import advisory_lock
    from kbworker.consolidate import ADVISORY_KEY  # single source of the dedupe key

    with advisory_lock(ADVISORY_KEY) as acquired:
        if not acquired:
            _fail(
                KBError(
                    f"consolidation lock {ADVISORY_KEY} is held (a consolidation run is in "
                    "flight) — nothing written; retry when it finishes"
                )
            )
        return _guarded(fn)


def _flow(
    *,
    apply: bool,
    dry_fn: Callable[[], list],
    apply_fn: Callable[[list], list],
    render: Callable[[list, bool], str],
    tally: Callable[[list], str],
    eligible_pred: Callable[[Any], bool],
    empty: str,
    dry_hint: str,
    none_hint: str,
    prompt: Callable[[int], str],
    undo: str,
    hold_lock: bool = False,
) -> None:
    """Shared dry-run-table -> confirm -> apply -> undo flow for every repair command."""
    outcomes = _guarded(dry_fn)
    if not outcomes:
        typer.echo(empty)
        return
    typer.echo(render(outcomes, False))
    typer.echo("")
    typer.echo(tally(outcomes))
    eligible = [o for o in outcomes if eligible_pred(o)]
    if not apply:
        typer.echo(dry_hint)
        return
    if not eligible:
        typer.echo(none_hint)
        return
    typer.confirm(prompt(len(eligible)), abort=True)
    run = _locked_apply if hold_lock else _guarded
    applied = run(lambda: apply_fn(eligible))
    typer.echo("")
    typer.echo(render(applied, True))
    typer.echo("")
    typer.echo(tally(applied))
    typer.echo("")
    typer.echo(undo)

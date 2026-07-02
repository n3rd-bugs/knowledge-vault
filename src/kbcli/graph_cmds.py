"""kbcli.graph_cmds — Phase 5 CLI commands: trace, similar, stats, backfill-entities.

Registered as top-level `kb` commands from kbcli.main. Thin shells over kbcore.
"""

from __future__ import annotations

import typer

from kbcore import entities, graph, render, stats
from kbcore.errors import KBError


def _fail(err: Exception) -> None:
    typer.secho(f"error: {err}", fg="red", err=True)
    raise typer.Exit(code=1)


def stats_cmd() -> None:
    """Corpus stats: counts by type/project/trust/status, entities, relations."""
    typer.echo(render.render_stats(stats.stats()))


def trace_cmd(
    node_id: int = typer.Argument(..., help="item (or entity) id to trace from"),
    node_kind: str = typer.Option("item", "--kind", help="item | entity"),
    depth: int = typer.Option(2, "--depth"),
) -> None:
    """Trace the relation graph (supersession, fixes, derived_from, mentions)."""
    try:
        tree = graph.trace(node_kind, node_id, depth=depth)
    except KBError as e:
        _fail(e)
    typer.echo(render.render_trace(tree))


def similar_cmd(
    item_id: int = typer.Argument(...),
    k: int = typer.Option(5, "-k"),
) -> None:
    """Nearest active items by meaning — "have we hit this before?"."""
    try:
        hits = graph.similar(item_id, k=k)
    except KBError as e:
        _fail(e)
    typer.echo(render.render_similar(hits))


def backfill_cmd() -> None:
    """Populate entities/mentions for existing items from their tags + code_ref."""
    processed = entities.backfill_entities()
    typer.echo(f"backfill-entities: processed {processed} active items")

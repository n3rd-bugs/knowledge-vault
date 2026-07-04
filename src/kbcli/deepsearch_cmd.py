"""kbcli.deepsearch_cmd — `kb deep-search` (explicit user-invoked GPU run)."""

from __future__ import annotations

import typer

from kbcore import render
from kbcore.deepsearch import deep_search
from kbcore.errors import KBError


def deep_search_cmd(
    question: str = typer.Argument(..., help="the architectural question to dig into"),
    project: str | None = typer.Option(None, "--project", "-p", help="bias + save target"),
    k_per_query: int = typer.Option(8, "-k", "--k-per-query", help="hits per sub-query"),
    max_items: int | None = typer.Option(None, "--max-items", help="synthesis corpus cap"),
    save: bool = typer.Option(True, "--save/--no-save", help="persist the report item"),
) -> None:
    """Multi-query deep search + grounded synthesis on the LOCAL model (slow: minutes)."""
    try:
        result = deep_search(
            question, project=project, k_per_query=k_per_query, max_items=max_items, save=save
        )
    except KBError as e:
        typer.secho(f"error: {e}", fg="red", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(render.render_deep_search(result))

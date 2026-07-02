"""kbcli.main — the `kb` command-line entrypoint.

Phase 0 exposes `migrate` and `version`; ingest/search/adr/link/projects arrive
in Phase 1 (PLAN.md §11).
"""

from __future__ import annotations

import typer

from kbcore import migrate as _migrate

app = typer.Typer(help="Knowledge Vault CLI", no_args_is_help=True)


@app.command()
def migrate() -> None:
    """Apply pending database migrations."""
    _migrate.migrate()


@app.command()
def version() -> None:
    """Print the Knowledge Vault version."""
    typer.echo("knowledge-vault 0.1.0")


if __name__ == "__main__":
    app()

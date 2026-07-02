"""kbcli.main — the `kb` command-line entrypoint (PLAN.md §11 Phase 1).

Thin shell over kbcore: parse args, call the service function, render. All
writes happen inside kbcore transactions; domain errors become clean exit
messages (CLAUDE.md §Code style).
"""

from __future__ import annotations

import json
import sys

import typer

from kbcli import graph_cmds, render, review_cmds, seeds
from kbcore import graph, items, search
from kbcore import ingest as ingest_mod
from kbcore import migrate as migrate_mod
from kbcore import reembed as reembed_mod
from kbcore import supersede as supersede_mod
from kbcore.errors import KBError

app = typer.Typer(help="Knowledge Vault CLI", no_args_is_help=True)
project_app = typer.Typer(help="Project registry commands")
app.add_typer(project_app, name="project")
app.add_typer(review_cmds.review_app, name="review")
# Phase 5 top-level commands (bodies in kbcli.graph_cmds to keep this file small).
app.command("stats")(graph_cmds.stats_cmd)
app.command("trace")(graph_cmds.trace_cmd)
app.command("similar")(graph_cmds.similar_cmd)
app.command("backfill-entities")(graph_cmds.backfill_cmd)

TAGS_HELP = "Namespaced tags, repeatable: lang:c hw:tps55288 protocol:spi tool:systemd …"


def _fail(err: Exception) -> None:
    typer.secho(f"error: {err}", fg="red", err=True)
    raise typer.Exit(code=1)


def _read_body(body: str | None, file: typer.FileText | None) -> str:
    if body is not None:
        return body
    if file is not None:
        return file.read()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    _fail(KBError("provide a body via --body, --file, or stdin"))
    raise AssertionError  # unreachable


def _parse_code_ref(code_ref: str | None) -> dict | None:
    if not code_ref:
        return None
    try:
        return json.loads(code_ref)
    except json.JSONDecodeError as e:
        _fail(
            KBError(
                f'--code-ref must be JSON like {{"repo":"x","path":"src/a.c","lines":"1-9"}}: {e}'
            )
        )
        raise AssertionError from None


@app.command()
def add(
    project: str = typer.Option(..., "--project", "-p"),
    type: str = typer.Option("note", "--type", "-t"),
    title: str = typer.Option(..., "--title"),
    body: str | None = typer.Option(None, "--body"),
    file: typer.FileText | None = typer.Option(None, "--file"),
    tags: list[str] = typer.Option([], "--tag", help=TAGS_HELP),
    code_ref: str | None = typer.Option(None, "--code-ref", help="JSON code anchor"),
) -> None:
    """Save a verified item (manual knowledge entry)."""
    try:
        item_id = ingest_mod.ingest(
            project,
            type,
            title,
            _read_body(body, file),
            kind="manual",
            tags=list(tags),
            code_ref=_parse_code_ref(code_ref),
        )
    except (KBError, ValueError) as e:
        _fail(e)
    if item_id is None:
        typer.echo("duplicate content — nothing ingested")
    else:
        typer.echo(f"saved item {item_id}")


@app.command()
def adr(
    project: str = typer.Option(..., "--project", "-p"),
    title: str = typer.Option(..., "--title"),
    context: str = typer.Option("", "--context"),
    decision: str = typer.Option("", "--decision"),
    alternatives: str = typer.Option("", "--alternatives"),
    consequences: str = typer.Option("", "--consequences"),
    tags: list[str] = typer.Option([], "--tag", help=TAGS_HELP),
    code_ref: str | None = typer.Option(None, "--code-ref"),
) -> None:
    """Save a decision with the soft ADR template (PRAG-4 — sections may stay empty)."""
    body = (
        f"## Context\n{context}\n\n## Decision\n{decision}\n\n"
        f"## Alternatives\n{alternatives}\n\n## Consequences\n{consequences}"
    )
    try:
        item_id = ingest_mod.ingest(
            project,
            "decision",
            title,
            body,
            kind="manual",
            tags=list(tags),
            code_ref=_parse_code_ref(code_ref),
        )
    except (KBError, ValueError) as e:
        _fail(e)
    typer.echo(
        "duplicate content — nothing ingested" if item_id is None else f"saved decision {item_id}"
    )


@app.command("search")
def search_cmd(
    query: str = typer.Argument(...),
    project: str | None = typer.Option(None, "--project", "-p"),
    domain: str | None = typer.Option(None, "--domain"),
    type: str | None = typer.Option(None, "--type", "-t"),
    tags: list[str] = typer.Option([], "--tags", help="Filter: item must carry all given tags"),
    boost_project: str | None = typer.Option(None, "--boost-project"),
    include_superseded: bool = typer.Option(False, "--include-superseded"),
    include_archived: bool = typer.Option(False, "--include-archived"),
    k: int = typer.Option(8, "-k"),
) -> None:
    """Hybrid search across all projects (default) — cross-domain recall is the point."""
    try:
        hits = search.search(
            query,
            project=project,
            domain=domain,
            type=type,
            tags=list(tags) or None,
            boost_project=boost_project,
            include_superseded=include_superseded,
            include_archived=include_archived,
            k=k,
            tool="cli.search",
        )
    except KBError as e:
        _fail(e)
    if not hits:
        typer.echo("no results")
        return
    for i, hit in enumerate(hits, 1):
        typer.echo(render.render_hit(hit, i))
        typer.echo("")


@app.command()
def get(
    item_id: int = typer.Argument(...),
    with_relations: bool = typer.Option(True, "--with-relations/--no-relations"),
) -> None:
    """Show one item in full: body, links, supersession chain."""
    try:
        item = items.get_item(item_id, with_relations=with_relations)
    except KBError as e:
        _fail(e)
    typer.echo(render.render_item(item))


@app.command("supersede")
def supersede_cmd(
    old_id: int = typer.Argument(...),
    reason: str = typer.Option(..., "--reason"),
    new_body: str | None = typer.Option(None, "--body"),
    new_item_id: int | None = typer.Option(None, "--new-item"),
) -> None:
    """Replace an item without losing history (corrections go here, never edits)."""
    try:
        new_id = supersede_mod.supersede(
            old_id, new_item_id=new_item_id, new_body=new_body, reason=reason
        )
    except KBError as e:
        _fail(e)
    typer.echo(f"item {old_id} superseded by {new_id}")


@app.command("link")
def link_cmd(
    src: int = typer.Argument(..., help="source item id"),
    rel: str = typer.Argument(..., help="fixes | alternative_to | depends_on | …"),
    dst: int = typer.Argument(..., help="destination item id"),
    evidence: int | None = typer.Option(None, "--evidence", help="evidence item id"),
) -> None:
    """Relate two items, e.g. `kb link 12 fixes 7`."""
    try:
        graph.link(src, rel, dst, evidence_item=evidence)
    except KBError as e:
        _fail(e)
    typer.echo(f"linked {src} -{rel}-> {dst}")


@app.command()
def projects() -> None:
    """Project registry: name, domain, item counts, last activity."""
    typer.echo(render.render_projects(items.list_projects()))


@project_app.command("set")
def project_set(
    name: str = typer.Argument(...),
    domain: str | None = typer.Option(None, "--domain"),
    repos: list[str] = typer.Option([], "--repo", help="repeatable local repo path"),
    description: str | None = typer.Option(None, "--description"),
) -> None:
    """Create/update registry metadata for a project (DATA-8)."""
    items.set_project(name, domain=domain, repos=list(repos) or None, description=description)
    typer.echo(f"project {name} updated")


@app.command("ingest-session")
def ingest_session_cmd(
    path: str = typer.Argument(..., help="path to the Claude Code .jsonl transcript"),
    project: str = typer.Option(..., "--project", "-p"),
    repo: str | None = typer.Option(None, "--repo", help="git worktree root (for code_ref)"),
    branch: str | None = typer.Option(None, "--branch"),
    commit: str | None = typer.Option(None, "--commit", help="short commit for code_ref"),
) -> None:
    """Extract unverified knowledge from a session transcript (§7.2, opt-in)."""
    from kbworker.sessions import ingest_session

    try:
        summary = ingest_session(path, project, repo=repo, branch=branch, commit=commit)
    except KBError as e:
        _fail(e)
    typer.echo(
        f"session {summary.path}: {summary.saved} saved (unverified), "
        f"{summary.rejected} rejected, offset {summary.offset}"
    )


@app.command("ingest-repo")
def ingest_repo_cmd(
    path: str = typer.Argument(..., help="path to a git repository"),
) -> None:
    """Ingest git-tracked docs from a repo — never source files (§7.5, Invariant 9)."""
    from kbworker.reposcan import scan_repo

    try:
        summary = scan_repo(path)
    except (KBError, ValueError) as e:
        _fail(e)
    typer.echo(
        f"repo {summary.repo} (project={summary.project}, commit={summary.commit or '-'}): "
        f"{summary.saved} docs ingested, {summary.skipped_dupes} duplicates skipped"
    )
    for uri in summary.ingested:
        typer.echo(f"  + {uri}")


@app.command()
def seed() -> None:
    """Ingest the seed corpus (idempotent — safe to re-run)."""
    new, skipped = seeds.run_seed()
    typer.echo(f"seed: {new} new items, {skipped} duplicates skipped")


@app.command()
def reembed() -> None:
    """Re-embed chunks whose embed_model differs from KB_EMBED_MODEL (DATA-3)."""
    n = reembed_mod.reembed()
    typer.echo(f"re-embedded {n} chunks")


@app.command()
def migrate() -> None:
    """Apply pending database migrations."""
    migrate_mod.migrate()


@app.command()
def version() -> None:
    """Print the Knowledge Vault version."""
    typer.echo("knowledge-vault 0.1.0")


if __name__ == "__main__":
    app()

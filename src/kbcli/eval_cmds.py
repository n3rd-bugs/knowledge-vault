"""kbcli.eval_cmds — the `kb eval` sub-app (retrieval-eval utilities).

`kb eval mine` turns real usage into candidate golden cases: an access_log
kb_search hit that the user then opened with kb_get within a window is evidence
that (query → item) was a good retrieval. Output is YAML case stubs on STDOUT
for owner review — this command NEVER writes eval/golden.yaml.
"""

from __future__ import annotations

import json

import typer

from kbcore import eval_harness

eval_app = typer.Typer(help="Retrieval-eval utilities (golden-set tooling)")


@eval_app.command("mine")
def eval_mine(
    window_minutes: int = typer.Option(
        15, "--window-minutes", "-w", help="max minutes between the search and the kb_get"
    ),
    limit: int = typer.Option(50, "--limit", "-n", help="max candidates to emit"),
) -> None:
    """Mine access_log (search→get confirmations) into golden-case YAML stubs."""
    candidates = eval_harness.mine_candidates(window_minutes=window_minutes, limit=limit)
    if not candidates:
        typer.echo("# no candidates: no kb_search hit was followed by a kb_get in the window")
        return
    typer.echo("# candidate golden cases mined from access_log (kb_search -> kb_get")
    typer.echo(f"# within {window_minutes} min). REVIEW before pasting into eval/golden.yaml —")
    typer.echo("# this command never writes that file.")
    for c in candidates:
        title = " ".join(str(c["title"]).split())  # keep the comment single-line
        typer.echo(
            f"- q: {json.dumps(c['q'], ensure_ascii=False)}"
            f"  # {c['confirmations']} confirmation(s), last {c['last_confirmed']:%Y-%m-%d}"
        )
        typer.echo(f"  expect_item_id: {c['expect_item_id']}  # {title}")

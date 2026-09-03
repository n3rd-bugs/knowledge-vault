"""kbcli.bench_cmds — `kb bench` (adoption benches; read-only, explicit user-invoked GPU use).

`kb bench judge` is the agreement probe behind the pair-judge swap (review #8
H3f/H4): it re-asks the UNMODIFIED pair-judge prompts over labelled pairs for each
candidate ``model x think`` and prints agreement / judged-rate / FP-FN / s-per-pair
through the real request path. Thin shell over :mod:`kbcore.bench_judge` (which
holds the mechanical read-only guarantees). The ONLY write this module can make is
``--archive`` — submit the results JSON to project kb-system via ``put_document``
(extract=false) so the evidence survives the next reboot — and that path imports
its write chokepoints LAZILY inside :func:`_archive`, so this module's import set
stays read-only (``tests/test_bench_judge.py`` pins it by AST). Rule 8: the bench
is an explicit, owner-invoked GPU run, refused while a consolidation is in flight.
"""

from __future__ import annotations

from pathlib import Path

import typer

from kbcore import bench_judge
from kbcore.errors import KBError

bench_app = typer.Typer(
    help="Adoption benches — read-only probes that gate a model-seat swap (rule 8: explicit)"
)


def _fail(err: Exception) -> None:
    typer.secho(f"error: {err}", fg="red", err=True)
    raise typer.Exit(code=1)


@bench_app.command("judge")
def bench_judge_cmd(
    models: str = typer.Option(
        ..., "--models", help="comma-separated candidate models, e.g. granite4.2:8b,qwen3:14b"
    ),
    think: str = typer.Option(
        "both", "--think", help="on|off|both|default (default = key omitted, the incumbent's shape)"
    ),
    pairs: str = typer.Option(
        "edges",
        "--pairs",
        help=(
            "edges (item-item alternative_to edges, label same-fact) | judged "
            "(contradiction_judgements rows, the incumbent's verdict as label) | a JSON "
            "file (a previous results file re-runs its set)"
        ),
    ),
    sample: int = typer.Option(
        30, "--sample", help="judged: rows to draw (positives first, then a seeded random draw)"
    ),
    negatives: int = typer.Option(
        0,
        "--negatives",
        help="edges: unlinked cross-project pairs from the 0.80-0.95 band added as hard negatives",
    ),
    out: Path = typer.Option(
        Path("."), "--out", help="directory for judge-<stamp>.json (default CWD; never /tmp)"
    ),
    archive: bool = typer.Option(
        False,
        "--archive",
        help="also submit the results JSON to kb-system (put_document, extract=false)",
    ),
    decision: int | None = typer.Option(
        None, "--decision", help="with --archive: kb-system decision item id to link derived_from"
    ),
) -> None:
    """Agreement probe for the pair-judge seat: candidates vs the incumbent's stored verdicts.

    READ-ONLY (Postgres read-only connections; no write module imported); refuses to
    start while a consolidation run holds the advisory lock (try-and-release probe,
    never held). Acceptance before a swap: agreement >= 0.95 on >= 30 judged pairs plus
    the alternative_to edges, s/pair <= 1.5 through this real path; paste the summary
    lines in the enabling commit and --archive the JSON.
    """
    model_list = [m.strip() for m in models.split(",") if m.strip()]
    if not model_list:
        _fail(KBError("--models needs at least one model"))
    thinks = bench_judge.THINK_MODES.get(think)
    if thinks is None:
        _fail(KBError("--think must be one of on, off, both, default"))
    assert thinks is not None  # for the type checker; _fail raised above
    try:
        bench_judge.assert_gpu_slot_free()
        pair_list = bench_judge.load_pairs(pairs, sample=sample, negatives=negatives)
        by_kind = {k: sum(1 for p in pair_list if p.kind == k) for k in bench_judge.KINDS}
        positives = sum(1 for p in pair_list if p.label)
        typer.echo(
            f"{len(pair_list)} pair(s): {by_kind[bench_judge.KIND_SAME_FACT]} same-fact, "
            f"{by_kind[bench_judge.KIND_CONTRADICT]} contradict; {positives} labelled true; "
            f"{len(model_list) * len(thinks)} config(s)"
        )
        result = bench_judge.run_bench(
            model_list, thinks, pair_list, on_progress=lambda s: typer.echo(s, err=True)
        )
        path = bench_judge.write_results(result, out)
    except KBError as e:
        _fail(e)
        raise AssertionError from None  # unreachable: _fail raised typer.Exit
    for line in bench_judge.summary_lines(result):
        typer.echo(line)
    typer.echo(f"results: {path}")
    if str(path.resolve()).startswith("/tmp/"):
        typer.echo("note: /tmp does not survive a reboot — copy or --archive the results", err=True)
    if archive:
        _archive(path, decision)


def _archive(path: Path, decision: int | None) -> None:
    """The ONLY write this command can make, behind ``--archive``: the results JSON becomes a
    kb-system document (extract=false — evidence, never mined) and, with ``--decision``,
    is linked ``derived_from`` the seat decision. Imports are LAZY on purpose: the
    module-level import set stays read-only, and the AST test asserts exactly that."""
    from kbcore import documents, graph

    try:
        doc = documents.put_document(
            "kb-system",
            path.name,
            content=path.read_bytes(),
            note="pair-judge agreement probe (kb bench judge) — evidence for the seat decision",
            tags=["tool:ollama", "topic:bench", "topic:pair-judge"],
            extract=False,
        )
        if decision is not None and doc.item_id is not None:
            graph.link(doc.item_id, "derived_from", decision)
    except KBError as e:
        _fail(e)
        raise AssertionError from None  # unreachable: _fail raised typer.Exit
    linked = f", derived_from #{decision}" if decision is not None else ""
    typer.echo(f"archived: {doc.uri} (source {doc.source_id}{linked})")

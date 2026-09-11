"""kbcli.queue_cmds — ``kb queue coverage`` (owner, read-only; wave S item S6).

A thin shell over :func:`kbcore.fleet_coverage.queue_coverage` (Invariant 2 —
no SQL here). Prints, per session, the per-project row/MB/status split and
EITHER a seed candidate (stamped rows only) OR the refusal with its reason;
``sum(octet_length)`` is always labelled post-scrub, NOT a seed (V1-M1).
"""

from __future__ import annotations

import typer

from kbcore import fleet_coverage
from kbcore.errors import KBError

queue_app = typer.Typer(help="Ingest-queue inspection (owner, read-only)", no_args_is_help=True)

_SPLIT_HEADER = (
    f"  {'project':<24} {'rows':>6} {'MB':>7} {'pending':>7} {'extracted':>9} {'failed':>6} "
    f"{'dup':>5}"
)


def _fail(err: Exception) -> None:
    typer.secho(f"error: {err}", fg="red", err=True)
    raise typer.Exit(code=1)


def render_session(s: fleet_coverage.SessionCoverage) -> list[str]:
    """The dry table for one session — pure, so tests pin the exact text."""
    lines = [
        f"session {s.uri}: {s.rows} rows, {s.post_scrub_mb:.1f} MB "
        f"({fleet_coverage.POST_SCRUB_LABEL}); {s.first_at.date().isoformat()} .. "
        f"{s.last_at.date().isoformat()}; boxes: {', '.join(s.boxes) or '-'}; "
        f"stamped {s.stamped_rows}/{s.rows}",
        _SPLIT_HEADER,
    ]
    for p in s.projects:
        lines.append(
            f"  {p.project:<24.24} {p.rows:>6} {p.mb:>7.1f} {p.pending:>7} {p.extracted:>9} "
            f"{p.failed:>6} {p.duplicate:>5}"
        )
    if s.seed_candidate is not None:
        lines.append(
            f"  seed candidate: {s.seed_candidate} (max flush offset+bytes over stamped rows)"
        )
    else:
        lines.append(f"  seed: REFUSED - {s.refusal}")
    return lines


@queue_app.command("coverage")
def coverage(
    session: str | None = typer.Option(
        None, "--session", help="session id PREFIX (the sid8 from the digest is enough)"
    ),
    box: str | None = typer.Option(
        None, "--box", help="submitting principal (meta.submitted_by) — every session it sent"
    ),
    limit: int = typer.Option(
        fleet_coverage.MAX_SESSIONS, "--limit", help="newest N sessions (default 50)"
    ),
) -> None:
    """What the queue holds for a session: per-project rows/MB/status, and the ONLY
    number safe to feed `kb-flush-backfill.sh --seed` (from flush stamps, one project,
    zero redactions) — or the reason none is offered. Read-only; owner only."""
    try:
        report = fleet_coverage.queue_coverage(session_prefix=session, box=box, limit=limit)
    except KBError as e:
        _fail(e)
    typer.echo(f"queue coverage for {report.selector}: {len(report.sessions)} session(s)")
    for s in report.sessions:
        typer.echo("\n".join(render_session(s)))

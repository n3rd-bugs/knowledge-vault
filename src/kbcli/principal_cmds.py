"""kbcli.principal_cmds — owner-local management of grantee credentials.

`kb principal create|grant|revoke|rotate|list` — the ops surface for per-project
sharing (migration 011). Owner-invoked on the host under the owner's ``.env`` (the
CLI defaults to the OWNER scope). All DB work goes through
:mod:`kbcore.principals` service functions (Invariant 2); SECRETS are printed
exactly once, at create/rotate, and NEVER by ``list`` (CLAUDE.md hygiene).
"""

from __future__ import annotations

import typer

from kbcore import principals
from kbcore.errors import KBError
from kbcore.items import parse_instant

principal_app = typer.Typer(
    help="Manage grantee credentials for per-project sharing", no_args_is_help=True
)


def _fail(err: Exception) -> None:
    typer.secho(f"error: {err}", fg="red", err=True)
    raise typer.Exit(code=1)


@principal_app.command("create")
def create(
    name: str = typer.Argument(..., help="handle / audit label, e.g. alice-laptop"),
    email: str | None = typer.Option(
        None, "--email", help="create a Google-identity grantee (claude.ai / web) with this email"
    ),
    token: bool = typer.Option(
        False, "--token", help="create a kbg_ bearer-token grantee (the default when no --email)"
    ),
    expires: str | None = typer.Option(
        None, "--expires", help="ISO date/timestamp after which the credential stops working"
    ),
    note: str | None = typer.Option(None, "--note", help="free-text note"),
    role: str = typer.Option(
        "reader",
        "--role",
        help="capability bundle: guest, reader, contributor, reviewer (default reader)",
    ),
) -> None:
    """Create a grantee. Prints a fresh kbg_ token ONCE for token grantees (only its
    hash is stored — it is never recoverable). --email makes a Google-identity
    grantee instead (no token). --role picks the feature bundle (tiered access);
    project grants still decide which rows are visible."""
    if email and token:
        _fail(KBError("pass --email OR --token, not both"))
    kind = "google" if email else "token"
    try:
        expires_at = parse_instant(expires, "--expires") if expires else None
        _pid, secret = principals.create_principal(
            name, kind=kind, email=email, expires_at=expires_at, note=note, role=role
        )
    except (KBError, ValueError) as e:
        _fail(e)
    if secret is not None:
        typer.echo(f"created token principal {name!r}")
        typer.secho(
            "token (shown ONCE — store it now, it is not recoverable):", fg="yellow", err=True
        )
        typer.echo(secret)
    else:
        typer.echo(f"created google principal {name!r} for {email}")
    typer.echo("grant it projects with: kb principal grant " + name + " <project> [--rw]")


@principal_app.command("grant")
def grant_cmd(
    name: str = typer.Argument(...),
    project: str = typer.Argument(...),
    rw: bool = typer.Option(False, "--rw", help="allow writes to this project (default read-only)"),
) -> None:
    """Grant a grantee read (or --rw read-write) access to a project."""
    try:
        principals.grant(name, project, rw=rw)
    except (KBError, ValueError) as e:
        _fail(e)
    typer.echo(f"granted {name} {'read-write' if rw else 'read-only'} on {project}")


@principal_app.command("ungrant")
def ungrant_cmd(
    name: str = typer.Argument(...),
    project: str = typer.Argument(...),
) -> None:
    """Remove a grantee's access to a project."""
    try:
        principals.ungrant(name, project)
    except (KBError, ValueError) as e:
        _fail(e)
    typer.echo(f"removed {name}'s grant on {project}")


@principal_app.command("role")
def role_cmd(
    name: str = typer.Argument(...),
    role: str = typer.Argument(..., help="one of: guest, reader, contributor, reviewer"),
) -> None:
    """Change a grantee's role (feature bundle — tiered access, plan §2). CLI-only:
    the web admin can only pick guest/reader at CREATION and never change a role.
    Takes effect on the next request (principal cache flushed, parity with grant)."""
    try:
        principals.set_role(name, role)
    except (KBError, ValueError) as e:
        _fail(e)
    typer.echo(f"set {name} role to {role}")


@principal_app.command("revoke")
def revoke_cmd(name: str = typer.Argument(...)) -> None:
    """Revoke a grantee. Its credential stops authenticating on the next request
    (within the token cache TTL for kbg_ bearers, immediately for web/OAuth)."""
    try:
        principals.revoke(name)
    except (KBError, ValueError) as e:
        _fail(e)
    typer.echo(f"revoked {name}")


@principal_app.command("rotate")
def rotate_cmd(name: str = typer.Argument(...)) -> None:
    """Mint a new secret for a token grantee; the old one dies immediately. Grants
    and audit history are preserved."""
    try:
        secret = principals.rotate(name)
    except (KBError, ValueError) as e:
        _fail(e)
    typer.echo(f"rotated {name} — new token (shown ONCE):")
    typer.echo(secret)


@principal_app.command("list")
def list_cmd() -> None:
    """List grantees with their grants — never a secret."""
    rows = principals.list_principals()
    if not rows:
        typer.echo("no grantees (the master token + allowlisted owner emails are the owner)")
        return
    for p in rows:
        status = "active"
        if p.revoked_at is not None:
            status = "REVOKED"
        elif p.expires_at is not None:
            status = f"expires {p.expires_at.date().isoformat()}"
        ident = p.email if p.kind == "google" else "token"
        grants = (
            ", ".join(f"{proj}{'(rw)' if rw else ''}" for proj, rw in p.grants) or "(no grants)"
        )
        typer.echo(f"{p.name}  [{p.kind}:{ident}]  role={p.role}  {status}")
        typer.echo(f"    grants: {grants}")
        if p.note:
            typer.echo(f"    note: {p.note}")

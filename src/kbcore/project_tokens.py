"""kbcore.project_tokens — one revocable credential per (project, machine).

The problem this replaces: a machine capturing sessions held ONE credential for
everything. For the owner that was ``KB_TOKEN`` itself, which cannot be revoked
without rotating every machine, and which grants far more than "write my
sessions into this project".

A project token is the narrow version:

* **Scoped.** A ``kbg_`` principal with the ``contributor`` role and exactly one
  read-write grant — the named project. It can search, browse and save there,
  and it can do nothing anywhere else.
* **Per machine.** Named ``<project>@<machine>``, so revoking one laptop does
  not disturb the same project on another, and the audit log names which
  machine wrote what.
* **Revocable.** ``kb principal revoke <name>`` (a timestamp flip, never a
  delete) stops it at the next request. Nothing else has to change.
* **Self-service, within the caller's own authority.** The requester's scope
  decides: an owner may take a token for any project including one that does
  not exist yet; a grantee only for projects they can already write; an
  identity with no write access gets nothing. A token is therefore never a way
  to gain access, only a way to carry access you already have to a machine.

Expiry is opt-in. A hook credential that expires without warning is a silent
capture outage, so the default is no expiry — the answer to a compromised
machine is revocation, which is immediate and specific.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from kbcore import principals, scope
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation

log = logging.getLogger("kbcore.project_tokens")

# The role a capture credential needs and no more: search + browse + save.
# Deliberately NOT reviewer (no review-resolve) and never owner.
TOKEN_ROLE = "contributor"

_SLUG_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,62}\Z")
_MACHINE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,40}\Z")


@dataclass(frozen=True)
class ProjectToken:
    """A freshly issued credential. ``token`` is plaintext and shown once."""

    name: str
    project: str
    machine: str
    token: str
    expires_at: datetime | None
    replaced: bool  # True when an older token for the same (project, machine) was revoked


def principal_name(project: str, machine: str) -> str:
    return f"{project}@{machine}"


def _validate(project: str, machine: str) -> tuple[str, str]:
    proj = (project or "").strip().lower()
    mach = (machine or "").strip() or "default"
    if not _SLUG_RE.match(proj):
        raise InvalidOperation(
            "project must be 1-63 chars of [a-z0-9._-] (lowercase, as the hooks slugify it)"
        )
    if not _MACHINE_RE.match(mach):
        raise InvalidOperation("machine must be 1-41 chars of [A-Za-z0-9._-]")
    return proj, mach


def issue(
    project: str,
    *,
    machine: str = "default",
    expires_days: int | None = None,
    note: str | None = None,
) -> ProjectToken:
    """Issue (or re-issue) the token for ``project`` on ``machine``.

    Authority comes from :mod:`kbcore.scope`, never from an argument. The caller
    must be able to WRITE the project — so this can carry existing access to a
    machine but can never create access. An owner may name a project that does
    not exist yet: that is the "starting a new project" case, and projects
    auto-upsert on first write anyway.

    Re-issuing REVOKES the previous token for the same (project, machine) in the
    same transaction, so a machine only ever has one live credential per project
    and a leaked one stops working the moment it is replaced.
    """
    proj, mach = _validate(project, machine)
    me = scope.current()

    if not me.is_owner:
        # A grantee may only carry access it already holds. Owners skip this:
        # assert_writable would reject a project that does not exist yet, which
        # is precisely the case they are allowed to open.
        if proj not in me.write_projects:
            raise InvalidOperation(
                f"you do not have write access to {proj!r}, so no token can be issued for it"
            )

    name = principal_name(proj, mach)
    expires_at = datetime.now(UTC) + timedelta(days=int(expires_days)) if expires_days else None

    replaced = False
    with transaction() as conn:
        row = conn.execute(
            "SELECT id FROM principals WHERE name = %s AND revoked_at IS NULL", (name,)
        ).fetchone()
        if row is not None:
            # Rotate rather than accumulate: the old secret stops working now.
            conn.execute("UPDATE principals SET revoked_at = now() WHERE id = %s", (row[0],))
            replaced = True
        if replaced:
            # The name is UNIQUE, so a revoked row must step aside before the
            # replacement can take it. Keep the history under a dated alias
            # rather than deleting it (Invariant 5).
            conn.execute(
                "UPDATE principals SET name = %s WHERE id = %s",
                (f"{name}#revoked-{datetime.now(UTC):%Y%m%d%H%M%S}", row[0]),
            )

    _, token = principals.create_principal(
        name,
        kind="token",
        role=TOKEN_ROLE,
        expires_at=expires_at,
        note=note or f"project token for {proj} on {mach} (issued to {me.name})",
    )
    principals.grant(name, proj, rw=True)
    principals._clear_cache()

    log.info("project token issued: %s (replaced=%s, expires=%s)", name, replaced, expires_at)
    return ProjectToken(
        name=name,
        project=proj,
        machine=mach,
        token=str(token),
        expires_at=expires_at,
        replaced=replaced,
    )


def revoke(project: str, machine: str = "default") -> bool:
    """Revoke the token for one (project, machine). True if one was live."""
    proj, mach = _validate(project, machine)
    name = principal_name(proj, mach)
    scope.assert_writable(proj)
    with transaction() as conn:
        row = conn.execute(
            "UPDATE principals SET revoked_at = now() "
            "WHERE name = %s AND revoked_at IS NULL RETURNING id",
            (name,),
        ).fetchone()
    principals._clear_cache()
    return row is not None


def list_for(project: str | None = None) -> list[dict]:
    """Live project tokens the CALLER may see — never any secret."""
    me = scope.current()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT p.name, p.created_at, p.expires_at, g.project
              FROM principals p
              JOIN principal_grants g ON g.principal_id = p.id
             WHERE p.kind = 'token' AND p.revoked_at IS NULL AND p.name LIKE '%%@%%'
             ORDER BY g.project, p.name
            """
        ).fetchall()
    out = []
    for name, created, expires, proj in rows:
        if project is not None and proj != project:
            continue
        if not me.is_owner and proj not in me.read_projects:
            continue
        out.append(
            {
                "name": name,
                "project": proj,
                "machine": name.split("@", 1)[1] if "@" in name else "",
                "created_at": created.isoformat() if created else None,
                "expires_at": expires.isoformat() if expires else None,
                "expired": bool(expires and expires <= datetime.now(UTC)),
            }
        )
    return out

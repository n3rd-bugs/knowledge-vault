"""kbcore.scope — the request-scoped authorization gate (per-project sharing).

Two orthogonal walls compose here, both enforced by the SAME parameterized
predicates so the proven no-existence-oracle discipline extends to both:

1. **Instance public-only** (``KB_PUBLIC_ONLY``, Phase 6): the whole instance may
   only see/write projects flagged ``public``. Unchanged in meaning.
2. **Per-principal grants** (migration 011): the credential on a request resolves
   to a :class:`Principal` — the OWNER (unrestricted, exactly today's behavior) or
   a GRANTEE walled to an explicit project set (read, and write where ``rw``).

A request-scoped :class:`contextvars.ContextVar` carries the resolved principal.
Each surface's auth layer sets it (``use_principal``); kbcore reads it. No
per-call flag is threaded — every read/write already funnels through this module.

**Fail-closed (servers):** ``enter_server_mode()`` is called by each server at
``build_app``. After that, a request that reaches kbcore WITHOUT a principal set
RAISES (→ masked 500, no data) instead of defaulting to OWNER — so a contextvar
propagation gap fails loud, never silently as the owner. Non-server processes
(CLI, worker, tests) leave server mode off and default to OWNER (they run locally
under the owner's ``.env``).

For a withheld target the predicates/asserts collapse to the byte-identical
NotFound a truly-missing target raises — never "forbidden" — so there is NO
existence leak. Every user value travels in ``params``; the SQL fragments are
constant column references chosen by kbcore (CLAUDE.md: parameterized SQL only).
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import psycopg

from kbcore.config import settings
from kbcore.db import connect
from kbcore.errors import InvalidOperation, NotFound


@dataclass(frozen=True)
class Principal:
    """The resolved identity of a request.

    OWNER (``is_owner=True``) is unrestricted. A GRANTEE carries the explicit set
    of projects it may read (``read_projects``) and the subset it may write
    (``write_projects``, always ⊆ read_projects). ``name`` is the audit label
    stamped into ``access_log.principal``.
    """

    name: str
    is_owner: bool = False
    read_projects: frozenset[str] = field(default_factory=frozenset)
    write_projects: frozenset[str] = field(default_factory=frozenset)
    kind: str | None = None


# The implicit owner: the master KB_TOKEN and allowlisted owner emails resolve here.
OWNER = Principal(name="owner", is_owner=True, kind="owner")

# Sentinel distinct from any Principal: the ContextVar was NEVER set on this
# request. Under server mode that is fail-closed (raise); otherwise → OWNER.
_UNSET: Any = object()
_PRINCIPAL: ContextVar[Any] = ContextVar("kb_principal", default=_UNSET)

# Process-wide: flipped true by a server's build_app() so an unset principal on a
# served request fails closed. Left false for CLI/worker/tests (implicit owner).
_SERVER_MODE = False


def enter_server_mode() -> None:
    """Mark this process as a SERVER: an unset principal now fails closed."""
    global _SERVER_MODE
    _SERVER_MODE = True


def _reset_server_mode() -> None:
    """Test hygiene only — restore the non-server default between tests."""
    global _SERVER_MODE
    _SERVER_MODE = False


def current() -> Principal:
    """The principal for this request.

    Unset + server mode → fail closed (RuntimeError → masked 500). Unset otherwise
    (CLI/worker/tests) → OWNER. Never silently returns OWNER on a server.
    """
    val = _PRINCIPAL.get()
    if val is _UNSET:
        if _SERVER_MODE:
            raise RuntimeError(
                "no principal set on a server request — refusing to default to owner "
                "(fail-closed; a request-scope propagation gap)"
            )
        return OWNER
    return val


@contextmanager
def use_principal(principal: Principal):
    """Bind ``principal`` for the duration of the block (set/reset the ContextVar)."""
    token = _PRINCIPAL.set(principal)
    try:
        yield
    finally:
        _PRINCIPAL.reset(token)


def set_principal(principal: Principal):
    """Set the principal and return the reset token (for non-``with`` call sites)."""
    return _PRINCIPAL.set(principal)


def reset_principal(token) -> None:
    _PRINCIPAL.reset(token)


def public_only() -> bool:
    """True if this INSTANCE may only see/write public projects (unchanged)."""
    return settings.kb_public_only


# --- SQL predicates (parameterized) ------------------------------------------
#
# Each returns (fragment, params). The fragment is built ONLY from constant
# column references; every runtime value (the grant project list) rides params.


def project_predicate(project_column: str) -> tuple[str, list[Any]]:
    """A predicate keeping ``project_column`` to the projects VISIBLE to the
    current request, AND-composed across the public-only and grant walls.

    Returns ``('', [])`` when unrestricted (owner on a full-access instance).
    """
    frags: list[str] = []
    params: list[Any] = []
    if settings.kb_public_only:
        frags.append(f"{project_column} IN (SELECT name FROM projects WHERE public IS TRUE)")
    p = current()
    if not p.is_owner:
        # Empty grant set ⇒ ``= ANY('{}')`` is FALSE for every row ⇒ sees nothing.
        frags.append(f"{project_column} = ANY(%s)")
        params.append(sorted(p.read_projects))
    return " AND ".join(frags), params


def project_predicate_named(project_column: str, key: str = "vis") -> tuple[str, dict[str, Any]]:
    """Named-parameter form of :func:`project_predicate` for dict-parameter queries
    (e.g. kb_similar). At most one value, bound to ``key``. ``('', {})`` when open."""
    frag, params = project_predicate(project_column)
    if not frag:
        return "", {}
    if params:  # exactly one positional value (the read-projects list)
        return frag.replace("%s", f"%({key})s"), {key: params[0]}
    return frag, {}


def hidden_predicate(project_column: str) -> tuple[str, list[Any]]:
    """The De Morgan dual of :func:`project_predicate`: TRUE when ``project_column``
    is NOT visible to the current request. ``('', [])`` when nothing is hidden.

    Used to drop relation edges whose OTHER endpoint is a withheld item (no id
    oracle through a visible item's link list).
    """
    frags: list[str] = []
    params: list[Any] = []
    if settings.kb_public_only:
        frags.append(f"{project_column} NOT IN (SELECT name FROM projects WHERE public IS TRUE)")
    p = current()
    if not p.is_owner:
        frags.append(f"{project_column} <> ALL(%s)")
        params.append(sorted(p.read_projects))
    return " OR ".join(frags), params


def hidden_predicate_named(project_column: str, key: str = "vis") -> tuple[str, dict[str, Any]]:
    """Named-parameter form of :func:`hidden_predicate` for queries that use a dict
    of parameters (e.g. the kb_get relation-hide clause). At most one value, bound
    to ``key``. Returns ``('', {})`` when nothing is hidden."""
    frag, params = hidden_predicate(project_column)
    if not frag:
        return "", {}
    if params:  # exactly one positional value (the read-projects list)
        return frag.replace("%s", f"%({key})s"), {key: params[0]}
    return frag, {}


# --- per-row helpers ---------------------------------------------------------


def _is_public(conn: psycopg.Connection, project: str) -> bool:
    row = conn.execute("SELECT public FROM projects WHERE name = %s", (project,)).fetchone()
    return bool(row and row[0])


def project_readable(conn: psycopg.Connection, project: str) -> bool:
    """True if the current request may READ ``project`` (grant ∩ public walls)."""
    p = current()
    if not p.is_owner and project not in p.read_projects:
        return False
    if settings.kb_public_only and not _is_public(conn, project):
        return False
    return True


def assert_readable(project: str, not_found_msg: str) -> None:
    """A project the request cannot read must look like it does not exist.

    Raises NotFound with the SAME message a truly-missing target would, so the
    caller cannot distinguish "withheld" from "absent". The grant check is a pure
    in-memory set membership (no DB round trip, so no timing oracle vs missing);
    the public-only check hits the DB only when that instance flag is on.
    """
    p = current()
    if not p.is_owner and project not in p.read_projects:
        raise NotFound(not_found_msg)
    if settings.kb_public_only:
        with connect() as conn:
            if not _is_public(conn, project):
                raise NotFound(not_found_msg)


# Uniform write-rejection wording for grantees: never distinguishes
# read-only-grant from not-granted-at-all from nonexistent (no existence oracle).
def _not_writable(project: str) -> InvalidOperation:
    return InvalidOperation(f"project {project!r} is not writable with this credential")


def assert_writable(project: str) -> None:
    """Reject a write the request is not permitted to make.

    Grantee: requires an ``rw`` grant on ``project`` — otherwise a uniform message
    that leaks neither existence nor the read/write distinction. Public-only: the
    project must be public (existing behavior/message preserved for that path).
    """
    p = current()
    if not p.is_owner and project not in p.write_projects:
        raise _not_writable(project)
    if settings.kb_public_only:
        with connect() as conn:
            if not _is_public(conn, project):
                raise InvalidOperation(
                    f"project {project!r} is not public / not writable on this instance"
                )


def require_owner(what: str = "this operation") -> None:
    """Guard an owner-only surface (review tools, on-demand GPU runs).

    Grantees get a clean, non-leaky message — the tool's EXISTENCE is not secret,
    only cross-project data is, and these surfaces expose cross-project payloads
    (review detail JSONB) or the owner's GPU (Invariant 8).
    """
    if not current().is_owner:
        raise InvalidOperation(f"{what} is available to the KB owner only")

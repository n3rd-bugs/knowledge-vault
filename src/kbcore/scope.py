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

**Fail-closed (servers):** ``enter_server_mode()`` is called by each server's
``main()`` before it starts serving. After that, a request that reaches kbcore
WITHOUT a principal set
RAISES (→ masked 500, no data) instead of defaulting to OWNER — so a contextvar
propagation gap fails loud, never silently as the owner. Non-server processes
(CLI, worker, tests) leave server mode off and default to OWNER (they run locally
under the owner's ``.env``).

For a withheld target the predicates/asserts collapse to the byte-identical
NotFound a truly-missing target raises — never "forbidden" — so there is NO
existence leak. Every user value travels in ``params``; the SQL fragments are
constant column references chosen by kbcore (CLAUDE.md: parameterized SQL only).

**COMPOSITION CONTRACT (tiered access, WEBUI_TIERED_ACCESS_PLAN §2):** a third
wall — capabilities — gates FEATURES, while project grants keep gating ROWS.
``Principal.caps`` / ``has_cap`` / ``require_cap`` decide whether a principal may
use a feature AT ALL (save, browse, deep search, …); ``project_predicate`` /
``assert_readable`` / ``assert_writable`` decide which data that feature may
touch. ``require_cap`` NEVER replaces the row walls — call sites keep both, and
the checks AND (exactly like public_only × grants already do). A contributor
without an rw grant still gets the uniform not-writable rejection; a reviewer
with zero grants sees zero rows everywhere.
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

# --- capability vocabulary (WEBUI_TIERED_ACCESS_PLAN §2) ----------------------

CAP_SEARCH = "search"  # kb_search + kb_get; web /, /tags/{tag}, /item/{id}
CAP_BROWSE = "browse"  # timeline/stats/projects/trace/similar (tools + views)
CAP_SAVE = "save"  # kb_save / kb_supersede / kb_link — ANDed with rw grants
CAP_REVIEW_READ = "review_read"  # kb_review_list, /review — Phase 4, after the detail scrub
CAP_REVIEW_RESOLVE = "review_resolve"  # kb_review_resolve / kb_review_bulk — Phase 4
CAP_DEEP_SEARCH = "deep_search"  # the owner's GPU/14B (Invariant 8)

ALL_CAPS = frozenset(
    {CAP_SEARCH, CAP_BROWSE, CAP_SAVE, CAP_REVIEW_READ, CAP_REVIEW_RESOLVE, CAP_DEEP_SEARCH}
)

# Role → capability bundle, expanded at resolution time (principals._build_grantee).
# 'reader' == today's grantee surface (the behavior-preserving default). 'reviewer'
# deliberately EXCLUDES the review caps until the review_queue.detail JSONB scrub
# lands (Phase 4 hard precondition: detail can carry cross-project titles/ids past
# the row-level gate) — a test pins this. No bundle grants CAP_DEEP_SEARCH (owner
# GPU; flippable later without a migration). 'owner' is NOT a role: ownership is
# credential-derived (``is_owner``), never a DB row — migration 012's frozen CHECK
# is the escalation firewall.
ROLE_CAPS: dict[str, frozenset[str]] = {
    "guest": frozenset({CAP_SEARCH}),
    "reader": frozenset({CAP_SEARCH, CAP_BROWSE}),
    "contributor": frozenset({CAP_SEARCH, CAP_BROWSE, CAP_SAVE}),
    "reviewer": frozenset({CAP_SEARCH, CAP_BROWSE, CAP_SAVE}),
}


@dataclass(frozen=True)
class Principal:
    """The resolved identity of a request.

    OWNER (``is_owner=True``) is unrestricted. A GRANTEE carries the explicit set
    of projects it may read (``read_projects``) and the subset it may write
    (``write_projects``, always ⊆ read_projects). ``name`` is the audit label
    stamped into ``access_log.principal``. ``caps`` is the AUTHORITATIVE feature
    set (``role`` is its audit label): default-EMPTY so a construction site that
    forgets expansion degrades to nothing, never to more (fail-closed — the
    ``enter_server_mode`` philosophy).
    """

    name: str
    is_owner: bool = False
    read_projects: frozenset[str] = field(default_factory=frozenset)
    write_projects: frozenset[str] = field(default_factory=frozenset)
    kind: str | None = None
    role: str = "reader"
    caps: frozenset[str] = field(default_factory=frozenset)

    def has_cap(self, cap: str) -> bool:
        """True when this principal may use the FEATURE named ``cap``.

        Pure in-memory membership — no DB round trip, no timing oracle (the same
        property :func:`assert_readable` documents). Short-circuits on
        ``is_owner`` so a stale ``ALL_CAPS`` can never cost the owner a feature.
        """
        return self.is_owner or cap in self.caps


# The implicit owner: the master KB_TOKEN and allowlisted owner emails resolve here.
OWNER = Principal(name="owner", is_owner=True, kind="owner", role="owner", caps=ALL_CAPS)

# Sentinel distinct from any Principal: the ContextVar was NEVER set on this
# request. Under server mode that is fail-closed (raise); otherwise → OWNER.
_UNSET: Any = object()
_PRINCIPAL: ContextVar[Any] = ContextVar("kb_principal", default=_UNSET)

# Process-wide: flipped true by a server's main() (before uvicorn serves) so an
# unset principal on a served request fails closed. Left false for CLI/worker/tests
# (implicit owner).
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


def readable_item_ids(conn: psycopg.Connection, item_ids: list[int]) -> set[int]:
    """Subset of ``item_ids`` whose project the current request may READ.

    ONE scoped SELECT reusing :func:`project_predicate`, so masking is byte-identical
    to every other read wall (owner → all present ids; grantee/public-only → only the
    visible ones). Used to null a denormalized cross-project pointer (superseded_by)
    whose target lives in a withheld project — the batch form for a whole result set.
    """
    if not item_ids:
        return set()
    frag, params = project_predicate("project")
    where = f" AND {frag}" if frag else ""
    rows = conn.execute(
        f"SELECT id FROM items WHERE id = ANY(%s){where}",  # noqa: S608 — frag is a constant fragment; values ride params
        [list(item_ids), *params],
    ).fetchall()
    return {r[0] for r in rows}


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


def require_cap(cap: str, what: str = "this operation") -> None:
    """Guard a capability-gated FEATURE (tiered access, plan §2).

    UNIFORM wording that NEVER names the missing capability — the tier ladder is
    not enumerable through error text. Per-principal-constant (independent of any
    target), so placing it before a row wall leaks zero target-existence signal.
    Composes with — never replaces — ``project_predicate``/``assert_readable``/
    ``assert_writable`` (the composition contract in the module docstring).
    """
    if not current().has_cap(cap):
        raise InvalidOperation(f"{what} is not available with this credential")


def log_write(conn: psycopg.Connection, item_id: int, tool: str) -> None:
    """Attribute a grantee WRITE inside the caller's open transaction (audit).

    Owner/local writes stay unattributed (no row — the read paths already stamp NULL
    for the owner); only a NON-owner grantee's mutation records its principal name, so
    a grantee's writes are attributable just like its reads. ``item_id`` must already
    exist in this transaction (access_log FK) — true on every write path, where the
    row was just inserted/locked. In-transaction with the write, parameterized."""
    p = current()
    if p.is_owner:
        return
    conn.execute(
        "INSERT INTO access_log (item_id, tool, principal) VALUES (%s, %s, %s)",
        (item_id, tool, p.name),
    )

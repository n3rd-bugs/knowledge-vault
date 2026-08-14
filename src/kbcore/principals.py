"""kbcore.principals — grantee credentials for per-project sharing (migration 011).

Owner-invoked service functions (create / grant / revoke / rotate / list) plus the
request-time RESOLVERS the auth layers call to turn a presented credential into a
:class:`kbcore.scope.Principal`. All SQL lives here (Invariant 2); every write runs
in a transaction. Secrets are never stored — only the SHA-256 hex of a token — and
the plaintext is returned exactly once, at creation/rotation.

Token format: ``kbg_`` + ``secrets.token_urlsafe(32)`` (~43 url-safe chars, 256-bit
entropy). The distinct prefix keeps grantee tokens greppable and lets the master
constant-time comparison never collide.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime

from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation, NotFound
from kbcore.scope import ROLE_CAPS, Principal

log = logging.getLogger("kbcore.principals")

TOKEN_PREFIX = "kbg_"
_TOKEN_NBYTES = 32  # secrets.token_urlsafe(32) → ~43 chars, well over the 40 floor


def _hash(token: str) -> str:
    """SHA-256 hex of the full token string — 256-bit random secrets need no pepper."""
    return hashlib.sha256(token.encode()).hexdigest()


def mint_token() -> str:
    """A fresh ``kbg_`` bearer secret (plaintext, shown once)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_NBYTES)


@dataclass(frozen=True)
class PrincipalInfo:
    """Owner-facing view of a principal for ``kb principal list`` — never a secret."""

    id: int
    name: str
    kind: str
    role: str  # capability bundle (migration 012) — see scope.ROLE_CAPS
    email: str | None
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    note: str | None
    grants: tuple[tuple[str, bool], ...]  # (project, rw)


# --- owner-invoked management ------------------------------------------------


def create_principal(
    name: str,
    *,
    kind: str = "token",
    email: str | None = None,
    expires_at: datetime | None = None,
    note: str | None = None,
    role: str = "reader",
) -> tuple[int, str | None]:
    """Create a grantee. Returns ``(principal_id, plaintext_token | None)``.

    ``kind='token'`` mints a ``kbg_`` bearer, stores only its hash, and returns
    the plaintext secret exactly once — never recoverable afterwards (the marker
    sentence: :data:`kbcore.scrub.MINTED_SECRETS` must list this function).
    ``kind='google'`` binds a
    verified email (for the claude.ai connector / web UI) and returns no token.
    ``role`` is the capability bundle (plan §2), validated against
    :data:`kbcore.scope.ROLE_CAPS` — 'owner' is not creatable (never a row).
    """
    if kind not in ("token", "google"):
        raise InvalidOperation(f"unknown principal kind {kind!r}; one of token, google")
    if role not in ROLE_CAPS:
        raise InvalidOperation(f"unknown role {role!r}; one of {', '.join(sorted(ROLE_CAPS))}")
    if not name or not name.strip():
        raise InvalidOperation("principal name is required")
    name = name.strip()
    token: str | None = None
    secret_hash: str | None = None
    if kind == "token":
        if email is not None:
            raise InvalidOperation("a token principal takes no email")
        token = mint_token()
        secret_hash = _hash(token)
    else:  # google
        if not email or not email.strip():
            raise InvalidOperation("a google principal requires --email")
        email = email.strip().lower()
    with transaction() as conn:
        if conn.execute("SELECT 1 FROM principals WHERE name = %s", (name,)).fetchone():
            raise InvalidOperation(f"principal {name!r} already exists")
        # Email uniqueness is also enforced by a DB constraint; pre-check it so a
        # duplicate surfaces as a clean domain error (KBError) instead of a raw
        # psycopg UniqueViolation that would escape the web view as a 500.
        if (
            email is not None
            and conn.execute("SELECT 1 FROM principals WHERE email = %s", (email,)).fetchone()
        ):
            raise InvalidOperation(f"a principal with email {email!r} already exists")
        row = conn.execute(
            """
            INSERT INTO principals (name, kind, secret_hash, email, expires_at, note, role)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (name, kind, secret_hash, email, expires_at, note, role),
        ).fetchone()
    return int(row[0]), token


def _principal_id(conn, name: str) -> int:
    row = conn.execute("SELECT id FROM principals WHERE name = %s", (name,)).fetchone()
    if row is None:
        raise NotFound(f"principal {name!r} does not exist")
    return int(row[0])


def grant(name: str, project: str, *, rw: bool = False) -> None:
    """Grant ``name`` access to ``project`` (read, or read-write when ``rw``).

    Idempotent: re-granting updates the rw flag (a pure upsert, never a delete).
    """
    if not project or not project.strip():
        raise InvalidOperation("project is required")
    with transaction() as conn:
        pid = _principal_id(conn, name)
        conn.execute(
            """
            INSERT INTO principal_grants (principal_id, project, rw)
            VALUES (%s, %s, %s)
            ON CONFLICT (principal_id, project) DO UPDATE SET rw = EXCLUDED.rw
            """,
            (pid, project.strip(), rw),
        )
    # Flush the positive token cache so a newly-granted project is visible on the
    # next request instead of lagging up to the cache TTL (parity with revoke()).
    _clear_cache()


def ungrant(name: str, project: str) -> None:
    """Remove a project grant from ``name`` (owner CLI action; automation never
    touches these tables, so Invariant 5 — 'automation never deletes' — is not
    implicated)."""
    with transaction() as conn:
        pid = _principal_id(conn, name)
        conn.execute(
            "DELETE FROM principal_grants WHERE principal_id = %s AND project = %s",
            (pid, project.strip()),
        )
    # Flush the positive token cache so a de-scoped project stops being readable on
    # the next request instead of lingering up to the cache TTL (parity with revoke()).
    _clear_cache()


def revoke(name: str) -> None:
    """Revoke a principal (timestamp flip). Its credential stops authenticating on
    the next request — within the token cache TTL for `kbg_` bearers, immediately
    for the per-request web/OAuth resolution."""
    with transaction() as conn:
        pid = _principal_id(conn, name)
        conn.execute(
            "UPDATE principals SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
            (pid,),
        )
    _clear_cache()


def rotate(name: str) -> str:
    """Mint a NEW secret for a token principal; the old hash dies immediately.

    Returns the new plaintext (shown once). Rejects google principals (no secret)
    and revoked principals. Grants and audit history are preserved (same row)."""
    with transaction() as conn:
        row = conn.execute(
            "SELECT id, kind, revoked_at FROM principals WHERE name = %s", (name,)
        ).fetchone()
        if row is None:
            raise NotFound(f"principal {name!r} does not exist")
        pid, kind, revoked_at = row
        if kind != "token":
            raise InvalidOperation(f"principal {name!r} is a {kind} identity — nothing to rotate")
        if revoked_at is not None:
            raise InvalidOperation(f"principal {name!r} is revoked; create a new one instead")
        token = mint_token()
        conn.execute("UPDATE principals SET secret_hash = %s WHERE id = %s", (_hash(token), pid))
    _clear_cache()
    return token


def set_role(name: str, role: str) -> None:
    """Change ``name``'s role (capability bundle). CLI-only surface — the web
    admin can never call this (a trip-wire test pins ``set_role`` out of kbweb).

    Validated against :data:`kbcore.scope.ROLE_CAPS` ('owner' is unreachable —
    never a row). Cache-flush parity with grant/ungrant/revoke: the change takes
    effect on the next request instead of lagging up to the token cache TTL.
    """
    if role not in ROLE_CAPS:
        raise InvalidOperation(f"unknown role {role!r}; one of {', '.join(sorted(ROLE_CAPS))}")
    with transaction() as conn:
        pid = _principal_id(conn, name)
        conn.execute("UPDATE principals SET role = %s WHERE id = %s", (role, pid))
    _clear_cache()


def list_principals() -> list[PrincipalInfo]:
    """All principals with their grants — NEVER a secret (CLAUDE.md hygiene)."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, name, kind, role, email, created_at, expires_at, revoked_at, note
            FROM principals ORDER BY name
            """
        ).fetchall()
        grants: dict[int, list[tuple[str, bool]]] = {}
        for pid, project, rw in conn.execute(
            "SELECT principal_id, project, rw FROM principal_grants ORDER BY project"
        ).fetchall():
            grants.setdefault(pid, []).append((project, rw))
    return [
        PrincipalInfo(
            id=r[0],
            name=r[1],
            kind=r[2],
            role=r[3],
            email=r[4],
            created_at=r[5],
            expires_at=r[6],
            revoked_at=r[7],
            note=r[8],
            grants=tuple(grants.get(r[0], [])),
        )
        for r in rows
    ]


# --- request-time resolution (auth layers) -----------------------------------
#
# A tiny positive-only TTL cache follows the _install_verify_cache pattern in
# kbmcp.server: cache only SUCCESSFUL resolutions for KB_PRINCIPAL_CACHE_TTL
# seconds so a hot connector burst skips the DB, while a revocation takes effect
# within that TTL and a freshly-minted token authenticates immediately (misses
# are never cached). Grants ride inside the cached Principal, so they too refresh
# within one TTL — no separate grant cache that could outlive a revocation.

_cache: dict[str, tuple[Principal, float]] = {}


def _clear_cache() -> None:
    _cache.clear()


def _build_grantee(conn, pid: int, name: str, kind: str, role: str) -> Principal:
    read: set[str] = set()
    write: set[str] = set()
    for project, rw in conn.execute(
        "SELECT project, rw FROM principal_grants WHERE principal_id = %s", (pid,)
    ).fetchall():
        read.add(project)
        if rw:
            write.add(project)
    caps = ROLE_CAPS.get(role)
    if caps is None:
        # Schema/code skew (a role value this build does not know) serves the wall
        # LOUDLY: zero capabilities — never a 500, never more access (plan §2).
        log.warning("principal %r has unknown role %r; serving zero capabilities", name, role)
        caps = frozenset()
    return Principal(
        name=name,
        is_owner=False,
        read_projects=frozenset(read),
        write_projects=frozenset(write),
        kind=kind,
        role=role,
        caps=caps,
    )


def _live_where() -> str:
    return "revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())"


def resolve_token(token: str | None) -> Principal | None:
    """Resolve a presented ``kbg_`` bearer to a grantee Principal, or None.

    None means "not a live grantee token" — the caller must treat it exactly like
    an unknown token (same 401, no revoked-vs-unknown oracle). Never resolves the
    master token (that is matched constant-time BEFORE this is called)."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    h = _hash(token)
    now = time.monotonic()
    entry = _cache.get(h)
    if entry is not None and entry[1] > now:
        return entry[0]
    with connect() as conn:
        row = conn.execute(
            f"SELECT id, name, kind, role FROM principals "
            f"WHERE secret_hash = %s AND {_live_where()}",  # noqa: S608 — _live_where() is a constant fragment
            (h,),
        ).fetchone()
        if row is None:
            _cache.pop(h, None)
            return None
        principal = _build_grantee(conn, int(row[0]), row[1], row[2], row[3])
    # Evict expired entries, then cache this positive result for the TTL window.
    from kbcore.config import settings

    for stale in [k for k, v in _cache.items() if v[1] <= now]:
        del _cache[stale]
    _cache[h] = (principal, now + settings.kb_principal_cache_ttl)
    return principal


def resolve_name(name: str | None) -> Principal | None:
    """Resolve a LIVE grantee by its handle (web-cookie path), or None if missing/
    revoked/expired. Re-checked per request so a revoked grantee's still-valid
    cookie dies immediately (the cookie signature alone must never authorize)."""
    if not name:
        return None
    with connect() as conn:
        row = conn.execute(
            f"SELECT id, name, kind, role FROM principals WHERE name = %s AND {_live_where()}",  # noqa: S608 — constant fragment
            (name,),
        ).fetchone()
        if row is None:
            return None
        return _build_grantee(conn, int(row[0]), row[1], row[2], row[3])


def resolve_email(email: str | None) -> Principal | None:
    """Resolve a verified Google email to a grantee Principal, or None.

    Owner-email mapping (KB_WEB_ALLOWED_EMAILS) is handled by the caller BEFORE
    this — an allowlisted owner email never reaches here. Not cached: the web/MCP
    OAuth surfaces already re-check per request, keeping revocation instant."""
    if not email:
        return None
    email = email.strip().lower()
    with connect() as conn:
        row = conn.execute(
            f"SELECT id, name, kind, role FROM principals "
            f"WHERE email = %s AND kind = 'google' AND {_live_where()}",  # noqa: S608 — constant fragment
            (email,),
        ).fetchone()
        if row is None:
            return None
        return _build_grantee(conn, int(row[0]), row[1], row[2], row[3])

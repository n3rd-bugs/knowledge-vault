"""kbcore.enrolment — one-time codes that enrol a new machine's session hooks.

The problem this solves: ``kb_setup`` answers over the claude.ai connector, so
anything it returns is written into a chat transcript — and the session hooks
flush transcripts INTO the KB, which is then backed up off-site. A live bearer
handed over that way is durable in three places at once, none of which can be
un-published. So the connector hands over a code instead: single-use, minutes
long, and worthless the moment the machine has used it.

Authority rules (all enforced here, never in a caller — Invariant 2):

* A code NEVER carries more than the identity that asked for it. The requester
  is read from :mod:`kbcore.scope`, not from an argument, so a caller cannot ask
  for someone else's authority.
* An OWNER's code redeems to the master credential. That is not an escalation —
  the owner already holds it — and it is what makes the hooks work on projects
  that do not exist yet, which a fixed-grant token cannot do.
* A GRANTEE's code mints a fresh per-machine ``kbg_`` principal carrying exactly
  that grantee's grants and role. Every machine is then revocable on its own.
* An identity with no authority (authenticated but ungranted) cannot get a code
  at all.

Redemption is a single atomic UPDATE, so two racing redemptions cannot both
succeed. Invalid, expired and already-redeemed are reported IDENTICALLY (the
no-oracle contract used everywhere else in this codebase); the real reason is
recorded server-side.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from kbcore import principals, scope
from kbcore.db import transaction
from kbcore.errors import InvalidOperation

log = logging.getLogger("kbcore.enrolment")

CODE_PREFIX = "kbe_"
_CODE_NBYTES = 24  # ~32 url-safe chars: short enough to retype, 192 bits of entropy

# Deliberately short. The code only has to survive the seconds between the model
# printing it and the machine redeeming it; every extra minute is exposure in a
# transcript that gets stored and backed up.
DEFAULT_TTL_MINUTES = 15
MAX_TTL_MINUTES = 60

# One identity should never need many live codes at once; a runaway loop asking
# for them would otherwise mint unbounded credentials-in-waiting.
MAX_LIVE_CODES_PER_IDENTITY = 5


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def mint_code() -> str:
    """A fresh ``kbe_`` enrolment code (plaintext, shown once)."""
    return CODE_PREFIX + secrets.token_urlsafe(_CODE_NBYTES)


@dataclass(frozen=True)
class IssuedCode:
    """What the requester is shown. The plaintext appears here and nowhere else."""

    code: str
    expires_at: datetime
    grants_owner: bool
    issued_to: str


@dataclass(frozen=True)
class RedeemedCredential:
    """What a machine gets back. ``token`` is the bearer it should store."""

    token: str
    is_owner: bool
    principal_name: str | None  # the per-machine principal, when one was minted
    expires_at: datetime | None


def _looks_like_code(value: str | None) -> bool:
    return bool(value) and str(value).startswith(CODE_PREFIX)


def create_code(*, ttl_minutes: int = DEFAULT_TTL_MINUTES, note: str | None = None) -> IssuedCode:
    """Issue a one-time enrolment code for the CURRENT principal.

    The authority is read from :mod:`kbcore.scope` rather than taken as an
    argument: a caller can only ever obtain a code for itself.
    """
    principal = scope.current()
    ttl = max(1, min(int(ttl_minutes or DEFAULT_TTL_MINUTES), MAX_TTL_MINUTES))
    expires_at = datetime.now(UTC) + timedelta(minutes=ttl)

    grants_owner = bool(principal.is_owner)
    principal_id: int | None = None
    if not grants_owner:
        # A grantee may enrol a machine with its OWN authority. An identity with
        # nothing to give (authenticated but ungranted) gets nothing — otherwise
        # enrolment would be a way to mint a credential from thin air.
        row = principals.resolve_name(principal.name)
        if row is None or not principal.read_projects:
            raise InvalidOperation(
                "this identity has no access to enrol a machine with; "
                "ask the owner for a project grant first"
            )
        principal_id = _principal_row_id(principal.name)

    code = mint_code()
    with transaction() as conn:
        live = conn.execute(
            """
            SELECT count(*) FROM enrolment_codes
             WHERE issued_to = %s AND redeemed_at IS NULL AND expires_at > now()
            """,
            (principal.name,),
        ).fetchone()[0]
        if int(live) >= MAX_LIVE_CODES_PER_IDENTITY:
            raise InvalidOperation(
                f"too many unredeemed enrolment codes ({live}); "
                "redeem or wait for one to expire before issuing another"
            )
        conn.execute(
            """
            INSERT INTO enrolment_codes
                   (code_hash, issued_to, grants_owner, principal_id, expires_at, redeemed_note)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (_hash(code), principal.name, grants_owner, principal_id, expires_at, note),
        )
    # The plaintext is never logged — only that one was issued, and to whom.
    log.info("enrolment code issued to %s (owner=%s, ttl=%dm)", principal.name, grants_owner, ttl)
    return IssuedCode(
        code=code, expires_at=expires_at, grants_owner=grants_owner, issued_to=principal.name
    )


def _principal_row_id(name: str) -> int:
    with transaction() as conn:
        row = conn.execute("SELECT id FROM principals WHERE name = %s", (name,)).fetchone()
        if row is None:
            raise InvalidOperation("unknown identity")
        return int(row[0])


# One message for every failure mode. A caller must not be able to tell an
# unknown code from an expired one from an already-redeemed one — that
# difference is exactly what a code-guessing attack would measure.
_REFUSED = "enrolment code is not valid"


def redeem(
    code: str | None, *, machine: str | None = None, from_addr: str | None = None
) -> RedeemedCredential:
    """Redeem a code ONCE and return the credential the machine should store.

    Raises :class:`InvalidOperation` with an identical message for every failure
    (unknown / expired / already used / malformed).
    """
    if not _looks_like_code(code):
        raise InvalidOperation(_REFUSED)
    label = (machine or "machine").strip()[:60] or "machine"

    with transaction() as conn:
        # Single atomic claim: the WHERE clause is the mutual exclusion, so two
        # simultaneous redemptions cannot both come back with a row.
        row = conn.execute(
            """
            UPDATE enrolment_codes
               SET redeemed_at = now(), redeemed_from = %s, redeemed_note = %s
             WHERE code_hash = %s
               AND redeemed_at IS NULL
               AND expires_at > now()
         RETURNING id, issued_to, grants_owner, principal_id
            """,
            (from_addr, label, _hash(str(code))),
        ).fetchone()
        if row is None:
            log.warning("enrolment refused (unknown/expired/already used) from %s", from_addr)
            raise InvalidOperation(_REFUSED)
        code_id, issued_to, grants_owner, principal_id = (
            int(row[0]),
            row[1],
            bool(row[2]),
            row[3],
        )

    if grants_owner:
        # The owner already holds this credential; the code was only the envelope
        # that carried it out of the transcript.
        from kbcore.config import settings

        log.info("enrolment redeemed by %s as OWNER for %r", from_addr, label)
        return RedeemedCredential(
            token=settings.kb_token, is_owner=True, principal_name=None, expires_at=None
        )

    # A grantee code mints a machine-specific principal carrying EXACTLY the
    # requesting grantee's grants — never more — so this machine can be revoked
    # without touching the person's other machines.
    src = _principal_info(int(principal_id)) if principal_id is not None else None
    if src is None:
        # The requesting grantee was revoked or removed between issue and
        # redemption. Same refusal as any other bad code — no oracle.
        raise InvalidOperation(_REFUSED)
    name = _unique_machine_name(issued_to, label)
    new_id, token = principals.create_principal(
        name,
        kind="token",
        role=src.role,
        note=f"enrolled machine for {issued_to} (code {code_id})",
        expires_at=src.expires_at,
    )
    for project, rw in src.grants:
        principals.grant(name, project, rw=rw)
    with transaction() as conn:
        conn.execute(
            "UPDATE enrolment_codes SET issued_principal_id = %s WHERE id = %s",
            (new_id, code_id),
        )
    log.info("enrolment redeemed by %s -> principal %s", from_addr, name)
    return RedeemedCredential(
        token=str(token), is_owner=False, principal_name=name, expires_at=src.expires_at
    )


def _principal_info(pid: int) -> principals.PrincipalInfo | None:
    """The requesting grantee's live record, or None if it no longer authenticates.

    A code outlives nothing: if the grantee was revoked or expired after the code
    was issued, redemption must not resurrect their authority.
    """
    for info in principals.list_principals():
        if info.id == pid:
            if info.revoked_at is not None:
                return None
            if info.expires_at is not None and info.expires_at <= datetime.now(UTC):
                return None
            return info
    return None


def _unique_machine_name(issued_to: str, label: str) -> str:
    """A readable, collision-free principal name for an enrolled machine."""
    base = f"{issued_to}-{label}".lower()
    base = "".join(c if c.isalnum() or c in "-_." else "-" for c in base)[:48].strip("-")
    with transaction() as conn:
        for suffix in ("", *(f"-{n}" for n in range(2, 50))):
            candidate = f"{base}{suffix}" or "enrolled-machine"
            if (
                conn.execute("SELECT 1 FROM principals WHERE name = %s", (candidate,)).fetchone()
                is None
            ):
                return candidate
    return f"{base}-{secrets.token_hex(3)}"


def is_live_code(code: str | None) -> bool:
    """True only for a code that exists, is unredeemed and has not expired.

    The bearer layer needs this because a PREFIX is not a credential. An earlier
    version of resolve_bearer returned an authenticated principal for anything
    starting with ``kbe_``, which satisfied the bearer gate (CLAUDE.md rule 6)
    with four known characters. The identity it granted held no projects and no
    capabilities, so nothing could be read — but "authenticates with no secret"
    is not a state this system may be in, and it made the document-staging
    endpoints reachable without a credential.

    Deliberately no cache: a code is single-use and short-lived, so the window
    a cache would optimise is the window in which correctness matters most.
    """
    if not _looks_like_code(code):
        return False
    with transaction() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM enrolment_codes
             WHERE code_hash = %s AND redeemed_at IS NULL AND expires_at > now()
            """,
            (_hash(str(code)),),
        ).fetchone()
    return row is not None


def sweep_expired(older_than_days: int = 30) -> int:
    """Drop long-dead codes. Returns how many rows went.

    Unlike knowledge rows (Invariant 5), a spent or expired code is not a record
    of anything — it holds no content, and keeping the hash forever would only
    grow the table. Codes are swept well after they stop being usable.
    """
    with transaction() as conn:
        cur = conn.execute(
            "DELETE FROM enrolment_codes WHERE expires_at < now() - make_interval(days => %s)",
            (int(older_than_days),),
        )
        return cur.rowcount or 0

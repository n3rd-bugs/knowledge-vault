"""kbcore.enrolment — one-time codes that enrol a machine's session hooks.

These are credential tests, so they are written around the properties that make
the scheme safe rather than around the happy path: a code must never carry more
authority than the identity that asked for it, must work exactly once, and must
not tell an attacker WHY it was refused.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kbcore import enrolment, principals, scope
from kbcore.db import transaction
from kbcore.errors import InvalidOperation


@pytest.fixture()
def as_owner():
    with scope.use_principal(scope.OWNER):
        yield


def _grantee(name: str, projects: tuple[str, ...] = ("alpha",), rw: bool = True) -> str:
    """A live grantee with grants, returning its principal name."""
    principals.create_principal(name, kind="token", role="contributor")
    for proj in projects:
        principals.grant(name, proj, rw=rw)
    return name


# ---------------------------------------------------------------- issuing


def test_owner_code_redeems_to_the_owner_credential(kb, as_owner, monkeypatch):
    """The owner's machine needs authority over projects that do not exist yet.

    A fixed-grant token cannot express that — it fails the moment a session runs
    in a new directory — so an owner's code redeems to the credential the owner
    already holds. That is a transport decision, not an escalation.
    """
    from kbcore.config import settings

    monkeypatch.setattr(settings, "kb_token", "master-secret-for-test")
    issued = enrolment.create_code()
    assert issued.code.startswith(enrolment.CODE_PREFIX)
    assert issued.grants_owner is True

    cred = enrolment.redeem(issued.code, machine="laptop")
    assert cred.is_owner is True
    assert cred.token == "master-secret-for-test"
    assert cred.principal_name is None, "an owner code must not create a principal row"


def test_grantee_code_mints_a_PER_MACHINE_principal_with_only_their_grants(kb, as_owner):
    """A grantee's machine gets its own revocable identity, never the owner's."""
    name = _grantee("alice", projects=("alpha", "beta"), rw=True)
    principals.grant(name, "readonly-proj", rw=False)
    alice = principals.resolve_name(name)

    with scope.use_principal(alice):
        issued = enrolment.create_code()
    assert issued.grants_owner is False

    cred = enrolment.redeem(issued.code, machine="alice-desktop")
    assert cred.is_owner is False
    assert cred.token.startswith(principals.TOKEN_PREFIX)
    assert cred.principal_name and cred.principal_name != name

    # The minted machine principal carries EXACTLY the requester's grants.
    machine = principals.resolve_token(cred.token)
    assert machine is not None
    assert machine.is_owner is False
    assert machine.read_projects == alice.read_projects
    assert machine.write_projects == alice.write_projects


def test_an_ungranted_identity_cannot_obtain_a_code(kb):
    """Authenticated but with nothing granted: enrolment must not be a way to
    mint authority out of thin air."""
    walled = scope.Principal(name="google:stranger@example.com", is_owner=False)
    with scope.use_principal(walled), pytest.raises(InvalidOperation):
        enrolment.create_code()


def test_a_caller_cannot_request_a_code_for_someone_else(kb, as_owner):
    """Authority comes from kbcore.scope, never from an argument — so there is
    no parameter through which a grantee could ask for owner authority."""
    import inspect

    params = set(inspect.signature(enrolment.create_code).parameters)
    assert not (params & {"principal", "owner", "grants_owner", "as_owner", "identity"}), (
        "create_code takes an authority argument — authority must come from scope"
    )


# ---------------------------------------------------------------- redeeming


def test_a_code_works_exactly_once(kb, as_owner, monkeypatch):
    from kbcore.config import settings

    monkeypatch.setattr(settings, "kb_token", "master-secret-for-test")
    issued = enrolment.create_code()
    enrolment.redeem(issued.code, machine="first")
    with pytest.raises(InvalidOperation):
        enrolment.redeem(issued.code, machine="second")


def test_an_expired_code_is_refused(kb, as_owner):
    issued = enrolment.create_code()
    with transaction() as conn:
        conn.execute(
            "UPDATE enrolment_codes SET expires_at = %s",
            (datetime.now(UTC) - timedelta(minutes=1),),
        )
    with pytest.raises(InvalidOperation):
        enrolment.redeem(issued.code)


def test_refusals_are_indistinguishable(kb, as_owner, monkeypatch):
    """Unknown, malformed, expired and already-used must read identically.

    The difference between them is exactly what a code-guessing attack measures,
    and it is the same no-oracle contract the rest of this codebase keeps.
    """
    from kbcore.config import settings

    monkeypatch.setattr(settings, "kb_token", "master-secret-for-test")
    spent = enrolment.create_code()
    enrolment.redeem(spent.code)

    stale = enrolment.create_code()
    with transaction() as conn:
        conn.execute(
            "UPDATE enrolment_codes SET expires_at = %s WHERE code_hash != %s",
            (datetime.now(UTC) - timedelta(minutes=1), enrolment._hash(spent.code)),
        )

    messages = set()
    for bad in (None, "", "not-a-code", enrolment.mint_code(), spent.code, stale.code):
        with pytest.raises(InvalidOperation) as exc:
            enrolment.redeem(bad)
        messages.add(str(exc.value))
    assert len(messages) == 1, f"refusal leaks the reason: {messages}"


def test_the_plaintext_code_is_never_stored(kb, as_owner):
    issued = enrolment.create_code()
    with transaction() as conn:
        rows = conn.execute("SELECT code_hash, redeemed_note FROM enrolment_codes").fetchall()
    blob = " ".join(str(c) for row in rows for c in row)
    assert issued.code not in blob, "the plaintext code was persisted"
    assert enrolment._hash(issued.code) in blob


def test_a_revoked_grantee_cannot_redeem_a_code_issued_earlier(kb, as_owner):
    """A code must not outlive the authority behind it."""
    name = _grantee("bob")
    bob = principals.resolve_name(name)
    with scope.use_principal(bob):
        issued = enrolment.create_code()
    principals.revoke(name)
    with pytest.raises(InvalidOperation):
        enrolment.redeem(issued.code, machine="bob-laptop")


def test_live_code_count_is_bounded(kb, as_owner):
    for _ in range(enrolment.MAX_LIVE_CODES_PER_IDENTITY):
        enrolment.create_code()
    with pytest.raises(InvalidOperation):
        enrolment.create_code()


def test_ttl_is_clamped(kb, as_owner):
    issued = enrolment.create_code(ttl_minutes=10_000)
    horizon = datetime.now(UTC) + timedelta(minutes=enrolment.MAX_TTL_MINUTES + 1)
    assert issued.expires_at <= horizon


# ------------------------------------------------------- bearer resolution


def test_an_enrolment_code_authenticates_as_a_ZERO_authority_identity(kb, as_owner):
    """The code has to authenticate — otherwise /api/enrol would need the bearer
    check disabled for an unauthenticated path, which CLAUDE.md rule 6 forbids.
    What it must NOT do is carry any authority of its own.

    It must be a REAL issued code: this test previously used mint_code(), which
    only generates a string and stores nothing, and it passed — which is how the
    prefix-only bypass survived review.
    """
    from kbmcp import principal_ctx

    issued = enrolment.create_code()
    p = principal_ctx.resolve_bearer("Bearer " + issued.code)
    assert p is not None
    assert p.name == principal_ctx.ENROLLING_NAME
    assert p.is_owner is False
    assert not p.read_projects and not p.write_projects
    assert not p.caps, "the enrolling identity must hold no capabilities"


def test_garbage_still_fails_to_authenticate():
    from kbmcp import principal_ctx

    assert principal_ctx.resolve_bearer("Bearer nonsense") is None
    assert principal_ctx.resolve_bearer("") is None


def test_a_PREFIX_is_not_a_credential(kb, as_owner):
    """The bearer gate must not be satisfiable by knowing four characters.

    resolve_bearer used to return an authenticated principal for anything
    starting with `kbe_`. The identity it granted held no projects and no
    capabilities, so nothing could be read — but "authenticates with no secret"
    is not a state this system may be in (CLAUDE.md rule 6), and it made the
    document-staging endpoints reachable without a credential. Verified live
    against production before the fix: `Bearer kbe_x` returned HTTP 200.
    """
    from kbmcp import principal_ctx

    for fake in ("kbe_", "kbe_x", "kbe_totally-made-up-not-a-real-code", "kbe_" + "0" * 40):
        assert principal_ctx.resolve_bearer(f"Bearer {fake}") is None, (
            f"{fake!r} authenticated without being a real code"
        )

    # A genuine, live code still authenticates — as the narrowest identity.
    issued = enrolment.create_code()
    p = principal_ctx.resolve_bearer(f"Bearer {issued.code}")
    assert p is not None and p.name == principal_ctx.ENROLLING_NAME
    assert not p.read_projects and not p.caps


def test_a_spent_or_expired_code_stops_authenticating(kb, as_owner, monkeypatch):
    """Authentication and redemption must agree: once a code is used or has
    expired it is not a credential for anything, including reaching the route
    that would tell it so."""
    from kbcore.config import settings
    from kbmcp import principal_ctx

    monkeypatch.setattr(settings, "kb_token", "master-secret-for-test")
    spent = enrolment.create_code()
    assert principal_ctx.resolve_bearer(f"Bearer {spent.code}") is not None
    enrolment.redeem(spent.code)
    assert principal_ctx.resolve_bearer(f"Bearer {spent.code}") is None

    stale = enrolment.create_code()
    with transaction() as conn:
        conn.execute(
            "UPDATE enrolment_codes SET expires_at = %s WHERE code_hash = %s",
            (datetime.now(UTC) - timedelta(minutes=1), enrolment._hash(stale.code)),
        )
    assert principal_ctx.resolve_bearer(f"Bearer {stale.code}") is None

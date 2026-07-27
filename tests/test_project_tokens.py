"""kbcore.project_tokens — one revocable credential per (project, machine).

These are credential tests: the properties that matter are that a token carries
exactly one project, never more authority than the requester already had, and
that revoking or replacing it actually stops it working.
"""

from __future__ import annotations

import pytest

from kbcore import principals, project_tokens, scope
from kbcore.errors import InvalidOperation, KBError


@pytest.fixture()
def as_owner():
    with scope.use_principal(scope.OWNER):
        yield


def test_a_project_token_can_write_its_project_and_nothing_else(kb, as_owner):
    issued = project_tokens.issue("alpha", machine="laptop")
    assert issued.name == "alpha@laptop"
    assert issued.token.startswith(principals.TOKEN_PREFIX)

    p = principals.resolve_token(issued.token)
    assert p is not None and p.is_owner is False
    assert p.write_projects == frozenset({"alpha"})
    assert p.read_projects == frozenset({"alpha"})
    assert "save" in p.caps, "a capture token must be able to save"

    with scope.use_principal(p):
        scope.assert_writable("alpha")  # allowed
        with pytest.raises(KBError):
            scope.assert_writable("beta")  # everything else is closed


def test_the_owner_may_open_a_project_that_does_not_exist_yet(kb, as_owner):
    """Starting a new project is the case the owner most needs this for, and
    projects auto-upsert on first write anyway."""
    issued = project_tokens.issue("brand-new-project", machine="laptop")
    p = principals.resolve_token(issued.token)
    assert p is not None and p.write_projects == frozenset({"brand-new-project"})


def test_a_grantee_can_only_carry_access_it_already_has(kb, as_owner):
    """A token must be a way to CARRY access to a machine, never to gain it."""
    principals.create_principal("alice", kind="token", role="contributor")
    principals.grant("alice", "alpha", rw=True)
    principals.grant("alice", "readonly", rw=False)
    alice = principals.resolve_name("alice")

    with scope.use_principal(alice):
        ok = project_tokens.issue("alpha", machine="alice-box")
        assert ok.project == "alpha"
        for denied in ("readonly", "someone-elses", "brand-new"):
            with pytest.raises(InvalidOperation):
                project_tokens.issue(denied, machine="alice-box")


def test_reissuing_revokes_the_previous_token_for_that_machine(kb, as_owner):
    first = project_tokens.issue("alpha", machine="laptop")
    assert principals.resolve_token(first.token) is not None

    second = project_tokens.issue("alpha", machine="laptop")
    assert second.replaced is True
    assert second.token != first.token
    assert principals.resolve_token(first.token) is None, "the replaced token still works"
    assert principals.resolve_token(second.token) is not None


def test_revoking_one_machine_leaves_the_others_working(kb, as_owner):
    laptop = project_tokens.issue("alpha", machine="laptop")
    desktop = project_tokens.issue("alpha", machine="desktop")

    assert project_tokens.revoke("alpha", "laptop") is True
    assert principals.resolve_token(laptop.token) is None
    assert principals.resolve_token(desktop.token) is not None, "revocation was not specific"
    assert project_tokens.revoke("alpha", "laptop") is False, "second revoke should be a no-op"


def test_an_expired_token_stops_authenticating(kb, as_owner):
    issued = project_tokens.issue("alpha", machine="laptop", expires_days=1)
    assert issued.expires_at is not None
    from kbcore.db import transaction

    with transaction() as conn:
        conn.execute(
            "UPDATE principals SET expires_at = now() - interval '1 day' WHERE name = %s",
            (issued.name,),
        )
    principals._clear_cache()
    assert principals.resolve_token(issued.token) is None


def test_listing_never_reveals_a_secret(kb, as_owner):
    issued = project_tokens.issue("alpha", machine="laptop")
    rows = project_tokens.list_for()
    assert any(r["name"] == issued.name and r["project"] == "alpha" for r in rows)
    assert issued.token not in str(rows)


def test_names_are_validated(kb, as_owner):
    for bad in ("Has Caps", "with/slash", "", "x" * 80, "../escape"):
        with pytest.raises(InvalidOperation):
            project_tokens.issue(bad, machine="laptop")
    for bad_machine in ("has space", "a/b", "x" * 80):
        with pytest.raises(InvalidOperation):
            project_tokens.issue("alpha", machine=bad_machine)

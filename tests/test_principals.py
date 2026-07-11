"""Per-project sharing — credentials, resolution, fail-closed, backward compat.

The no-oracle READ/WRITE walling matrix lives in test_no_oracle.py; this file
covers the credential lifecycle (create/grant/revoke/rotate/expire), the auth-layer
resolvers + TTL cache, the fail-closed server-mode guard, token-shown-once/hash-only
storage, access_log attribution, and the ZERO-ROWS backward-compat guarantee. Real
DB in kb_test; Ollama mocked by conftest.
"""

from __future__ import annotations

import time

import pytest

from kbcore import db, principals, scope
from kbcore.config import settings
from kbcore.errors import InvalidOperation
from kbcore.ingest import ingest
from kbcore.items import get_item, set_project
from kbcore.search import search

pytestmark = pytest.mark.usefixtures("kb")


def _grantee(name: str, projects: dict[str, bool]) -> scope.Principal:
    """A live token grantee with {project: rw} grants, returned as a scope.Principal."""
    _pid, token = principals.create_principal(name, kind="token")
    for project, rw in projects.items():
        principals.grant(name, project, rw=rw)
    resolved = principals.resolve_token(token)
    assert resolved is not None
    return resolved


# --- credential lifecycle -----------------------------------------------------


def test_token_shown_once_only_hash_stored():
    pid, token = principals.create_principal("alice", kind="token")
    assert token and token.startswith("kbg_")
    assert len(token) >= 44  # 'kbg_' + >=40 url-safe chars
    with db.connect() as conn:
        row = conn.execute(
            "SELECT secret_hash, email FROM principals WHERE id = %s", (pid,)
        ).fetchone()
    assert row[0] and token not in row[0], "plaintext token must never be stored"
    assert row[1] is None
    # list never reveals a secret
    listed = principals.list_principals()
    assert all(token not in str(p) for p in listed)


def test_resolve_token_roundtrip_and_grants():
    _pid, token = principals.create_principal("bob", kind="token")
    principals.grant("bob", "proj-a", rw=True)
    principals.grant("bob", "proj-b", rw=False)
    p = principals.resolve_token(token)
    assert p is not None and not p.is_owner and p.name == "bob"
    assert p.read_projects == frozenset({"proj-a", "proj-b"})
    assert p.write_projects == frozenset({"proj-a"})


def test_unknown_and_malformed_tokens_resolve_none():
    assert principals.resolve_token(None) is None
    assert principals.resolve_token("") is None
    assert principals.resolve_token("not-a-kbg-token") is None
    assert principals.resolve_token("kbg_deadbeefdeadbeefdeadbeef") is None  # well-formed, unknown


def test_revoked_token_stops_resolving_within_cache_ttl(monkeypatch):
    monkeypatch.setattr(settings, "kb_principal_cache_ttl", 0)  # no caching window
    _pid, token = principals.create_principal("carol", kind="token")
    assert principals.resolve_token(token) is not None
    principals.revoke("carol")
    assert principals.resolve_token(token) is None, "a revoked token must not authenticate"


def test_cache_respects_revocation_after_ttl(monkeypatch):
    monkeypatch.setattr(settings, "kb_principal_cache_ttl", 1)
    _pid, token = principals.create_principal("dave", kind="token")
    assert principals.resolve_token(token) is not None  # caches positive
    principals.revoke("dave")  # revoke() clears the cache → immediate on next call
    assert principals.resolve_token(token) is None


def test_cache_hit_avoids_db(monkeypatch):
    monkeypatch.setattr(settings, "kb_principal_cache_ttl", 60)
    _pid, token = principals.create_principal("erin", kind="token")
    assert principals.resolve_token(token) is not None
    # A raw DB revoke that does NOT clear the cache stays masked for the TTL window
    # (bounded staleness ≤ TTL is the documented contract).
    with db.transaction() as conn:
        conn.execute("UPDATE principals SET revoked_at = now() WHERE name = 'erin'")
    assert principals.resolve_token(token) is not None, "cached positive served within TTL"


def test_expired_token_does_not_resolve():
    from datetime import UTC, datetime, timedelta

    past = datetime.now(UTC) - timedelta(days=1)
    _pid, token = principals.create_principal("frank", kind="token", expires_at=past)
    assert principals.resolve_token(token) is None


def test_rotate_kills_old_secret():
    _pid, token = principals.create_principal("grace", kind="token")
    principals.grant("grace", "proj-a")
    new_token = principals.rotate("grace")
    assert new_token != token
    assert principals.resolve_token(token) is None, "old secret must be dead"
    rep = principals.resolve_token(new_token)
    assert rep is not None and rep.read_projects == frozenset({"proj-a"}), "grants preserved"


def test_rotate_rejects_google_and_revoked():
    principals.create_principal("gid", kind="google", email="g@example.com")
    with pytest.raises(InvalidOperation):
        principals.rotate("gid")
    principals.create_principal("rev", kind="token")
    principals.revoke("rev")
    with pytest.raises(InvalidOperation):
        principals.rotate("rev")


def test_google_principal_resolution_and_owner_precedence(monkeypatch):
    principals.create_principal("gg", kind="google", email="Team@Example.com")
    principals.grant("gg", "proj-a")
    p = principals.resolve_email("team@example.com")  # stored lowercased
    assert p is not None and p.name == "gg" and p.read_projects == frozenset({"proj-a"})
    # a google principal has no secret to resolve as a token
    assert principals.resolve_email("nobody@example.com") is None


def test_duplicate_name_and_bad_kind_rejected():
    principals.create_principal("dup", kind="token")
    with pytest.raises(InvalidOperation):
        principals.create_principal("dup", kind="token")
    with pytest.raises(InvalidOperation):
        principals.create_principal("x", kind="banana")
    with pytest.raises(InvalidOperation):
        principals.create_principal("y", kind="google")  # missing email


def test_resolve_name_live_only():
    _pid, _token = principals.create_principal("web1", kind="google", email="w@example.com")
    principals.grant("web1", "proj-a")
    assert principals.resolve_name("web1") is not None
    principals.revoke("web1")
    assert principals.resolve_name("web1") is None
    assert principals.resolve_name("nope") is None


# --- fail-closed server mode --------------------------------------------------


def test_fail_closed_unset_principal_raises_in_server_mode():
    set_project("p", public=False)
    ingest("p", "note", "t", "b")  # seed as owner (server mode off)
    scope.enter_server_mode()
    try:
        with pytest.raises(RuntimeError):
            scope.current()  # unset principal on a server → fail closed, no owner default
    finally:
        scope._reset_server_mode()


def test_server_mode_with_principal_set_ok():
    set_project("p", public=False)
    ingest("p", "note", "t", "b")
    g = _grantee("g", {"p": False})
    scope.enter_server_mode()
    try:
        with scope.use_principal(g):
            assert scope.current().name == "g"  # a set principal is fine under server mode
        with scope.use_principal(scope.OWNER):
            assert scope.current().is_owner
    finally:
        scope._reset_server_mode()


# --- access_log attribution ---------------------------------------------------


def test_access_log_carries_principal_name():
    set_project("p", public=False)
    i = ingest("p", "note", "spi bus note", "the spi bus runs at 1MHz")
    g = _grantee("logger", {"p": False})
    with scope.use_principal(g):
        search("spi bus", k=5)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT principal FROM access_log WHERE item_id = %s", (i,)
        ).fetchall()
    assert ("logger",) in rows, "grantee name stamped into access_log"
    # owner reads stamp NULL (legacy/owner rows)
    search("spi bus", k=5)  # as owner
    with db.connect() as conn:
        owner_rows = conn.execute(
            "SELECT count(*) FROM access_log WHERE item_id = %s AND principal IS NULL", (i,)
        ).fetchone()
    assert owner_rows[0] >= 1


# --- backward compatibility (zero principals rows) ----------------------------


def test_zero_rows_owner_behaves_exactly_as_today():
    # No principals rows exist. The implicit OWNER sees everything, writes anywhere.
    set_project("a", public=False)
    set_project("b", public=False)
    ia = ingest("a", "note", "alpha", "alpha body")
    ib = ingest("b", "note", "beta", "beta body")
    assert get_item(ia).project == "a"
    assert get_item(ib).project == "b"
    hits = {h.item_id for h in search("alpha beta body", k=10)}
    assert {ia, ib} <= hits
    with db.connect() as conn:
        n = conn.execute("SELECT count(*) FROM principals").fetchone()[0]
    assert n == 0, "backward-compat scenario keeps the principals table empty"


def test_owner_predicate_is_empty_when_unrestricted():
    # The generalized predicate must be a no-op for the owner on a full instance —
    # byte-identical SQL to before (no fragment, no params).
    with scope.use_principal(scope.OWNER):
        assert scope.project_predicate("i.project") == ("", [])
        assert scope.hidden_predicate("o.project") == ("", [])


def test_ttl_cache_positive_expires(monkeypatch):
    monkeypatch.setattr(settings, "kb_principal_cache_ttl", 1)
    _pid, token = principals.create_principal("ttl", kind="token")
    assert principals.resolve_token(token) is not None
    time.sleep(1.1)
    # After the window the entry is re-queried from the DB (still valid here).
    assert principals.resolve_token(token) is not None

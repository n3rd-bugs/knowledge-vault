"""kb principal CLI — create/grant/revoke/rotate/list round-trips (owner-local).

Uses typer's CliRunner against the real kb_test DB (Ollama mocked by conftest).
Asserts the secret is printed exactly ONCE at create/rotate and NEVER by list.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from kbcli.main import app
from kbcore import principals

pytestmark = pytest.mark.usefixtures("kb")

runner = CliRunner()


def _run(*args: str):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_create_grant_list_roundtrip():
    out = _run("principal", "create", "alice")
    assert "created token principal" in out
    # the printed token authenticates and is a kbg_ secret
    token_line = [ln for ln in out.splitlines() if ln.startswith("kbg_")]
    assert token_line, out
    token = token_line[0].strip()
    _run("principal", "grant", "alice", "proj-a", "--rw")
    _run("principal", "grant", "alice", "proj-b")
    p = principals.resolve_token(token)
    assert p is not None
    assert p.read_projects == frozenset({"proj-a", "proj-b"})
    assert p.write_projects == frozenset({"proj-a"})
    listed = _run("principal", "list")
    assert "alice" in listed
    assert "proj-a(rw)" in listed and "proj-b" in listed
    assert token not in listed, "list must never print a secret"


def test_revoke_then_token_dead(monkeypatch):
    from kbcore.config import settings

    monkeypatch.setattr(settings, "kb_principal_cache_ttl", 0)
    out = _run("principal", "create", "bob")
    token = next(ln.strip() for ln in out.splitlines() if ln.startswith("kbg_"))
    assert principals.resolve_token(token) is not None
    _run("principal", "revoke", "bob")
    assert principals.resolve_token(token) is None


def test_rotate_prints_new_secret(monkeypatch):
    from kbcore.config import settings

    monkeypatch.setattr(settings, "kb_principal_cache_ttl", 0)
    out = _run("principal", "create", "carol")
    old = next(ln.strip() for ln in out.splitlines() if ln.startswith("kbg_"))
    rot = _run("principal", "rotate", "carol")
    new = next(ln.strip() for ln in rot.splitlines() if ln.startswith("kbg_"))
    assert new != old
    assert principals.resolve_token(old) is None
    assert principals.resolve_token(new) is not None


def test_create_google_principal_has_no_token():
    out = _run("principal", "create", "web-user", "--email", "user@example.com")
    assert "created google principal" in out
    assert not any(ln.startswith("kbg_") for ln in out.splitlines())
    _run("principal", "grant", "web-user", "proj-x")
    assert principals.resolve_email("user@example.com") is not None

"""kbweb.views_backup — the owner-only web backup-setup surface (2026-07-21).

Owner-override of the read-only web: mutating HOST config, never knowledge.
Proves the security contract end-to-end: the owner-only byte-404 wall for every
non-owner (grantee/guest/anon), CSRF + same-origin on every POST, and — the whole
point — that NO secret is ever echoed: the rclone credential is write-only and
never re-renders, and the age PRIVATE key is shown EXACTLY ONCE on the keygen
response and never again. Real DB in kb_test; Ollama mocked by conftest.
"""

from __future__ import annotations

import json
import logging
import re
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from kbcore import offsite_config, principals
from kbcore.config import settings
from kbcore.ingest import ingest
from tests.test_web_caps import assert_ghost_identical

TEST_TOKEN = "kbtest-" + "0" * 32
STUB_RECIPIENT = "age1" + "q" * 58
STUB_IDENTITY = "AGE-SECRET-KEY-1" + "A" * 58
CRED_MARKER = "RCLONE-CRED-SECRET-MARKER-DO-NOT-ECHO"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    stub = tmp_path / "age-keygen-stub.sh"
    stub.write_text(
        f"#!/usr/bin/env bash\ncat <<'EOF'\n# public key: {STUB_RECIPIENT}\n{STUB_IDENTITY}\nEOF\n"
    )
    stub.chmod(0o755)
    monkeypatch.setattr(settings, "kb_offsite_conf", str(tmp_path / "offsite.env"))
    monkeypatch.setattr(settings, "kb_rclone_conf_dir", str(tmp_path / "rclone"))
    monkeypatch.setattr(settings, "kb_backups", tmp_path / "backups")
    monkeypatch.setattr(settings, "kb_age_bin", str(stub))
    return SimpleNamespace(conf=tmp_path / "offsite.env", rclone_dir=tmp_path / "rclone")


@pytest.fixture()
def app(kb, env, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbweb.server import build_app

    return build_app()


@pytest.fixture()
def owner(app):
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/login", data={"token": TEST_TOKEN}).status_code == 200
    return c


def _grantee_client(app, name: str = "mallory", projects: tuple[str, ...] = ()) -> TestClient:
    _pid, token = principals.create_principal(name, kind="token")
    for p in projects:
        principals.grant(name, p, rw=False)
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/login", data={"token": token}).status_code == 200
    return c


def _csrf_from(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "the backups page must mint a CSRF token"
    return m.group(1)


# --- owner GET ----------------------------------------------------------------


def test_owner_sees_warning_and_masked_state(owner):
    r = owner.get("/backups")
    assert r.status_code == 200
    # The prominent residual-risk warning is present and states the posture plainly.
    assert "PRIVATE key does NOT live here" in r.text
    assert "only ENCRYPTED backups" in r.text
    assert "rclone credential DOES live here" in r.text
    # forms are present; fresh install shows 'not set'
    assert 'action="/backups/config"' in r.text
    assert 'action="/backups/keygen"' in r.text
    assert 'action="/backups/rclone"' in r.text
    assert "not set" in r.text


def test_backups_nav_link_is_owner_only(app):
    ingest("proj-a", "note", "seed", "seed body")
    o = TestClient(app, base_url="https://testserver")
    assert o.post("/login", data={"token": TEST_TOKEN}).status_code == 200
    assert 'href="/backups"' in o.get("/").text, "owner nav shows the backups link"
    g = _grantee_client(app, projects=("proj-a",))
    assert "/backups" not in g.get("/").text, "backups chrome leaked to a grantee"


# --- the non-owner wall (no oracle) -------------------------------------------


def test_grantee_backups_is_byte_identical_404(app):
    ingest("proj-a", "note", "seed", "seed body")
    g = _grantee_client(app, projects=("proj-a",))
    assert g.get("/no-such-path-xyz").status_code == 404
    for path in (
        "/backups",
        "/backups/config",
        "/backups/keygen",
        "/backups/rclone",
        "/backups/x/y",
    ):
        assert_ghost_identical(g, path)


def test_anon_backups_redirects_to_login(app):
    c = TestClient(app, base_url="https://testserver")
    for path, method in (
        ("/backups", "get"),
        ("/backups/config", "post"),
        ("/backups/keygen", "post"),
    ):
        r = getattr(c, method)(path, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/login"


# --- CSRF / same-origin -------------------------------------------------------


def test_post_config_without_csrf_is_403(owner):
    assert owner.post("/backups/config", data={"keep": "30"}).status_code == 403
    assert owner.post("/backups/config", data={"keep": "30", "csrf": "garbage"}).status_code == 403


def test_cross_origin_post_rejected_even_with_valid_csrf(owner):
    csrf = _csrf_from(owner.get("/backups").text)
    r = owner.post(
        "/backups/config",
        data={"enabled": "on", "rclone_remote": "gdrive:kb", "keep": "30", "csrf": csrf},
        headers={"origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403


# --- config round trip --------------------------------------------------------


def test_post_config_saves_and_renders_masked(owner):
    csrf = _csrf_from(owner.get("/backups").text)
    r = owner.post(
        "/backups/config",
        data={
            "enabled": "on",
            "rclone_remote": "gdrive:kb-backups",
            "age_recipient": STUB_RECIPIENT,
            "keep": "14",
            "csrf": csrf,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/backups"
    status = offsite_config.read_status()
    assert status["enabled"] is True
    assert status["rclone_remote"] == "gdrive:kb-backups"
    assert status["keep"] == 14
    page = owner.get("/backups").text
    assert "gdrive:kb-backups" in page and "enabled" in page


def test_post_config_invalid_rerenders_400_with_field_message(owner):
    csrf = _csrf_from(owner.get("/backups").text)
    r = owner.post(
        "/backups/config",
        data={"rclone_remote": "bad remote; rm -rf /", "keep": "30", "csrf": csrf},
    )
    assert r.status_code == 400
    assert "rclone_remote" in r.text


# --- age keypair: shown ONCE, never again, never stored -----------------------


def test_keygen_shows_private_key_once_and_never_again(owner, env):
    csrf = _csrf_from(owner.get("/backups").text)
    r = owner.post("/backups/keygen", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 200
    assert STUB_IDENTITY in r.text, "the private identity must be shown exactly once here"
    assert STUB_RECIPIENT in r.text
    # A subsequent GET /backups NEVER shows the private key again...
    page = owner.get("/backups").text
    assert STUB_IDENTITY not in page, "the private key must never be shown again"
    assert STUB_RECIPIENT in page, "the PUBLIC recipient was stored and may show"
    # ...and it was never persisted to any file.
    assert "AGE-SECRET-KEY" not in env.conf.read_text()
    assert STUB_IDENTITY not in env.conf.read_text()


# --- rclone credential: write-only, never re-rendered -------------------------


def test_rclone_paste_is_write_only_and_never_re_renders(owner, env):
    cred = json.dumps({"type": "service_account", "private_key": CRED_MARKER})
    csrf = _csrf_from(owner.get("/backups").text)
    r = owner.post(
        "/backups/rclone", data={"rclone_cred": cred, "csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 303
    # Stored write-only on disk...
    assert CRED_MARKER in (env.rclone_dir / "rclone.conf").read_text()
    # ...but NEVER echoed back on the page (masked as configured).
    page = owner.get("/backups").text
    assert CRED_MARKER not in page
    assert "configured" in page


# --- secret-leak trip-wire ----------------------------------------------------


def test_no_secret_is_logged_during_posts(owner, env, caplog):
    caplog.set_level(logging.DEBUG)
    csrf = _csrf_from(owner.get("/backups").text)
    owner.post("/backups/keygen", data={"csrf": csrf}, follow_redirects=False)
    csrf = _csrf_from(owner.get("/backups").text)
    cred = json.dumps({"type": "service_account", "private_key": CRED_MARKER})
    owner.post("/backups/rclone", data={"rclone_cred": cred, "csrf": csrf}, follow_redirects=False)
    assert STUB_IDENTITY not in caplog.text, "the age private key must never be logged"
    assert CRED_MARKER not in caplog.text, "the rclone credential must never be logged"

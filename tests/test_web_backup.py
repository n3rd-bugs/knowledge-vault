"""kbweb.views_backup — the owner-only, EMAIL-CENTRIC web backup surface (2026-07-21).

Owner-override of the read-only web: mutating HOST config, never knowledge. Proves
the security contract end-to-end for the radically-simplified one-click surface: the
owner-only byte-404 wall for every non-owner (grantee/guest/anon), CSRF + same-origin
on every POST, and — the whole point — that NO secret is ever echoed. The Drive
refresh token is written WRITE-ONLY into rclone.conf and never re-renders; the age
PRIVATE recovery key is DOWNLOADED (attachment, no-store) and never rendered/stored;
a NON-OWNER can never connect a Drive. Real DB in kb_test; Ollama mocked by conftest;
the Google token exchange is monkeypatched (no network).
"""

from __future__ import annotations

import logging
import re
import stat
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from kbcore import offsite_config, principals
from kbcore.config import settings
from kbcore.ingest import ingest
from kbweb import auth
from tests.test_web_caps import assert_ghost_identical

TEST_TOKEN = "kbtest-" + "0" * 32
STUB_RECIPIENT = "age1" + "q" * 58
STUB_IDENTITY = "AGE-SECRET-KEY-1" + "A" * 58
REFRESH_MARKER = "REFRESH-TOKEN-SECRET-MARKER-DO-NOT-ECHO"
OWNER_EMAIL = "owner@example.com"
PUBLIC_URL = "https://web.example.com"


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
    # The Drive connect reuses the sign-in OAuth client + redirect_uri.
    monkeypatch.setattr(settings, "kb_web_public_url", PUBLIC_URL)
    monkeypatch.setattr(settings, "google_client_id", "test-client-id.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_client_secret", "test-client-secret")
    monkeypatch.setattr(settings, "kb_web_allowed_emails", OWNER_EMAIL)
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


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _drive_token(email: str) -> dict:
    """The shape auth.exchange_google_drive_code returns (mocked in tests)."""
    return {
        "access_token": "ya29.access-token",
        "token_type": "Bearer",
        "refresh_token": REFRESH_MARKER,
        "expiry": "2026-07-21T12:00:00Z",
        "email": email,
    }


def _connect_drive(client: TestClient, monkeypatch, email: str = OWNER_EMAIL):
    """Drive one-click connect: start it (state cookie set), then complete the
    callback with a mocked token exchange. Returns the callback response."""
    r = client.get("/backups/connect", follow_redirects=False)
    assert r.status_code == 302
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    monkeypatch.setattr(auth, "exchange_google_drive_code", lambda code: _drive_token(email))
    return client.get(
        "/auth/google/callback", params={"state": state, "code": "good"}, follow_redirects=False
    )


# --- owner GET: the simplified email-centric page -----------------------------


def test_owner_sees_simplified_email_centric_page(owner):
    r = owner.get("/backups")
    assert r.status_code == 200
    # shortened, plain-language residual-risk callout
    assert "encrypted backups go to your Google Drive" in r.text
    assert "keep your downloaded recovery key" in r.text.lower()
    # the plain actions (the enable toggle form appears only once prerequisites
    # are met — a fresh install shows the guard message instead)
    assert 'href="/backups/connect"' in r.text
    assert 'action="/backups/keygen"' in r.text
    # fresh install status
    assert "not set" in r.text  # recovery key not set
    assert "Connect Drive and download your recovery key first" in r.text  # enable guard
    # the OLD paste box + rclone/age internals are gone
    assert 'action="/backups/rclone"' not in r.text
    assert "rclone" not in r.text.lower()
    assert "age recipient" not in r.text.lower()


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
        "/backups/connect",
        "/backups/config",
        "/backups/keygen",
        "/backups/x/y",
    ):
        assert_ghost_identical(g, path)


def test_anon_backups_redirects_to_login(app):
    c = TestClient(app, base_url="https://testserver")
    for path, method in (
        ("/backups", "get"),
        ("/backups/connect", "get"),
        ("/backups/config", "post"),
        ("/backups/keygen", "post"),
    ):
        r = getattr(c, method)(path, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/login"


# --- CSRF / same-origin -------------------------------------------------------


def test_post_config_without_csrf_is_403(owner):
    assert owner.post("/backups/config", data={"enabled": "on"}).status_code == 403
    assert (
        owner.post("/backups/config", data={"enabled": "on", "csrf": "garbage"}).status_code == 403
    )


def test_post_keygen_without_csrf_is_403(owner):
    assert owner.post("/backups/keygen", data={}).status_code == 403


def test_cross_origin_post_rejected_even_with_valid_csrf(owner):
    csrf = _csrf_from(owner.get("/backups").text)
    r = owner.post(
        "/backups/config",
        data={"enabled": "on", "csrf": csrf},
        headers={"origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403


# --- Google Drive connect: URL, token storage, owner-only guard ---------------


def test_connect_redirects_to_google_with_drive_scope(owner):
    r = owner.get("/backups/connect", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    q = parse_qs(urlsplit(loc).query)
    assert "drive.file" in q["scope"][0], "least-privilege drive.file scope"
    assert q["access_type"] == ["offline"], "offline → refresh_token"
    assert q["prompt"] == ["consent"], "force consent so a refresh_token is issued"
    assert q["redirect_uri"] == [f"{PUBLIC_URL}/auth/google/callback"], "reuses the sign-in URI"
    assert auth.STATE_COOKIE in r.cookies, "the signed state cookie is set"


def test_drive_connect_writes_token_and_sets_email(owner, env, monkeypatch):
    r = _connect_drive(owner, monkeypatch, OWNER_EMAIL)
    assert r.status_code == 303 and r.headers["location"] == "/backups?connected=1"
    conf_file = env.rclone_dir / "rclone.conf"
    assert conf_file.exists() and _mode(conf_file) == 0o600, "rclone.conf write-only, 0600"
    text = conf_file.read_text()
    assert "[gdrive]" in text and "scope = drive.file" in text, "the drive remote is written"
    assert REFRESH_MARKER in text, "the token rides in the write-only rclone.conf"
    status = offsite_config.read_status()
    assert status["drive_connected"] is True
    assert status["drive_email"] == OWNER_EMAIL
    assert status["rclone_remote"] == "gdrive:kb-backups", "remote auto-set — owner never sees it"
    # the token / refresh_token NEVER surfaces via status or any page
    assert REFRESH_MARKER not in str(status)
    page = owner.get("/backups").text
    assert OWNER_EMAIL in page, "the connected account email is shown"
    assert REFRESH_MARKER not in page, "the refresh token must never render"


def test_drive_connect_via_allowlisted_email_without_prior_session(owner, env, monkeypatch):
    # The 'authenticated email is the allowlisted owner' branch: even with no owner
    # session cookie, connecting the allowlisted account succeeds + signs the owner in.
    r0 = owner.get("/backups/connect", follow_redirects=False)
    state = parse_qs(urlsplit(r0.headers["location"]).query)["state"][0]
    owner.cookies.delete(auth.COOKIE)  # drop the owner session
    monkeypatch.setattr(auth, "exchange_google_drive_code", lambda code: _drive_token(OWNER_EMAIL))
    r = owner.get(
        "/auth/google/callback", params={"state": state, "code": "good"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/backups?connected=1"
    assert r.cookies.get(auth.COOKIE), "an owner session is issued"
    assert offsite_config.read_status()["drive_connected"] is True


def test_drive_connect_callback_rejects_non_owner(owner, env, monkeypatch):
    # A valid drive state cookie (owner started the connect), but NO owner session
    # and a NON-allowlisted Google email → the owner guard rejects; no token written.
    r0 = owner.get("/backups/connect", follow_redirects=False)
    state = parse_qs(urlsplit(r0.headers["location"]).query)["state"][0]
    owner.cookies.delete(auth.COOKIE)
    monkeypatch.setattr(
        auth, "exchange_google_drive_code", lambda code: _drive_token("mallory@evil.com")
    )
    r = owner.get(
        "/auth/google/callback", params={"state": state, "code": "good"}, follow_redirects=False
    )
    assert r.status_code == 403, "a non-owner must not be able to connect a Drive"
    assert r.cookies.get(auth.COOKIE) is None, "no owner session issued"
    assert not (env.rclone_dir / "rclone.conf").exists(), "no token was written"
    assert offsite_config.read_status()["drive_connected"] is False


# --- age recovery key: DOWNLOADED, never rendered, never stored ---------------


def test_keygen_returns_download_and_never_renders_key(owner, env):
    csrf = _csrf_from(owner.get("/backups").text)
    r = owner.post("/backups/keygen", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.headers["content-disposition"] == 'attachment; filename="kb-backup-recovery-key.txt"'
    assert "no-store" in r.headers["cache-control"]
    assert STUB_IDENTITY in r.text, "the private key IS the downloaded body"
    # ...never rendered into any HTML page (a later GET shows only 'set')
    page = owner.get("/backups").text
    assert STUB_IDENTITY not in page, "the private key must never be shown in the page"
    assert "set (downloaded" in page, "the page reports the recovery key is set"
    # ...and it was written to NO file (only the PUBLIC recipient is persisted)
    conf_text = env.conf.read_text()
    assert "AGE-SECRET-KEY" not in conf_text and STUB_IDENTITY not in conf_text
    assert STUB_RECIPIENT in conf_text, "only the public recipient is stored"


# --- the enablement guard -----------------------------------------------------


def test_enable_refused_without_drive_and_key(owner, env):
    csrf = _csrf_from(owner.get("/backups").text)
    r = owner.post("/backups/config", data={"enabled": "on", "csrf": csrf})
    assert r.status_code == 400
    assert "Connect your Google Drive and download your recovery key first" in r.text
    assert offsite_config.read_status()["enabled"] is False, "enable stays false"


def test_enable_succeeds_with_drive_and_key(owner, env, monkeypatch):
    _connect_drive(owner, monkeypatch, OWNER_EMAIL)  # Drive connected
    csrf = _csrf_from(owner.get("/backups").text)
    assert owner.post("/backups/keygen", data={"csrf": csrf}).status_code == 200  # recovery key set
    csrf = _csrf_from(owner.get("/backups").text)
    r = owner.post("/backups/config", data={"enabled": "on", "csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/backups"
    status = offsite_config.read_status()
    assert status["enabled"] is True
    assert status["rclone_remote"] == "gdrive:kb-backups"
    assert status["age_recipient"] == STUB_RECIPIENT


def test_disable_always_allowed(owner, env, monkeypatch):
    # Turning OFF needs no prerequisites (a fresh install can post enabled=off).
    csrf = _csrf_from(owner.get("/backups").text)
    r = owner.post("/backups/config", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert offsite_config.read_status()["enabled"] is False


# --- secret-leak trip-wire ----------------------------------------------------


def test_no_secret_is_logged_during_flows(owner, env, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _connect_drive(owner, monkeypatch, OWNER_EMAIL)
    csrf = _csrf_from(owner.get("/backups").text)
    owner.post("/backups/keygen", data={"csrf": csrf}, follow_redirects=False)
    assert REFRESH_MARKER not in caplog.text, "the Drive refresh token must never be logged"
    assert STUB_IDENTITY not in caplog.text, "the age private key must never be logged"

"""Machines DISCOVER the notification channel; the owner never pastes a token.

A credential typed into a session is captured by the very flush hooks being
installed and lands in the KB — an ntfy admin password leaked into a transcript
that way on 2026-07-31 and had to be rotated. Discovery over an authenticated
endpoint is the fix, and what it hands out is deliberately weak: a write-only
token scoped to one topic.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kbcore.config import settings
from kbrest.server import create_app


@pytest.fixture()
def client(kb):
    return TestClient(create_app())


def _auth() -> dict:
    return {"Authorization": f"Bearer {settings.kb_token}"}


def test_unconfigured_deployments_get_a_404_not_a_broken_file(client, monkeypatch):
    """A deployment that wants no notifications must not have installers writing
    empty or half-formed config."""
    monkeypatch.setattr(settings, "kb_notify_url", "")
    monkeypatch.setattr(settings, "kb_notify_topic", "")
    assert client.get("/api/setup/notify.env", headers=_auth()).status_code == 404


def test_a_configured_channel_is_served_as_a_usable_env_file(client, monkeypatch):
    monkeypatch.setattr(settings, "kb_notify_url", "https://ntfy.example")
    monkeypatch.setattr(settings, "kb_notify_topic", "claude-abc")
    monkeypatch.setattr(settings, "kb_notify_token", "tk_write_only")

    body = client.get("/api/setup/notify.env", headers=_auth()).text
    got = dict(
        line.split("=", 1) for line in body.splitlines() if line and not line.startswith("#")
    )
    assert got["NTFY_URL"] == "https://ntfy.example"
    assert got["NTFY_TOPIC"] == "claude-abc"
    assert got["NTFY_TOKEN"] == "tk_write_only"
    # 0 = every completion: a private single-subscriber channel, where a missed
    # completion costs more than an extra ping.
    assert got["NTFY_MIN_SECONDS"] == "0"


def test_the_topic_is_never_invented_by_the_client(client, monkeypatch):
    """A machine that guesses its own topic publishes into a channel nobody is
    subscribed to — which is indistinguishable from 'notifications are broken'.
    The server is the single source of truth for which topic to use."""
    monkeypatch.setattr(settings, "kb_notify_url", "https://ntfy.example")
    monkeypatch.setattr(settings, "kb_notify_topic", "the-one-true-topic")
    body = client.get("/api/setup/notify.env", headers=_auth()).text
    assert "NTFY_TOPIC=the-one-true-topic" in body


def test_no_admin_credential_is_ever_served(client, monkeypatch):
    """Only the write-only token may leave the host. The admin password must not
    appear even if it is set in the environment."""
    monkeypatch.setattr(settings, "kb_notify_url", "https://ntfy.example")
    monkeypatch.setattr(settings, "kb_notify_topic", "claude-abc")
    monkeypatch.setattr(settings, "kb_notify_token", "tk_write_only")
    monkeypatch.setattr(settings, "kb_token", settings.kb_token)

    body = client.get("/api/setup/notify.env", headers=_auth()).text
    assert "NTFY_ADMIN_PASSWORD" not in body
    assert "NTFY_PUBLISH_TOKEN" not in body  # the server-side name never leaks
    assert body.count("tk_") == 1, "more than one credential in the payload"


def test_it_is_gated_like_the_other_setup_assets(client, monkeypatch):
    monkeypatch.setattr(settings, "kb_notify_url", "https://ntfy.example")
    monkeypatch.setattr(settings, "kb_notify_topic", "claude-abc")
    assert (
        client.get("/api/setup/notify.env").status_code
        == client.get("/api/setup/install.sh").status_code
    )

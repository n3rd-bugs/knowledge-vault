"""OAuth token-lifetime decoupling (the 'KB loses authentication quickly' fix).

_build_auth must pass kb_oauth_token_ttl through to the GoogleProvider as
fastmcp_access_token_expiry_seconds so the FastMCP-issued token outlives
Google's ~1h upstream expiry. (The companion fix — persisting the OAuth state
dir via the fastmcp-oauth volume — is compose-level and verified at rollout.)
"""

from __future__ import annotations

from kbcore.config import settings

TEST_TOKEN = "kbtest-" + "0" * 32


def test_build_auth_passes_token_ttl(monkeypatch):
    monkeypatch.setattr(settings, "kb_oauth_enabled", True)
    monkeypatch.setattr(settings, "google_client_id", "cid.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_client_secret", "GOCSPX-" + "0" * 20)
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    # kb_public_url now defaults to "" (no hardcoded host); OAuth requires it set.
    monkeypatch.setattr(settings, "kb_public_url", "https://kb.example.com")
    monkeypatch.setattr(settings, "kb_oauth_token_ttl", 12_345)

    from kbmcp.server import _build_auth

    auth = _build_auth()
    assert auth is not None
    # MultiAuth wraps the provider; find the OAuthProxy layer and check the ttl.
    provider = getattr(auth, "server", None) or getattr(auth, "_server", None) or auth
    ttl = getattr(provider, "_fastmcp_access_token_expiry_seconds", None) or getattr(
        provider, "fastmcp_access_token_expiry_seconds", None
    )
    assert ttl == 12_345, f"ttl not passed through (got {ttl!r})"

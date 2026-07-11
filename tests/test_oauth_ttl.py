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


def _oauth_settings(monkeypatch):
    monkeypatch.setattr(settings, "kb_oauth_enabled", True)
    monkeypatch.setattr(settings, "google_client_id", "cid.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_client_secret", "GOCSPX-" + "0" * 20)
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_public_url", "https://kb.example.com")


def test_build_auth_sets_proactive_refresh_threshold(monkeypatch):
    """token_expiry_threshold_seconds > 0 so a token refreshes BEFORE it expires,
    avoiding a mid-flight expiry that surfaces to claude.ai as a spurious logout."""
    _oauth_settings(monkeypatch)
    from kbmcp.server import _build_auth

    auth = _build_auth()
    provider = getattr(auth, "server", None) or getattr(auth, "_server", None) or auth
    threshold = getattr(provider, "_token_expiry_threshold_seconds", None)
    if threshold is None:
        threshold = getattr(provider, "token_expiry_threshold_seconds", None)
    assert threshold and threshold > 0, f"proactive refresh threshold not set (got {threshold!r})"


def test_build_auth_cimd_follows_config_flag(monkeypatch):
    """enable_cimd is wired to KB_OAUTH_CIMD (owner A/Bs DCR vs CIMD via .env)."""
    _oauth_settings(monkeypatch)
    from fastmcp.server.auth.providers import google as google_mod

    import kbmcp.server as srv

    captured: dict = {}
    real_init = google_mod.GoogleProvider.__init__

    def spy_init(self, *args, **kwargs):
        captured["enable_cimd"] = kwargs.get("enable_cimd")
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(google_mod.GoogleProvider, "__init__", spy_init)

    monkeypatch.setattr(settings, "kb_oauth_cimd", False)
    srv._build_auth()
    assert captured["enable_cimd"] is False

    monkeypatch.setattr(settings, "kb_oauth_cimd", True)
    srv._build_auth()
    assert captured["enable_cimd"] is True

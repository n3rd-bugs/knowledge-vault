"""The setup manifest — how a remote box learns its hooks are out of date.

A machine cannot otherwise tell: it compares what it has against whatever the
server hands out, so a server that was never rebuilt makes every box report
itself current. That happened on 2026-07-31 — a remote machine matched all four
files byte-for-byte against a stale image and concluded there was nothing to do,
while the fix it needed sat unshipped.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from kbcore.config import settings
from kbrest.server import _SETUP_ASSETS, _SETUP_SCRIPTS, create_app


@pytest.fixture()
def client(kb):
    return TestClient(create_app())


def _auth() -> dict:
    return {"Authorization": f"Bearer {settings.kb_token}"}


def test_the_manifest_is_not_swallowed_by_the_script_route(client):
    """'/api/setup/{name}' is declared for arbitrary names; if it were matched
    first, manifest.json would 404 as an unknown script."""
    r = client.get("/api/setup/manifest.json", headers=_auth())
    assert r.status_code == 200, "the catch-all route shadowed the manifest"
    assert "version" in r.json()


def test_every_served_script_is_listed_with_its_real_digest(client):
    files = client.get("/api/setup/manifest.json", headers=_auth()).json()["files"]
    assert set(files) == {n for n in _SETUP_SCRIPTS if (_SETUP_ASSETS / n).exists()}
    for name, digest in files.items():
        real = hashlib.sha256((_SETUP_ASSETS / name).read_bytes()).hexdigest()
        assert digest == real, f"{name} digest does not match the served file"


def test_the_notification_hooks_are_actually_servable(client):
    """They shipped in the skill but were missing from the allowlist, so a remote
    box could never fetch them — the whole reason notifications only ever worked
    on the owner's machine."""
    files = client.get("/api/setup/manifest.json", headers=_auth()).json()["files"]
    assert "notify-complete.sh" in files
    assert "turn-start.sh" in files
    for n in ("notify-complete.sh", "turn-start.sh"):
        assert client.get(f"/api/setup/{n}", headers=_auth()).status_code == 200


def test_the_version_changes_when_any_asset_changes(client, monkeypatch, tmp_path):
    """A version derived from CONTENT cannot go stale; one a human must bump can,
    and the staleness is invisible."""
    first = client.get("/api/setup/manifest.json", headers=_auth()).json()["version"]
    assert first == client.get("/api/setup/manifest.json", headers=_auth()).json()["version"], (
        "version is not stable for unchanged content"
    )

    target = _SETUP_ASSETS / "install.sh"
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\n# version probe\n")
        second = client.get("/api/setup/manifest.json", headers=_auth()).json()["version"]
    finally:
        target.write_bytes(original)
    assert second != first, "editing a shipped asset did not change the version"


def test_the_manifest_is_no_more_permissive_than_the_scripts(client):
    """Bearer enforcement lives in middleware this harness does not exercise
    (/api/stats is equally open here), so asserting an absolute 401 would be
    asserting the harness, not the app. What IS meaningful and testable: the
    manifest must not be reachable under conditions the scripts are not — it
    exposes the same names plus their digests, nothing more."""
    assert (
        client.get("/api/setup/manifest.json").status_code
        == client.get("/api/setup/install.sh").status_code
    ), "the manifest is gated differently from the scripts it describes"


def test_the_manifest_leaks_nothing_beyond_names_and_digests(client):
    body = client.get("/api/setup/manifest.json", headers=_auth()).json()
    assert set(body) == {"version", "files", "count"}
    for digest in body["files"].values():
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)

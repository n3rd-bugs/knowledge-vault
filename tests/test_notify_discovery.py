"""Machines DISCOVER the notification channel; the owner never pastes a token.

A credential typed into a session is captured by the very flush hooks being
installed and lands in the KB — an ntfy admin password leaked into a transcript
that way on 2026-07-31 and had to be rotated. Discovery over an authenticated
endpoint is the fix, and what it hands out is deliberately weak: a write-only
token scoped to one topic.

WEAK IS NOT FREE (2026-08-01). "Authenticated" first meant *any* live credential,
which included every narrow per-project grantee — a read grant on one project
became a credential for the owner's private notification channel, distributed
quietly, and the last rotation showed how expensive quiet distribution is. The
endpoint now answers the OWNER and the bootstrap case it exists for (an
unredeemed enrolment code); everyone else gets the byte-identical 404 an
unconfigured deployment returns, so the refusal reveals nothing about whether a
channel exists at all.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kbcore import enrolment, principals, scope
from kbcore.config import settings
from kbmcp import principal_ctx
from kbrest.server import create_app

OWNER_TOKEN = "kbtest-" + "0" * 32

# A narrow per-project grantee — the identity that used to be handed the publish
# token. Built in memory: authorization is a pure function of the principal.
GRANTEE = scope.Principal(
    name="alpha-reader",
    is_owner=False,
    read_projects=frozenset({"alpha"}),
    write_projects=frozenset({"alpha"}),
    kind="token",
    role="contributor",
)


@pytest.fixture()
def client(kb):
    return TestClient(create_app())


@pytest.fixture()
def configured(monkeypatch):
    """The app with a channel configured, no DB and no bearer layer — so a test
    can bind any principal and see exactly what that identity is served."""
    monkeypatch.setattr(settings, "kb_notify_url", "https://ntfy.example")
    monkeypatch.setattr(settings, "kb_notify_topic", "claude-abc")
    monkeypatch.setattr(settings, "kb_notify_token", "tk_write_only")
    return TestClient(create_app())


@pytest.fixture()
def served(kb, monkeypatch):
    """The REST app behind its real bearer middleware, so a request authenticates
    exactly as it does in production (the gate under test is authorization, and
    testing it with the auth layer removed would test nothing)."""
    monkeypatch.setattr(settings, "kb_token", OWNER_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    monkeypatch.setattr(settings, "kb_web_public_url", "")
    monkeypatch.setattr(settings, "kb_notify_url", "https://ntfy.example")
    monkeypatch.setattr(settings, "kb_notify_topic", "claude-abc")
    monkeypatch.setattr(settings, "kb_notify_token", "tk_write_only")
    from kbrest.server import build_app

    return TestClient(build_app())


def _auth() -> dict:
    return {"Authorization": f"Bearer {settings.kb_token}"}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


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


# --- who may fetch it (2026-08-01) --------------------------------------------


def test_the_owner_gets_the_channel(served):
    r = served.get("/api/setup/notify.env", headers=_bearer(OWNER_TOKEN))
    assert r.status_code == 200
    assert "NTFY_TOPIC=claude-abc" in r.text


def test_an_enrolling_machine_gets_it_because_that_is_what_it_is_for(served):
    """The bootstrap case: a box mid-install holds an unredeemed enrolment code
    and nothing else. Refusing it here would put the token back in a chat reply,
    which is the leak this endpoint was built to end."""
    with scope.use_principal(scope.OWNER):
        issued = enrolment.create_code(project="newbox")

    r = served.get("/api/setup/notify.env", headers=_bearer(issued.code))
    assert r.status_code == 200
    assert "NTFY_TOKEN=tk_write_only" in r.text
    # …and the GET must not have spent the one-time code (only POST /api/enrol does).
    assert enrolment.is_live_code(issued.code)


def test_a_project_grantee_is_refused_and_told_nothing(configured, monkeypatch):
    """A narrow per-project credential has no business holding the owner's
    publish token. It gets the SAME response an unconfigured deployment gives —
    byte-identical, so it cannot even learn that a channel exists.

    Bound through :func:`kbcore.scope.use_principal` rather than a DB row: the
    property under test is authorization, and a principal is the whole input to
    it (the same transport-independent style as test_a11_3_write_holes)."""
    with scope.use_principal(GRANTEE):
        refused = configured.get("/api/setup/notify.env")
    assert refused.status_code == 404

    # The no-oracle comparison: what an unconfigured deployment returns the owner.
    monkeypatch.setattr(settings, "kb_notify_url", "")
    monkeypatch.setattr(settings, "kb_notify_topic", "")
    unconfigured = configured.get("/api/setup/notify.env")

    assert unconfigured.status_code == refused.status_code
    assert unconfigured.content == refused.content, "the refusal is distinguishable"


def test_the_refusal_never_leaks_the_channel(configured):
    with scope.use_principal(GRANTEE):
        body = configured.get("/api/setup/notify.env").text
    for leaked in ("ntfy.example", "claude-abc", "tk_write_only", "NTFY_"):
        assert leaked not in body


def test_an_authenticated_but_ungranted_identity_is_refused_too(configured):
    """The Google-sign-in walled identity (no projects, no caps) is not the
    bootstrap case either — only an unredeemed enrolment code is."""
    walled = scope.Principal(name="google:someone@example.com", is_owner=False)
    with scope.use_principal(walled):
        assert configured.get("/api/setup/notify.env").status_code == 404


def test_the_bootstrap_identity_is_the_only_non_owner_that_passes(configured):
    """Guards the shape of the exception: it is the ENROLLING identity, not
    'any principal with no grants' and not a name a caller can choose."""
    enrolling = scope.Principal(name=principal_ctx.ENROLLING_NAME, is_owner=False)
    with scope.use_principal(enrolling):
        assert configured.get("/api/setup/notify.env").status_code == 200


def test_an_unknown_credential_still_gets_the_bearer_401(served):
    """Authorization narrowing must not weaken authentication: an unknown token
    is refused by the bearer gate before this route is reached at all."""
    assert served.get("/api/setup/notify.env", headers=_bearer("nope")).status_code == 401


def test_the_hook_scripts_stay_reachable_for_every_credential(served):
    """Only the CHANNEL is owner/bootstrap-gated. The scripts are not secret, and
    a grantee's machine still has to be able to install its hooks."""
    with scope.use_principal(scope.OWNER):
        _pid, token = principals.create_principal("notify-grantee-3", kind="token")
        principals.grant("notify-grantee-3", "alpha", rw=False)

    assert served.get("/api/setup/install.sh", headers=_bearer(token)).status_code == 200
    assert served.get("/api/setup/notify.env", headers=_bearer(token)).status_code == 404


def test_it_is_gated_like_the_other_setup_assets(client, monkeypatch):
    monkeypatch.setattr(settings, "kb_notify_url", "https://ntfy.example")
    monkeypatch.setattr(settings, "kb_notify_topic", "claude-abc")
    assert (
        client.get("/api/setup/notify.env").status_code
        == client.get("/api/setup/install.sh").status_code
    )


# --- the kbe_ laundering path (Wave-2 review #2) -------------------------------
# ANY grantee can mint an enrolment code (kb_setup has no owner gate), and that
# code AUTHENTICATES as an enrolling machine. Before the sentinel split it then
# resolved to the SAME identity the notify gate admits, so a read grant on one
# project could pull the owner's ntfy publish token. It now resolves to a DISTINCT
# sentinel (ENROLLING_GRANTEE_NAME) that the gate refuses.


def test_a_grantee_issued_enrolment_code_cannot_launder_the_channel(served, monkeypatch):
    """End-to-end through the real bearer layer: a code minted UNDER A GRANTEE is
    refused notify.env with the byte-identical unknown-path 404, while an
    owner-issued code (the true bootstrap identity) still passes."""
    with scope.use_principal(scope.OWNER):
        _pid, gtoken = principals.create_principal("launderer", kind="token")
        principals.grant("launderer", "alpha", rw=False)
        owner_code = enrolment.create_code(project="newbox")
    grantee = principals.resolve_token(gtoken)
    with scope.use_principal(grantee):
        grantee_code = enrolment.create_code(project="alpha")

    refused = served.get("/api/setup/notify.env", headers=_bearer(grantee_code.code))
    assert refused.status_code == 404

    ok = served.get("/api/setup/notify.env", headers=_bearer(owner_code.code))
    assert ok.status_code == 200 and "NTFY_TOKEN=tk_write_only" in ok.text

    # The grantee refusal is byte-identical to what an unconfigured deployment
    # returns the owner — no oracle that a channel (or the grantee code) exists.
    monkeypatch.setattr(settings, "kb_notify_url", "")
    monkeypatch.setattr(settings, "kb_notify_topic", "")
    unconfigured = served.get("/api/setup/notify.env", headers=_bearer(OWNER_TOKEN))
    assert unconfigured.status_code == refused.status_code
    assert unconfigured.content == refused.content, "the grantee refusal is distinguishable"


def test_the_grantee_enrolling_sentinel_is_refused(configured):
    """The grantee-issued enrolment sentinel is NOT the bootstrap identity — it
    carries only a grantee's authority and must not reach the owner's channel."""
    grantee_enrolling = scope.Principal(name=principal_ctx.ENROLLING_GRANTEE_NAME, is_owner=False)
    with scope.use_principal(grantee_enrolling):
        assert configured.get("/api/setup/notify.env").status_code == 404


def test_resolve_bearer_splits_the_enrolling_sentinel_by_issuer(kb):
    """resolve_bearer maps the enrolling identity by WHO ISSUED the code: an
    owner-issued code -> ENROLLING_NAME (passes notify.env); a grantee-issued one
    -> ENROLLING_GRANTEE_NAME (refused). Both stay zero-authority and both are
    recognised as enrolling, so each still reaches /api/enrol + /api/setup."""
    with scope.use_principal(scope.OWNER):
        _pid, gtoken = principals.create_principal("sentinel-grantee", kind="token")
        principals.grant("sentinel-grantee", "alpha", rw=False)
        owner_code = enrolment.create_code(project="newbox")
    grantee = principals.resolve_token(gtoken)
    with scope.use_principal(grantee):
        grantee_code = enrolment.create_code(project="alpha")

    po = principal_ctx.resolve_bearer("Bearer " + owner_code.code)
    pg = principal_ctx.resolve_bearer("Bearer " + grantee_code.code)
    assert po is not None and po.name == principal_ctx.ENROLLING_NAME
    assert pg is not None and pg.name == principal_ctx.ENROLLING_GRANTEE_NAME
    for p in (po, pg):
        assert not p.read_projects and not p.write_projects and not p.caps
        assert p.name in principal_ctx.ENROLLING_NAMES

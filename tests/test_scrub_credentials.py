"""The scrubber's CLASS tripwire: every credential this system mints, holds or
distributes must be redactable, and adding a new one must FAIL here.

Background (2026-07-31): an ntfy admin password leaked into a session transcript
under `bash -x`, was flushed to the KB by the very hooks being installed, and had
to be rotated — two items still returned plaintext credentials afterwards,
because nothing in ``SECRET_PATTERNS`` matched either the ``tk_…`` token shape or
the ``curl -u user:password`` shape. Patching those two holes fixes the incident;
it does not fix the CLASS, which is "a credential kind exists that the scrubber
has never heard of".

So this file is deliberately written as an ENUMERATION, not a list of examples:

* :data:`CREDENTIAL_KINDS` names every credential kind in the system, each with a
  sample shaped like the real thing (CLAUDE.md: fake tokens shaped like real ones).
* :func:`test_the_registry_covers_every_credential_setting` asserts the registry's
  settings-backed entries are EXACTLY the credential-bearing ``Settings`` fields.
  Add ``kb_slack_webhook_secret`` to config and this test goes red until someone
  proves it is scrubbed — which is the whole point (CLAUDE.md rule 12's scrub
  clause stops being discipline and becomes a mechanism).

No REAL secret is ever handled here: every value is a synthetic sentinel that is
monkeypatched into settings, so a failing assertion cannot print a live
credential into pytest output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

from kbcore import scrub as scrub_mod
from kbcore.config import Settings, settings
from kbcore.scrub import credential_setting_fields, scrub, scrub_tree


@dataclass(frozen=True)
class CredentialKind:
    """One kind of credential, and how the scrubber is expected to catch it.

    ``setting`` names the ``Settings`` field holding it on a deployment that has
    one (caught by exact value, whatever its shape). ``label`` is the redaction
    label expected when the sample is caught by SHAPE instead — which is what
    protects a credential minted for a DIFFERENT machine, quoted in a transcript
    on this one.
    """

    kind: str
    sample: str
    setting: str | None = None
    label: str | None = None


# 'kbg_'/'kbe_' samples use the real prefixes with filler bodies; the ntfy and
# Google samples use their real vendor shapes. None of these are live values.
CREDENTIAL_KINDS: tuple[CredentialKind, ...] = (
    # No label: a master token is `openssl rand -hex 32` — it has no prefix and
    # no shape, so only the config layer can catch it. That is a real limit,
    # written down rather than papered over: a master token from ANOTHER
    # deployment quoted here is not redactable, which is one more reason
    # enrolment hands out scoped kbg_ credentials instead (SHARING.md §2.5).
    CredentialKind(
        kind="master bearer (KB_TOKEN)",
        sample="kbtok-" + "9" * 40,
        setting="kb_token",
    ),
    CredentialKind(
        kind="grantee bearer minted by kbcore.principals",
        sample="kbg_" + "A" * 43,
        label="kb_token",
    ),
    CredentialKind(
        kind="enrolment code minted by kbcore.enrolment",
        sample="kbe_" + "B" * 32,
        label="kb_enrol_code",
    ),
    CredentialKind(
        kind="ntfy publish token distributed to every enrolled machine",
        sample="tk_" + "c" * 24,
        setting="kb_notify_token",
        label="ntfy_token",
    ),
    CredentialKind(
        kind="Postgres password",
        sample="Pg-" + "d" * 30,
        setting="kb_db_password",
    ),
    CredentialKind(
        kind="Google OAuth client secret",
        sample="GOCSPX-" + "e" * 28,
        setting="google_client_secret",
        label="google_secret",
    ),
    CredentialKind(
        kind="web cookie/CSRF signing secret",
        sample="ws-" + "f" * 40,
        setting="kb_web_signing_secret",
    ),
    CredentialKind(
        kind="ntfy admin basic-auth as curl types it (the 2026-07-31 leak)",
        sample="curl -u kbadmin:" + "g" * 20,
        label="basic_auth",
    ),
    CredentialKind(
        kind="basic-auth as the header it becomes",
        sample="Authorization: Basic a2JhZG1pbjpodW50ZXIyaHVudGVyMg==",
        label="auth_header",
    ),
    CredentialKind(
        kind="a password inline in a connection URL",
        sample="postgresql://kb:" + "h" * 20 + "@db.internal:5433/kb",
        label="url_credentials",
    ),
    # No setting: generate_age_keypair RETURNS the identity and persists it
    # NOWHERE (it is shown once), so only its SHAPE can save it if it lands in a
    # transcript — and it decrypts every offsite backup.
    CredentialKind(
        kind="age PRIVATE identity minted by kbcore.offsite_config.generate_age_keypair",
        sample="AGE-SECRET-KEY-1" + "Q" * 55,
        label="age_identity",
    ),
    # No setting: store_drive_token writes the refresh token WRITE-ONLY into
    # rclone.conf (never a Settings field, never read back), so shape is again the
    # only defence for a copy quoted in a session.
    CredentialKind(
        kind="Google OAuth refresh token stored by kbcore.offsite_config.store_drive_token",
        sample="1//0" + "g" * 30,
        label="google_refresh_token",
    ),
    # No setting: a claude.ai user's Google ACCESS token reaches kb-mcp-public on
    # every sign-in and was logged by httpx in the tokeninfo URL (2026-10-28) —
    # it is never ours to configure, so shape is the only defence.
    CredentialKind(
        kind="Google OAuth access token verified by the public MCP",
        sample="ya29." + "a0AfB_" + "k" * 60,
        label="google_access_token",
    ),
)

SETTINGS_BACKED = {k.setting: k for k in CREDENTIAL_KINDS if k.setting}


# --- the tripwire -------------------------------------------------------------


def test_the_registry_covers_every_credential_setting():
    """A new credential in config must be registered here — and proven scrubbable
    by the tests below — before it can ship.

    This is the assertion that converts CLAUDE.md rule 12's "matched by a
    kbcore.scrub pattern so it cannot survive ingest" from a promise a reviewer
    has to remember into a check that runs on every commit.
    """
    from_config = set(credential_setting_fields())
    assert from_config == set(SETTINGS_BACKED), (
        "credential settings and the registry in this file have diverged: "
        f"unregistered={sorted(from_config - set(SETTINGS_BACKED))} "
        f"stale={sorted(set(SETTINGS_BACKED) - from_config)}"
    )


def test_the_field_walk_matches_the_settings_model():
    """The walk must be derived from the model, not hand-listed (a hand-listed
    one is what let kbg_/kbe_/tk_ each be missed in turn)."""
    expected = {
        name
        for name, field in Settings.model_fields.items()
        if re.search(r"(?i)token|password|secret", name) and field.annotation is str
    }
    assert set(credential_setting_fields()) == expected
    # …and an int TTL that merely has 'token' in its name is not a credential.
    assert "kb_oauth_token_ttl" not in credential_setting_fields()


# --- the CLASS tripwire: every MINTER must have a scrub pattern ----------------


def test_every_minter_has_a_scrub_pattern():
    """CLAUDE.md rule 12's class guard for the MINTING side: a kbcore function that
    returns a plaintext secret must have a SECRET_PATTERNS entry that redacts its
    output by shape. Adding a new minter without a pattern fails HERE — not after
    the secret has already shipped to an off-site backup unredacted.

    This is the check that would have caught the age private key and the Google
    refresh token being unredactable (Wave-2 review #2): the age key decrypts
    EVERY backup and is shown-once to the web; the refresh token is minted through
    store_drive_token — yet neither had a shape rule until now."""
    labels = {label for label, _ in scrub_mod.SECRET_PATTERNS}
    missing = {
        minter: label for minter, label in scrub_mod.MINTED_SECRETS.items() if label not in labels
    }
    assert not missing, (
        "these minters return a plaintext secret with no SECRET_PATTERNS rule to "
        f"redact it — CLAUDE.md rule 12 requires one: {missing}"
    )


def test_the_minter_registry_names_real_functions():
    """The registry must reference LIVE kbcore functions — so renaming or removing
    a minter turns the entry stale and fails here, instead of the tripwire quietly
    guarding a function that no longer mints anything."""
    import importlib

    for dotted in scrub_mod.MINTED_SECRETS:
        module_name, _, func = dotted.rpartition(".")
        module = importlib.import_module(module_name)
        assert callable(getattr(module, func, None)), f"{dotted} is not a callable minter"


# --- every kind, planted in text ----------------------------------------------


@pytest.mark.parametrize("cred", CREDENTIAL_KINDS, ids=lambda c: c.kind)
def test_every_credential_kind_is_scrubbed(cred, monkeypatch):
    """Plant the credential in text shaped like the transcripts that leak it."""
    if cred.setting:
        monkeypatch.setattr(settings, cred.setting, cred.sample)
    text = f"I ran this and it worked:\n  {cred.sample}\nthen moved on"
    clean, redactions = scrub(text)
    assert cred.sample not in clean, f"{cred.kind} survives ingest"
    assert redactions, f"{cred.kind} was removed but not COUNTED (meta.redactions)"


@pytest.mark.parametrize("cred", [c for c in CREDENTIAL_KINDS if c.label], ids=lambda c: c.kind)
def test_shape_matching_protects_credentials_this_box_does_not_hold(cred, monkeypatch):
    """A credential minted for ANOTHER machine has no settings entry here, so only
    its SHAPE can save it — which is exactly the case the ntfy token was in when
    it leaked (this host holds the admin account, remote boxes hold the token)."""
    for field in SETTINGS_BACKED:
        monkeypatch.setattr(settings, field, "")  # hold nothing at all
    clean, redactions = scrub(f"the other box uses {cred.sample} for pushes")
    assert cred.sample not in clean
    assert cred.label in redactions, f"expected label {cred.label!r}, got {redactions}"


@pytest.mark.parametrize("field", sorted(SETTINGS_BACKED), ids=lambda f: f)
def test_a_configured_secret_is_redacted_by_VALUE_whatever_its_shape(field, monkeypatch):
    """The shape layer cannot help a db password or a signing secret — they have
    no shape. The config layer redacts them by exact value, labelled with the
    setting they came from so a reviewer can see WHICH secret was in the text."""
    sentinel = f"SNTL-{field}-" + "0" * 24
    monkeypatch.setattr(settings, field, sentinel)
    clean, redactions = scrub(f"the deploy notes said to use {sentinel} here")
    assert sentinel not in clean
    assert redactions.get(field) == 1


def test_secrets_are_scrubbed_in_nested_structures_too(monkeypatch):
    """Ingest scrubs the whole tree (code_ref, meta, tags), not just the body."""
    monkeypatch.setattr(settings, "kb_notify_token", "tk_" + "h" * 24)
    red: dict[str, int] = {}
    cleaned = scrub_tree(
        {"meta": {"cmd": ["curl", "-H", f"Authorization: Bearer tk_{'h' * 24}"]}}, red
    )
    assert "h" * 24 not in str(cleaned)
    assert red


# --- the exact shapes from the 2026-07-31 incident ----------------------------


def test_the_ntfy_token_shape_that_leaked_is_matched():
    clean, red = scrub("export NTFY_TOKEN=tk_0123456789abcdefghij")
    assert "tk_0123456789abcdefghij" not in clean
    assert red == {"ntfy_token": 1}


def test_the_curl_basic_auth_shape_that_leaked_is_matched():
    for line in (
        "curl -u kbadmin:sup3rsecretpw https://ntfy.example/topic",
        "curl --user kbadmin:sup3rsecretpw https://ntfy.example/topic",
        "curl -ukbadmin:sup3rsecretpw https://ntfy.example/topic",
    ):
        clean, red = scrub(line)
        assert "sup3rsecretpw" not in clean, line
        assert red.get("basic_auth") == 1, line


def test_a_password_inline_in_a_url_is_removed_but_the_url_stays_readable():
    """The most common way a db password reaches a transcript. The scheme and
    host survive on purpose — a note that says "the connection to db.internal
    failed" is still worth having; the password is not."""
    clean, red = scrub("psql postgresql://kb:hunter2secret@db.internal:5433/kb failed")
    assert "hunter2secret" not in clean
    assert red == {"url_credentials": 1}
    assert "db.internal:5433/kb" in clean and "postgresql://" in clean


def test_a_plain_url_with_a_port_is_not_mistaken_for_credentials():
    text = "the sidecar listens on http://127.0.0.1:8787/rerank and needs no auth"
    assert scrub(text) == (text, {})


def test_a_url_after_dash_u_is_not_mistaken_for_user_and_password():
    """`-u https://host/topic` is a typo, not a credential — redacting it would
    teach reviewers that [REDACTED] markers mean nothing."""
    text = "curl -u https://ntfy.example/topic -d hi"
    clean, red = scrub(text)
    assert clean == text
    assert red == {}


# --- no collateral damage -----------------------------------------------------


def test_placeholder_and_short_config_values_are_never_redacted(monkeypatch):
    """The shipped defaults are placeholders. Redacting them would mangle prose
    (and .env.example walkthroughs) for no security gain."""
    monkeypatch.setattr(settings, "kb_db_password", "change-me")
    monkeypatch.setattr(settings, "kb_token", "short")
    text = "set KB_DB_PASSWORD to something other than change-me; short values are refused"
    clean, red = scrub(text)
    assert clean == text
    assert red == {}


def test_an_unset_secret_never_matches_the_empty_string(monkeypatch):
    for field in SETTINGS_BACKED:
        monkeypatch.setattr(settings, field, "")
    text = "nothing secret here at all"
    assert scrub(text) == (text, {})


def test_redaction_is_idempotent(monkeypatch):
    """Re-scrubbing already-redacted text must be a no-op — the drain re-scrubs,
    and double-counting redactions would misreport how much a review needs."""
    monkeypatch.setattr(settings, "kb_notify_token", "tk_" + "j" * 24)
    once, first = scrub("token tk_" + "j" * 24 + " and curl -u a:bbbbbbbbbbbb now")
    twice, second = scrub(once)
    assert twice == once
    assert second == {}
    assert first


def test_own_secrets_are_ordered_longest_first(monkeypatch):
    """A secret that CONTAINS another (a signing secret derived from the token,
    say) must be redacted whole, not left as a redacted core plus a live tail."""
    inner = "inner-" + "k" * 20
    outer = inner + "-outer-suffix"
    monkeypatch.setattr(settings, "kb_token", inner)
    monkeypatch.setattr(settings, "kb_web_signing_secret", outer)
    clean, _red = scrub(f"the value is {outer}")
    assert inner not in clean and outer not in clean
    assert "[REDACTED:kb_web_signing_secret]" in clean


def test_the_field_walk_ignores_non_string_settings(monkeypatch):
    """getattr on an int TTL must not crash the ingest chokepoint."""
    monkeypatch.setattr(settings, "kb_oauth_token_ttl", 86_400)
    assert all(isinstance(v, str) for _n, v in scrub_mod.own_secrets())

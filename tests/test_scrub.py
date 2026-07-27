"""Scrubber unit tests (SEC-2). Fake tokens shaped like real ones (CLAUDE.md)."""

from __future__ import annotations

from kbcore.scrub import scrub

FAKE_GHP = "ghp_" + "0" * 36
FAKE_AWS = "AKIA" + "0" * 16


def test_scrubber_redacts_fake_ghp_token():
    text = f"pushed with token {FAKE_GHP} to origin"
    clean, redactions = scrub(text)
    assert FAKE_GHP not in clean
    assert "[REDACTED:github_token]" in clean
    assert redactions == {"github_token": 1}


def test_scrubber_redacts_multiple_kinds():
    text = (
        f"aws {FAKE_AWS} then password=hunter42secret and a jwt "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
    )
    clean, redactions = scrub(text)
    assert FAKE_AWS not in clean
    assert "hunter42secret" not in clean
    assert redactions["aws_key"] == 1
    assert redactions["credential"] == 1
    assert redactions["jwt"] == 1


def test_scrubber_redacts_private_key_block():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIfake\n-----END RSA PRIVATE KEY-----"
    clean, redactions = scrub(text)
    assert "MIIfake" not in clean
    assert redactions == {"private_key": 1}


def test_scrubber_leaves_clean_text_alone():
    text = "SPI mode 1 means CPOL=0 CPHA=1; nothing secret here."
    clean, redactions = scrub(text)
    assert clean == text
    assert redactions == {}


def test_our_own_minted_bearer_is_redacted():
    """A credential this system MINTS must be redactable by this system.

    kbg_ tokens used to survive ingest unless they happened to be written as
    `KB_TOKEN=...` (caught by the generic `credential` rule). A token quoted in
    prose — exactly how a setup flow would hand one to a user — was captured
    verbatim into the KB by the session hooks that flush every transcript.
    """
    from kbcore.scrub import scrub

    tok = "kbg_" + "A" * 43
    for text in (
        tok,
        f"your token is {tok} keep it safe",
        f"KB_TOKEN={tok}",
        f'{{"KB_TOKEN": "{tok}"}}',
        f"export KB_TOKEN='{tok}'",
    ):
        out, red = scrub(text)
        assert tok not in out, f"the minted bearer survived ingest in: {text[:40]!r}"
        assert "REDACTED" in out
        assert sum(red.values()) >= 1

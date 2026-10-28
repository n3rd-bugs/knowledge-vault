"""kbcore.scrub — secret redaction on every ingest (SEC-2, PLAN.md §5.3).

Matches are replaced with ``[REDACTED:{label}]`` and counted per label so the
item carries ``meta.redactions`` and can be flagged in review.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Order matters: specific token formats first, the generic assignment last so
# e.g. `token=ghp_...` redacts as a github token, not a generic credential —
# the credential value pattern therefore refuses to match an existing
# [REDACTED:...] marker (review finding: it used to re-redact and double-count).
# The private-key body is bounded to keep the lazy scan linear-ish on
# pathological BEGIN-without-END input (ReDoS finding).
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,20000}?-----END [A-Z ]*PRIVATE KEY-----"
        ),
    ),
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"ghp_[A-Za-z0-9]{36}")),
    ("api_key", re.compile(r"sk-[A-Za-z0-9\-_]{20,}")),
    # OUR OWN grantee bearer. It used to survive ingest unless it happened to be
    # written as `KB_TOKEN=...` (caught by the generic `credential` rule below) —
    # so a token quoted in prose, or handed to a user in a chat reply, was
    # captured verbatim into the KB by the session hooks. Any credential this
    # system MINTS must be redactable by this system.
    ("kb_token", re.compile(r"kbg_[A-Za-z0-9\-_]{20,}")),
    # ...and the ENROLMENT code, for the same reason. It is printed into a
    # kb_setup reply, and the hooks flush that transcript into the KB and then
    # off-site — so until it is redeemed it is a live credential heading for
    # durable storage. Adding kbg_ without kbe_ left the newer of the two
    # credentials this system mints unredactable.
    ("kb_enrol_code", re.compile(r"kbe_[A-Za-z0-9\-_]{20,}")),
    # ntfy access tokens (`tk_…`) — the credential this system DISTRIBUTES to
    # every machine it enrols, so it travels through exactly the transcripts the
    # session hooks flush. It was unredactable until 2026-08-01: an ntfy admin
    # password leaked into a transcript on 2026-07-31 (a `bash -x` trace) and had
    # to be rotated, and items 1589/2176 still returned plaintext credentials
    # afterwards because nothing here matched the shape.
    ("ntfy_token", re.compile(r"tk_[A-Za-z0-9]{8,}")),
    # Google OAuth client secrets ship with their own prefix; matched by shape so
    # one quoted from ANOTHER project's console is caught too, not just this
    # deployment's (which the config layer below already covers).
    ("google_secret", re.compile(r"GOCSPX-[A-Za-z0-9_\-]{10,}")),
    # The age PRIVATE identity (AGE-SECRET-KEY-1…): the single key that decrypts
    # EVERY offsite backup. offsite_config.generate_age_keypair returns it to be
    # shown ONCE and deliberately persists it NOWHERE — so if it lands in a
    # transcript (a keygen run, a paste) the flush hooks would otherwise carry the
    # whole backup set's decryption key into the KB and off-site. Bounded {50,}.
    ("age_identity", re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]{50,}")),
    # A Google OAuth REFRESH token (1//0…): the crown-jewel backup secret
    # offsite_config.store_drive_token writes write-only into rclone.conf. It is
    # long-lived, so a copy quoted in a transcript is a durable credential heading
    # for durable storage — exactly the shape the session hooks flush.
    ("google_refresh_token", re.compile(r"1//0[A-Za-z0-9_\-]{20,}")),
    # A Google OAuth ACCESS token (ya29.…): what the public MCP's Google sign-in
    # verifies on every cache miss (tokeninfo?access_token=…), and what httpx's
    # INFO request line wrote into journald until 2026-10-28 — kbcore.logsafe now
    # sends every log record through this list too. Short-lived (~1 h), but a
    # working bearer for that hour wherever a copy lands.
    ("google_access_token", re.compile(r"ya29\.[A-Za-z0-9_\-.]{20,}")),
    # …and the third shape the same credential travels in: inline URL userinfo
    # (`postgresql://kb:pw@host`, `https://admin:pw@ntfy…`). The config layer
    # below catches THIS deployment's db password wherever it appears, but a URL
    # pasted from another box or another project is only catchable by shape. The
    # lookbehind/lookahead keep the scheme and host readable — the note stays
    # useful, the credential does not survive.
    (
        "url_credentials",
        re.compile(r"(?<=://)[^\s:/@]{1,64}:(?!\[REDACTED:)[^\s@/]{1,128}(?=@)"),
    ),
    # The same leak's OTHER shape: HTTP basic auth, both as curl types it
    # (`-u user:password`, separated OR glued as `-uuser:password`) and as the
    # header it becomes. Two guards keep it off ordinary text: `(?!//)` stops a
    # URL (`-u https://host`) looking like user:password, and the password must
    # be 6+ chars (same floor as the generic credential rule) so a flag-like
    # `--update:main` is not mistaken for one. It is still deliberately
    # over-inclusive — `docker run -u www-data:www-data` redacts too — because the
    # two errors are not symmetric: a redacted note is a small loss, a leaked
    # password costs a rotation across every enrolled machine.
    (
        "basic_auth",
        re.compile(
            r"(?<![\w-])(?:-{1,2}u(?:ser)?[ =]\s*|-u)"
            r"[^\s:/@]{1,64}:(?!//)(?!\[REDACTED:)\S{6,128}"
        ),
    ),
    (
        "auth_header",
        re.compile(
            r"(?i)authorization\s*:\s*(?:basic|bearer|token)\s+(?!\[REDACTED:)[A-Za-z0-9+/=._\-]{8,}"
        ),
    ),
    # Anchored at the start of a [\w-] run, with the step to the FIRST eyJ in that
    # run atomic. The unanchored form backtracked quadratically on a long run of
    # "eyJ" with no dots — 64 KB cost 1.5 s, and kbcore.logsafe runs this list on
    # request paths anyone can send (2026-10-28 review). Same matches, except a run
    # prefix glued to the token (`%3DeyJ…` → "3D") is redacted along with it.
    ("jwt", re.compile(r"(?<![\w-])(?>[\w-]*?eyJ)[\w-]+\.[\w-]+\.[\w-]+")),
    (
        "credential",
        re.compile(r"(?i)(?:password|passwd|secret|token)\s*[=:]\s*(?!\[REDACTED:)\S{6,}"),
    ),
]


# Every kbcore function that MINTS or returns a PLAINTEXT secret, mapped to the
# SECRET_PATTERNS label that must redact its output by SHAPE (a secret minted for
# ANOTHER machine has no config-value entry on this box, so only its shape can
# save it). CLAUDE.md rule 12: every secret this system issues needs a scrub
# pattern so it cannot survive ingest. The tripwire in
# tests/test_scrub_credentials.py asserts each label here is a real
# SECRET_PATTERNS entry AND each function still exists — so the NEXT minter added
# without a matching pattern fails the build instead of shipping an unredactable
# credential (kbg_, kbe_ and tk_ were each missed in turn before this was a check).
#
# MARKER CONVENTION (review #3 wave D, 2026-08-14) — the OTHER direction, tied
# to source rather than to this dict's author remembering it exists: every
# minter's docstring carries the sentence "returns the plaintext secret exactly
# once", and tests/test_capture_funnel.py greps kbcore for that marker and fails
# if any marked function is absent from this registry. A NEW minter therefore
# writes the marker (the review convention for any function handing back a
# plaintext credential) and the tripwire catches the registry omission
# mechanically. store_drive_token is the one registry entry WITHOUT the marker:
# it RECEIVES the secret as an argument and returns nothing, so the sentence
# would be false in its docstring — it stays hand-registered here.
MINTED_SECRETS: dict[str, str] = {
    "kbcore.principals.create_principal": "kb_token",  # kbg_ grantee bearer
    "kbcore.project_tokens.issue": "kb_token",  # kbg_ per-project token
    "kbcore.enrolment.mint_code": "kb_enrol_code",  # kbe_ enrolment code
    "kbcore.offsite_config.generate_age_keypair": "age_identity",  # AGE-SECRET-KEY-1…
    "kbcore.offsite_config.store_drive_token": "google_refresh_token",  # 1//0…
}


# --- this deployment's OWN secrets, by exact value ----------------------------
#
# The shape patterns above catch a credential wherever it appears, including ones
# this box never held. They cannot catch a secret with no distinctive shape — a
# database password, a Google client secret, a web signing secret — unless it
# happens to be written as `password=…`. Those ARE known to this process: they
# are Settings fields. So the second layer walks the config and redacts the
# literal values, generalizing what used to be a hand-written special case for
# ``kb_token`` alone. Adding a credential to config now makes it redactable
# without touching this file (tests/test_scrub_credentials.py is the tripwire
# that says so out loud).
_CREDENTIAL_FIELD_RE = re.compile(r"(?i)token|password|secret")

# Values that are placeholders, not secrets — redacting them would mangle prose
# and, worse, teach reviewers to ignore [REDACTED] markers.
_PLACEHOLDER_VALUES = frozenset(
    {"change-me", "change-me-long-random", "changeme", "none", "null", "unset"}
)
# Short strings are far more likely to be an English word than a credential.
_MIN_OWN_SECRET_LEN = 12


@lru_cache(maxsize=1)
def credential_setting_fields() -> tuple[str, ...]:
    """Names of every ``Settings`` field that holds a credential (str-typed and
    named ``*token*``/``*password*``/``*secret*``).

    Derived from the model, never hand-listed: a new credential setting is picked
    up automatically. Field NAMES are cached (they are fixed at import); the
    VALUES are read per call, so a test that monkeypatches settings still works.
    """
    from kbcore.config import Settings

    return tuple(
        sorted(
            name
            for name, field in Settings.model_fields.items()
            if _CREDENTIAL_FIELD_RE.search(name) and field.annotation is str
        )
    )


def own_secrets() -> list[tuple[str, str]]:
    """``[(label, value)]`` for the credentials THIS deployment holds, longest
    value first so a secret that contains another is redacted whole."""
    from kbcore.config import settings  # local import — scrub stays dependency-light

    found: list[tuple[str, str]] = []
    for name in credential_setting_fields():
        value = getattr(settings, name, None)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if len(value) < _MIN_OWN_SECRET_LEN or value.lower() in _PLACEHOLDER_VALUES:
            continue
        found.append((name, value))
    found.sort(key=lambda pair: len(pair[1]), reverse=True)
    return found


def merge_redactions(dst: dict[str, int], src: dict[str, int]) -> None:
    """Fold scrub counts from ``src`` into ``dst`` (accumulate per label).

    Lets the ingest chokepoint accumulate redaction counts across every scrubbed
    field (title, body, uri, tags, code_ref, meta) into one ``meta.redactions``.
    """
    for label, count in src.items():
        dst[label] = dst.get(label, 0) + count


def scrub_tree(value: object, redactions: dict[str, int]) -> object:
    """Scrub every STRING leaf of a JSON-ish value (str / list / dict), folding
    redaction counts into ``redactions``; non-string leaves pass through.

    A11-3 finding 24 (the kbcore chokepoint, Invariant 2): the ingest path used to
    scrub only title/body, so a secret pasted into a uri, a tag, or a code_ref/meta
    string stored unredacted and rendered live in the owner's item/review views.
    Recursing here lets ingest_in redact all of them at the single write point.
    Deterministic ⇒ idempotent (a re-scrub of already-redacted text is a no-op).
    """
    if isinstance(value, str):
        cleaned, red = scrub(value)
        merge_redactions(redactions, red)
        return cleaned
    if isinstance(value, dict):
        return {k: scrub_tree(v, redactions) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_tree(v, redactions) for v in value]
    return value


def scrub(text: str) -> tuple[str, dict[str, int]]:
    """Redact secrets from *text*.

    Returns ``(clean_text, redactions)`` where redactions maps label -> count.
    Runs on every ingest path before anything is stored (SEC-2).
    """
    redactions: dict[str, int] = {}
    # Postgres TEXT cannot store NUL (\x00); a transcript message carrying a
    # literal  (json.loads decodes it to a real NUL) would otherwise crash
    # the enqueue INSERT and — on the LOCAL path — wedge behind an advanced
    # checkpoint. Strip NUL before anything else. (Counted so it's visible.)
    if "\x00" in text:
        redactions["nul_stripped"] = text.count("\x00")
        text = text.replace("\x00", "")
    # This deployment's OWN secrets, wherever they appear (bare, in a config
    # snippet, in a flushed transcript of a kb_setup conversation) — the generic
    # token= pattern only catches prefixed forms, and a db password has no shape
    # at all. Exact match, no regex risk. Runs BEFORE the shape patterns so the
    # redaction is labelled with the setting it came from.
    for label, value in own_secrets():
        if value in text:
            n = text.count(value)
            text = text.replace(value, f"[REDACTED:{label}]")
            redactions[label] = redactions.get(label, 0) + n
    for label, pattern in SECRET_PATTERNS:
        text, n = pattern.subn(f"[REDACTED:{label}]", text)
        if n:
            redactions[label] = redactions.get(label, 0) + n
    return text, redactions

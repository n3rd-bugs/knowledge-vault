"""kbcore.logsafe — no secret reaches journald, whichever library logs it.

Until 2026-10-28 kb-mcp-public logged a live Google access token on every
verify-cache miss: httpx writes ``HTTP Request: GET <url> "HTTP/1.1 200 OK"`` at
INFO, and FastMCP's verifier GETs ``tokeninfo?access_token=…``. The fix is a
LogRecord factory that runs every record through kbcore.scrub. No DB, no network.
"""

from __future__ import annotations

import inspect
import logging
import time

import httpx
import pytest
from uvicorn.logging import AccessFormatter

from kbcore import logsafe
from kbcore.config import settings
from kbcore.scrub import scrub

_GOOGLE = "ya29." + "a0AfB_byC" + "x" * 120  # Google access-token SHAPE, not a live value
_TOKENINFO = f"https://oauth2.googleapis.com/tokeninfo?access_token={_GOOGLE}"


@pytest.fixture()
def installed(monkeypatch):
    """Install the factory for one test and put the process back exactly as it was."""
    original = logging.getLogRecordFactory()
    monkeypatch.setattr(logsafe, "_installed", False)
    logsafe.install()
    yield
    logging.setLogRecordFactory(original)


def _messages(caplog, name: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == name]


def test_httpx_request_line_loses_the_google_access_token(installed, caplog):
    """The exact call httpx makes (httpx/_client.py), with an httpx.URL argument."""
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info(
            'HTTP Request: %s %s "%s %d %s"', "GET", httpx.URL(_TOKENINFO), "HTTP/1.1", 200, "OK"
        )
    [line] = _messages(caplog, "httpx")
    assert _GOOGLE not in line and "ya29." not in line
    assert "[REDACTED:google_access_token]" in line
    assert line.startswith("HTTP Request: GET https://oauth2.googleapis.com/tokeninfo?")
    assert line.endswith('"HTTP/1.1 200 OK"'), "the rest of the line stays readable"


def test_uvicorn_access_record_keeps_the_tuple_its_formatter_unpacks(installed):
    """uvicorn's AccessFormatter takes record.args apart positionally, so collapsing
    the record into one string (the generic path) would make the access line crash."""
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("203.0.113.9:4444", "GET", f"/callback?access_token={_GOOGLE}&x=1", "1.1", 401),
        None,
    )
    assert isinstance(record.args, tuple) and len(record.args) == 5
    assert record.args[4] == 401, "non-string args pass through untouched"
    fmt = AccessFormatter(fmt="%(client_addr)s %(request_line)s %(status_code)s", use_colors=False)
    line = fmt.format(record)
    assert _GOOGLE not in line and "[REDACTED:google_access_token]" in line


def test_this_deployments_own_secrets_are_redacted_by_value(installed, caplog, monkeypatch):
    master = "kbtok-" + "7" * 40
    monkeypatch.setattr(settings, "kb_token", master)
    with caplog.at_level(logging.INFO, logger="kbmcp"):
        logging.getLogger("kbmcp").info("config dump: token %s in use", master)
    [line] = _messages(caplog, "kbmcp")
    assert master not in line and "[REDACTED:kb_token]" in line


def test_the_ntfy_topic_in_a_push_url_is_redacted(installed, caplog, monkeypatch):
    topic = "kb-alerts-" + "q" * 12
    monkeypatch.setattr(settings, "kb_notify_topic", topic)
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info(
            "HTTP Request: %s %s", "POST", httpx.URL(f"https://ntfy.example.net/{topic}")
        )
    [line] = _messages(caplog, "httpx")
    assert topic not in line
    assert line == "HTTP Request: POST https://ntfy.example.net/[REDACTED:kb_notify_topic]"


def test_a_short_topic_is_left_alone(installed, caplog, monkeypatch):
    """Below the own-secret floor a topic is more likely an ordinary word."""
    monkeypatch.setattr(settings, "kb_notify_topic", "kb")
    with caplog.at_level(logging.INFO, logger="kbmcp"):
        logging.getLogger("kbmcp").info("kb started")
    assert _messages(caplog, "kbmcp") == ["kb started"]


def test_a_secret_split_across_format_and_args_is_still_caught(installed, caplog):
    """'token=%s' % (value,): neither piece looks like a credential on its own."""
    with caplog.at_level(logging.INFO, logger="kbmcp"):
        logging.getLogger("kbmcp").info("retrying with token=%s", "s3cr3t-" + "v" * 12)
    [line] = _messages(caplog, "kbmcp")
    assert "s3cr3t-" not in line


def test_clean_records_are_left_exactly_as_logged(installed):
    args = ("POST", "http://127.0.0.1:11434/api/embed")
    record = logging.getLogger("httpx").makeRecord(
        "httpx", logging.INFO, __file__, 1, "HTTP Request: %s %s", args, None
    )
    assert record.msg == "HTTP Request: %s %s" and record.args is args


def test_a_failing_redaction_withholds_the_record_instead_of_leaking_it(installed, monkeypatch):
    def boom(_text):
        raise RuntimeError("scrub broke")

    monkeypatch.setattr(logsafe, "redact", boom)
    record = logging.getLogger("kbmcp").makeRecord(
        "kbmcp", logging.INFO, __file__, 1, "token=%s", (_GOOGLE,), None
    )
    assert record.getMessage() == "[log record withheld: redaction failed (RuntimeError)]"


def test_install_is_idempotent(monkeypatch):
    original = logging.getLogRecordFactory()
    monkeypatch.setattr(logsafe, "_installed", False)
    try:
        logsafe.install()
        first = logging.getLogRecordFactory()
        logsafe.install()
        assert logging.getLogRecordFactory() is first, "a second install must not re-wrap"
    finally:
        logging.setLogRecordFactory(original)


def test_scrub_knows_the_google_access_token_shape():
    clean, red = scrub(f"curl '{_TOKENINFO}'")
    assert _GOOGLE not in clean
    assert red == {"google_access_token": 1}


# --- bounded work: these run on the event loop, on text anyone can send -------

_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlF"


def test_the_jwt_pattern_is_linear_on_input_built_to_make_it_backtrack():
    """The old unanchored pattern took ~1.5 s on 64 KB of "eyJ" with no dots."""
    started = time.perf_counter()
    scrub("eyJ" * 21_000)
    assert time.perf_counter() - started < 0.25


def test_the_jwt_rewrite_is_byte_identical_to_the_old_pattern():
    """Ingest dedupes on the SCRUBBED text: a re-sent transcript must scrub exactly
    as it did before the rewrite, glued run prefixes (`%3DeyJ…`) included."""
    import random
    import re

    from kbcore import scrub as scrub_mod

    old = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")
    new = dict(scrub_mod.SECRET_PATTERNS)["jwt"]
    repl = scrub_mod._REPLACEMENTS["jwt"]
    rng = random.Random(7)
    alphabet = 'eyJ.-_a%3D "é'
    samples = [f"id_token%3D{_JWT}&x", f"%22{_JWT}", f"x.{_JWT}.y", f"abc{_JWT} {_JWT}"]
    samples += [
        "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 40))) for _ in range(20000)
    ]
    for s in samples:
        assert new.sub(repl, s) == old.sub("[REDACTED:jwt]", s), s


@pytest.mark.parametrize(
    "text",
    [f"Bearer {_JWT}", f"id_token={_JWT}&x=1", f'"{_JWT}"', f"%22{_JWT}%22", f"glued{_JWT}"],
)
def test_the_jwt_pattern_still_finds_tokens_wherever_they_sit(text):
    clean, red = scrub(text)
    assert "dozjgNryP4J3" not in clean and red.get("jwt") == 1


def test_an_adversarial_access_line_is_cut_before_it_is_scanned(installed):
    path = "/api/search?q=" + "eyJ" * 5000 + "&token=abcdefgh"
    started = time.perf_counter()
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("203.0.113.9:4444", "GET", path, "1.1", 401),
        None,
    )
    assert time.perf_counter() - started < 0.05
    assert len(record.args[2]) < 2200 and "chars dropped by kbcore.logsafe" in record.args[2]
    assert "abcdefgh" not in record.args[2], "past the cap means not emitted at all"
    assert record.args[4] == 401


def test_a_long_message_is_cut_and_nothing_past_the_cap_is_emitted(installed, caplog):
    with caplog.at_level(logging.INFO, logger="kbmcp"):
        logging.getLogger("kbmcp").info("payload %s then %s", "x" * 9000, _GOOGLE)
    [line] = _messages(caplog, "kbmcp")
    assert "ya29." not in line and "chars dropped by kbcore.logsafe" in line


def test_an_oauth_code_in_a_callback_url_is_redacted(installed):
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "203.0.113.9:4444",
            "GET",
            "/auth/callback?code=4/0AX4XfWh-secretcode&state=s1",
            "1.1",
            302,
        ),
        None,
    )
    assert "4/0AX4XfWh" not in record.args[2]
    assert record.args[2] == "/auth/callback?code=[REDACTED:query]&state=s1", "state stays readable"


def test_a_traceback_carrying_a_secret_is_redacted(installed, caplog):
    try:
        raise RuntimeError(f"GET {_TOKENINFO} failed")
    except RuntimeError:
        with caplog.at_level(logging.ERROR, logger="kbmcp"):
            logging.getLogger("kbmcp").exception("verify failed")
    [record] = [r for r in caplog.records if r.name == "kbmcp"]
    text = logging.Formatter().format(record)
    assert "ya29." not in text and "RuntimeError" in text and "Traceback" in text
    assert record.exc_info is None, "a formatter must not be able to re-render the raw one"


def test_a_malformed_record_cannot_leak_through_handle_error(installed, capsys):
    """logging prints a bad call's msg AND args to stderr (Handler.handleError)."""
    logger = logging.getLogger("kbtest.malformed")
    logger.propagate = False
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    try:
        logger.error("token=%s and %s", _GOOGLE)  # one argument short
    finally:
        logger.removeHandler(handler)
        logger.propagate = True
    assert "ya29." not in capsys.readouterr().err


# --- secrets that cross a cut (the redacted text is cut, not the raw text) -----


@pytest.mark.parametrize("before_cut", [1, 5, 19, 40])
def test_a_secret_crossing_the_message_cut_is_not_emitted(installed, monkeypatch, before_cut):
    own = "kbtok-" + "8" * 40  # shapeless: matched only by its exact value
    monkeypatch.setattr(settings, "kb_token", own)
    for secret in (own, "kbg_" + "Q" * 43, _GOOGLE, _JWT):
        filler = "~" * (logsafe._MAX_MESSAGE - before_cut)  # a char no secret contains
        record = logging.getLogger("kbmcp").makeRecord(
            "kbmcp", logging.INFO, __file__, 1, "%s%s tail", (filler, secret), None
        )
        emitted = record.getMessage()
        # Cutting the RAW text kept the secret's first `before_cut` chars, too few for
        # any pattern to match, and emitted them; the redacted text keeps a marker.
        kept = emitted[len(filler) : len(filler) + before_cut]
        assert kept != secret[:before_cut], f"{secret[:6]}… leaked across the cut"
        assert kept == "[REDACTED:"[:before_cut] or kept.startswith("[REDACTED:")
        assert len(emitted) < logsafe._MAX_MESSAGE + 80


def test_a_secret_crossing_the_traceback_tail_cut_is_not_emitted(installed, caplog):
    half = logsafe._MAX_TRACEBACK // 2
    try:  # ya29 straddles the start of the kept tail window
        raise RuntimeError("y" * 40_000 + _GOOGLE + "z" * (half - 20))
    except RuntimeError:
        with caplog.at_level(logging.ERROR, logger="kbmcp"):
            logging.getLogger("kbmcp").exception("boom")
    [record] = [r for r in caplog.records if r.name == "kbmcp"]
    text = logging.Formatter().format(record)
    assert "a0AfB_byC" not in text and "xxxxxxxxxxxxxxxxxxxx" not in text[-half:]


def test_a_private_key_crossing_the_cut_or_unterminated_is_redacted(installed):
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" * 80
    pem = f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----"
    for text in (
        "x" * (logsafe._MAX_MESSAGE - 500) + pem,  # its END lies past the cut
        f"cfg dump: -----BEGIN RSA PRIVATE KEY-----\n{body[:900]}",  # no END at all
    ):
        record = logging.getLogger("kbmcp").makeRecord(
            "kbmcp", logging.INFO, __file__, 1, "%s", (text,), None
        )
        emitted = record.getMessage()
        assert "MIIEvQIBADAN" not in emitted and "[REDACTED:private_key]" in emitted


def test_a_traceback_full_of_key_headers_is_cheap(installed, caplog):
    """scrub's private_key rule rescans 20000 chars from every BEGIN; any
    authenticated MCP caller can put 16 KiB of them into a logged exception."""
    try:
        raise ValueError("-----BEGIN PRIVATE KEY-----" * 600)
    except ValueError:
        started = time.perf_counter()
        logging.getLogger("kbmcp").exception("tool error")
        assert time.perf_counter() - started < 0.02


def test_no_fragment_of_a_planted_secret_survives_any_cut(monkeypatch):
    """Property test (round-3 review): redaction-heavy text — api_key-shaped runs
    that SHRINK when redacted, pulling a fixed cut forward into the scan margin —
    with secrets planted anywhere, cut by the message and the traceback caps. No
    8-char run of any planted secret may come out of either."""
    import random

    own = "kbtok-" + "9" * 40  # shapeless: matched only by its exact value
    monkeypatch.setattr(settings, "kb_token", own)
    grantee = "kbg_" + "Q" * 43
    shrinker = "sk-" + "W" * 60  # -> [REDACTED:api_key], 45 chars shorter
    grower = "?code=ab "  # -> ?code=[REDACTED:query], 14 chars LONGER
    words = ["alpha ", "beta ", "gamma\n", "delta ", "error: ", "value ", "\x00"]
    rng = random.Random(3)
    for case in range(300):
        size, parts = rng.randint(9_000, 40_000), []
        while sum(map(len, parts)) < size:
            roll = rng.random()
            if roll < 0.20:
                parts.append(shrinker)
            elif roll < 0.35:
                parts.append(grower)
            elif roll < 0.40:
                parts.append(own)
            elif roll < 0.45:
                parts.append(grantee)
            else:
                parts.append(rng.choice(words))
        text = "".join(parts)
        for out in (
            logsafe._head(text, logsafe._MAX_MESSAGE),
            logsafe._ends(text, logsafe._MAX_TRACEBACK),
        ):
            for run in ("99999999", "QQQQQQQQ", "WWWWWWWW"):
                assert run not in out, f"case {case}: {run} leaked"


_BODY = "MIIJQgIBADANBgkqhkiG9w0BAQEFAASCCSwwggkoAgEAAoICAQ" * 180  # ~9 KB, RSA-8192-sized


@pytest.mark.parametrize(
    "key",
    [
        f"-----BEGIN PRIVATE KEY-----\n{_BODY}\n-----END PRIVATE KEY-----",
        f"-----BEGIN PRIVATE KEY-----\n{_BODY}\n-----END PRIVATE\x00 KEY-----",  # NUL in END
        f"-----BEGIN PRIVATE KEY-----\n{_BODY}\nValueError: bad key",  # no END at all
        f"-----BEGIN PRIVATE KEY-----\n{_BODY}\n-----BEGIN CERTIFICATE-----\nMIIC\n"
        "-----END CERTIFICATE-----",  # ended by the next BEGIN
    ],
    ids=["terminated", "nul-in-end", "unterminated", "next-begin"],
)
def test_a_long_key_whose_begin_line_precedes_the_kept_tail_is_redacted(key):
    text = "y" * 30_000 + key + "z" * 6_000
    keep = logsafe._MAX_TRACEBACK // 2
    begin = text.index("-----BEGIN")
    # The layout must stay what it tests: BEGIN outside the kept tail, body inside it.
    assert len(text) - (keep + logsafe._LOOKBACK) < begin < len(text) - keep
    assert text.rindex("MIIJ") > len(text) - keep
    out = logsafe._ends(text, logsafe._MAX_TRACEBACK)
    assert "MIIJQgIBADAN" not in out and "-----END PRIVATE" not in out


def test_a_key_bigger_than_the_look_back_still_loses_its_tail_body():
    """A body + END with its BEGIN beyond even the look-back: the tail-window rule."""
    body = "MIIJQgIBADANBgkqhkiG9w0BAQEFAASCCSwwggkoAgEAAoICAQ" * 500  # ~25 KB
    text = (
        "y" * 30_000
        + f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----"
        + "z" * 2_000
    )
    keep = logsafe._MAX_TRACEBACK // 2
    assert text.index("-----BEGIN") < len(text) - (keep + logsafe._LOOKBACK)
    out = logsafe._ends(text, logsafe._MAX_TRACEBACK)
    assert "MIIJQgIBADAN" not in out[-keep:]


def test_a_short_line_that_grows_when_redacted_is_still_cut():
    out = logsafe._head("?code=x " * 900, logsafe._MAX_MESSAGE)  # 7200 chars -> ~20 KB redacted
    assert len(out) < logsafe._MAX_MESSAGE + 60 and "chars dropped by kbcore.logsafe" in out


def test_url_userinfo_is_masked_short_in_logs():
    assert logsafe.redact("connect postgresql://kb:s3cretpw@db:5432/kb") == (
        "connect postgresql://***@db:5432/kb"
    )


def test_an_own_secret_containing_an_at_sign_survives_the_url_mask_whole(monkeypatch):
    monkeypatch.setattr(settings, "kb_db_password", "Sup3r@Secret-2026x")
    out = logsafe.redact("connect postgresql://kb:Sup3r@Secret-2026x@db:5432/kb")
    assert "Secret-2026x" not in out and "Sup3r" not in out


def test_a_topic_with_a_shape_inside_it_is_still_redacted_whole(monkeypatch):
    topic = "kb-task-alerts-" + "abcdefghij" * 3  # "sk-alerts-..." is an api_key shape
    monkeypatch.setattr(settings, "kb_notify_topic", topic)
    out = logsafe.redact(f"HTTP Request: POST https://ntfy.example.net/{topic}")
    assert out == "HTTP Request: POST https://ntfy.example.net/[REDACTED:kb_notify_topic]"


def test_a_google_token_containing_a_shorter_shape_is_redacted_whole():
    token = "ya29.a0AfB1cdE2tk_Q7wX2mPz9-LkV3" + "m" * 40  # "tk_..." is the ntfy shape
    clean, red = scrub(f"tokeninfo?access_token={token} 200")
    assert "LkV3" not in clean and red == {"google_access_token": 1}


def test_a_json_quoted_credential_is_redacted():
    out = logsafe.redact(
        '{"user": "kb", "password": "hunter2-very-secret", "token":"s3cr3t-12345"}'
    )
    assert "hunter2" not in out and "s3cr3t" not in out and '"user": "kb"' in out


def test_one_unbroken_secret_longer_than_the_margin_does_not_leak_at_a_cut():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJ" + "A" * 4300 + ".c2lnbmF0dXJl"  # claims > margin
    head = logsafe._head("x" * 8000 + " " + jwt + " ok", logsafe._MAX_MESSAGE)
    assert "eyJhbGciOiJIUzI1NiJ9" not in head
    run = "sk-" + "Q" * 16_981  # one run longer than the tail look-back
    text = "y" * 35_323 + " " + run + " "
    text += "z" * (60_000 - len(text))
    assert "QQQQQQQQ" not in logsafe._ends(text, logsafe._MAX_TRACEBACK)


def test_a_malformed_call_with_an_exception_as_msg_leaks_nothing(installed, capsys):
    logger = logging.getLogger("kbtest.malformed2")
    logger.propagate = False
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    try:  # one argument too many for a msg with no % placeholder: handleError prints both
        logger.error(RuntimeError(f"GET {_TOKENINFO} failed"), "retrying")
    finally:
        logger.removeHandler(handler)
        logger.propagate = True
    assert "ya29." not in capsys.readouterr().err


def test_a_nul_inside_a_key_header_does_not_bring_the_slow_rule_back():
    started = time.perf_counter()
    out = logsafe.redact("-----BEGIN PRIVATE\x00 KEY-----" * 450)
    assert time.perf_counter() - started < 0.01
    assert "PRIVATE KEY" not in out


def test_a_failed_redaction_of_an_access_line_still_formats(installed, monkeypatch, capsys):
    def boom(_text):
        raise RuntimeError("scrub broke")

    monkeypatch.setattr(logsafe, "redact", boom)
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("203.0.113.9:4444", "GET", f"/x?access_token={_GOOGLE}", "1.1", 200),
        None,
    )
    fmt = AccessFormatter(fmt="%(client_addr)s %(request_line)s %(status_code)s", use_colors=False)
    line = fmt.format(record)
    assert "ya29." not in line and "[withheld]" in line and "200" in line


def test_a_topic_configured_with_slashes_is_still_redacted(installed, caplog, monkeypatch):
    topic = "kb-alerts-" + "q" * 12
    monkeypatch.setattr(settings, "kb_notify_topic", f"/{topic}/")
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info("HTTP Request: POST https://ntfy.example.net/%s", topic)
    assert topic not in _messages(caplog, "httpx")[0]


@pytest.mark.parametrize(
    "module_name",
    ["kbmcp.server", "kbrest.server", "kbweb.server", "kbworker.watcher", "kbworker.consolidate"],
)
def test_every_long_running_entry_point_installs_the_funnel(module_name):
    """Tripwire: a served process that configures logging without the funnel is
    exactly how the tokeninfo line shipped."""
    import importlib

    source = inspect.getsource(importlib.import_module(module_name).main)
    assert "logsafe.install(" in source, f"{module_name}.main() logs without kbcore.logsafe"
    # Request-serving processes keep the default cap; only the workers raise it.
    worker = module_name.startswith("kbworker.")
    assert ("WORKER_MAX_MESSAGE" in source) == worker, f"{module_name}: wrong record cap"


def test_a_worker_logs_its_own_long_summary_whole(monkeypatch, caplog):
    """The nightly's one-line counts summary (~20 KB) was cut to 8 KiB on its first
    night; a worker installs with WORKER_MAX_MESSAGE, a server keeps the default."""
    original = logging.getLogRecordFactory()
    monkeypatch.setattr(logsafe, "_installed", False)
    summary = {f"step_{i}": {"n": i, "note": "x" * 40} for i in range(300)}  # ~20 KB
    try:
        logsafe.install(logsafe.WORKER_MAX_MESSAGE)
        with caplog.at_level(logging.INFO, logger="kbworker.consolidate"):
            logging.getLogger("kbworker.consolidate").info("consolidation done: %s", summary)
        [line] = [r.getMessage() for r in caplog.records if r.name == "kbworker.consolidate"]
        assert "chars dropped" not in line and line.endswith("}}")
        logsafe.install()  # a server's install: the default 8 KiB cap again
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="kbworker.consolidate"):
            logging.getLogger("kbworker.consolidate").info("consolidation done: %s", summary)
        [line] = [r.getMessage() for r in caplog.records if r.name == "kbworker.consolidate"]
        assert "chars dropped by kbcore.logsafe" in line
    finally:
        logging.setLogRecordFactory(original)

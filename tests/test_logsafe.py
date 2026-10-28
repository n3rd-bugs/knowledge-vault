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
    assert "logsafe.install()" in source, f"{module_name}.main() logs without kbcore.logsafe"

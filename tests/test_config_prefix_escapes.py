"""KB_EMBED_QUERY_PREFIX means the same bytes whoever launches the process (KB 57359).

The deployed .env carries the qwen3-embedding query prefix as ONE double-quoted line with
its newline spelled as a backslash-n escape, and the readers disagree about that escape.
python-dotenv (pydantic-settings' env_file reader) and docker compose's env_file decode it
to a newline: kb-mcp, kb-rest, any shell run that leaves .env to pydantic-settings.
systemd's EnvironmentFile= keeps both characters (kb-worker, the 01:30 nightly), and so does
a bash ``set -a; . .env`` (scripts/*.sh), as double quotes in a real shell do.
pydantic-settings lets the process env beat its own .env read, so under systemd the literal
escape won, and the nightly report's eval_embed_query_prefix_sha read 945a97534ce6 (93 chars)
where production embeds with df4b2898bf22 (92 chars). kbcore.config now decodes the escape
in a field validator, whichever source the value comes from.

Every test builds a FRESH Settings with an explicit ``_env_file`` (a tmp file, or None)
over a monkeypatched process env: never the repo .env, never the module singleton
(conftest pins that one's prefix to ""). Pure config: no DB, no Ollama.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from kbcore.config import Settings

KEY = "KB_EMBED_QUERY_PREFIX"
# The code default IS the deployed prefix; only how .env spells its newline is at issue.
DEFAULT: str = Settings.model_fields["kb_embed_query_prefix"].default
# The deployment writes it as one double-quoted line with the newline spelled \n, and that
# escaped text is exactly what EnvironmentFile= exports.
ESCAPED = DEFAULT.replace("\n", "\\n")
DEPLOYED_LINE = f'{KEY}="{ESCAPED}"'


def _env_file(tmp_path: Path, line: str) -> Path:
    """A throwaway .env holding exactly ``line`` (the repo's own .env is never read)."""
    path = tmp_path / ".env"
    path.write_text(line + "\n", encoding="utf-8")
    return path


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the key in EVERY case: Settings is case-insensitive, so a lowercase
    ``kb_embed_query_prefix`` left in the caller's shell would otherwise leak in."""
    for name in [k for k in os.environ if k.upper() == KEY]:
        monkeypatch.delenv(name)


def _from_env(monkeypatch: pytest.MonkeyPatch, value: str, **init: str) -> str:
    """The prefix a process gets when its environment carries ``value`` and no .env exists."""
    _clear_env(monkeypatch)
    monkeypatch.setenv(KEY, value)
    return Settings(_env_file=None, **init).kb_embed_query_prefix


# ------------------------------------------------------- systemd's reading, modelled

# parse_env_file_internal() in systemd's src/basic/env-file.c (read at tag v259), states
# DOUBLE_QUOTE_VALUE / DOUBLE_QUOTE_VALUE_ESCAPE: after a backslash, a SHELL_NEED_ESCAPE character
# (src/basic/escape.h: " \ ` $) is unescaped, a newline is eaten as a line continuation, and ANY
# other character is kept WITH its backslash, "like the real shell does". So \n stays two
# characters, where python-dotenv decodes it.
_SHELL_NEED_ESCAPE = '"\\`$'


def _systemd_reads(line: str) -> tuple[str, str]:
    """(name, value) that EnvironmentFile= exports for one ``NAME="..."`` line, that form only."""
    name, _, rest = line.partition("=")
    assert rest.startswith('"'), "the model covers a double-quoted value only"
    out: list[str] = []
    escaped = False
    for i, c in enumerate(rest[1:], start=1):
        if escaped:  # DOUBLE_QUOTE_VALUE_ESCAPE
            escaped = False
            if c in _SHELL_NEED_ESCAPE:
                out.append(c)
            elif c != "\n":
                out.append("\\" + c)
        elif c == "\\":
            escaped = True
        elif c == '"':  # the closing quote; PRE_VALUE then APPENDS any non-blank tail
            assert not rest[i + 1 :].strip(), "the model covers nothing after the closing quote"
            break
        else:
            out.append(c)
    return name, "".join(out)


@pytest.mark.parametrize(
    ("quoted", "exported"),
    [
        (r'"a\nb"', r"a\nb"),  # any other character keeps its backslash: the incident
        (r'"a\tb"', r"a\tb"),  # ... every such escape, not only \n
        (r'"\" \\ \` \$"', r'" \ ` $'),  # the SHELL_NEED_ESCAPE characters are unescaped
        ('"a\\\nb"', "ab"),  # backslash + newline is a line continuation: eaten
    ],
    ids=["other-kept", "tab-kept", "need-escape", "continuation"],
)
def test_the_systemd_model_follows_env_file_c(quoted, exported):
    """The agreement test below is only as good as this model: one case per branch."""
    assert _systemd_reads(f"K={quoted}") == ("K", exported)


# ---------------------------------------------------- one deployed line, both readings


def test_the_systemd_reading_becomes_a_real_newline(monkeypatch):
    """A unit's environment holds the escape as two characters; the setting holds the
    newline, and no backslash survives."""
    got = _from_env(monkeypatch, ESCAPED)
    assert got == DEFAULT
    assert "\n" in got and "\\" not in got


def test_the_dotenv_reading_is_already_a_newline_and_left_alone(tmp_path, monkeypatch):
    """python-dotenv decodes the escape itself (as compose's env_file does), so on this
    path (kb-mcp, kb-rest, a shell) the validator has nothing to change."""
    env_file = _env_file(tmp_path, DEPLOYED_LINE)
    _clear_env(monkeypatch)
    assert dotenv_values(env_file)[KEY] == DEFAULT, "python-dotenv decodes \\n in double quotes"
    assert Settings(_env_file=env_file).kb_embed_query_prefix == DEFAULT


def test_both_launchers_read_the_same_line_to_the_same_prefix(tmp_path, monkeypatch):
    """The incident end to end. Under a unit the process env carries systemd's reading of
    the line AND pydantic-settings reads the same file itself; the env wins (premise below).
    Before KB 57359 that was a 93-char prefix with a literal backslash-n; now both launchers
    land on the code default, byte for byte."""
    env_file = _env_file(tmp_path, DEPLOYED_LINE)

    _clear_env(monkeypatch)  # a container or a shell: the .env read alone
    via_dotenv = Settings(_env_file=env_file).kb_embed_query_prefix

    name, exported = _systemd_reads(DEPLOYED_LINE)
    assert (name, exported) == (KEY, ESCAPED), "systemd hands the unit the escape undecoded"
    monkeypatch.setenv(name, exported)  # a unit: EnvironmentFile= exported the line first
    via_systemd = Settings(_env_file=env_file).kb_embed_query_prefix

    assert via_systemd == via_dotenv == DEFAULT


# -------------------------------------------------------- every escape, every source


@pytest.mark.parametrize(
    ("raw", "decoded"),
    [
        ("a\\nb\\nc", "a\nb\nc"),  # EVERY escape, not only the first
        ("x\ny\\nz", "x\ny\nz"),  # a real newline elsewhere does not stop the decoding
        ("\\n", "\n"),  # the escape alone
    ],
    ids=["every", "mixed", "bare"],
)
def test_every_backslash_n_becomes_a_newline(monkeypatch, raw, decoded):
    """EVERY two-character escape, wherever it sits. Inputs derived from the code default (one
    escape, always before "Query: ") cannot tell that rule from a narrower decoder."""
    assert _from_env(monkeypatch, raw) == decoded


def test_a_single_quoted_env_line_is_decoded_too(tmp_path, monkeypatch):
    """python-dotenv keeps \\n literal inside single quotes, as systemd does, so here the
    escape reaches the validator through the .env source itself, not the process env."""
    env_file = _env_file(tmp_path, f"{KEY}='{ESCAPED}'")
    _clear_env(monkeypatch)
    assert dotenv_values(env_file)[KEY] == ESCAPED, "python-dotenv keeps \\n in single quotes"
    assert Settings(_env_file=env_file).kb_embed_query_prefix == DEFAULT


def test_an_init_argument_is_decoded_too():
    """The remaining source: a value handed to Settings() directly (it outranks the env)."""
    assert Settings(_env_file=None, kb_embed_query_prefix=ESCAPED).kb_embed_query_prefix == DEFAULT


def test_the_pairing_check_sees_the_decoded_prefix(monkeypatch, caplog):
    """The field validator runs before _check_embed_pairing, so an escape-only prefix (empty
    once decoded) warns under a unit too; read raw, its two characters passed for a prefix."""
    with caplog.at_level(logging.WARNING, logger="kbcore.config"):
        got = _from_env(monkeypatch, "\\n", kb_embed_model="qwen3-embedding:0.6b")
    assert got == "\n"
    assert "instruction-aware" in caplog.text


# ---------------------------------------------------------- nothing else is touched


@pytest.mark.parametrize(
    ("model", "prefix"),
    [
        ("qwen3-embedding:0.6b", DEFAULT),  # the newline is already real
        ("qwen3-embedding:0.6b", "Query: "),  # plain text, no newline at all
        ("bge-m3", ""),  # bge-m3 takes no prefix
        ("qwen3-embedding:0.6b", "a\\tb"),  # another escape is NOT decoded
        ("qwen3-embedding:0.6b", "cost \\$5"),  # nor one systemd would have unescaped
        ("qwen3-embedding:0.6b", "Requête: "),  # non-ASCII survives (unicode_escape would not)
        ("qwen3-embedding:0.6b", "ends \\"),  # a trailing lone backslash
    ],
    ids=["real-newline", "plain", "empty", "tab-escape", "dollar-escape", "non-ascii", "lone-bs"],
)
def test_a_prefix_without_the_escape_passes_through(monkeypatch, model, prefix):
    """Only the two-character escape is rewritten; anything else arrives byte for byte."""
    assert _from_env(monkeypatch, prefix, kb_embed_model=model) == prefix


@pytest.mark.parametrize("raw", [ESCAPED, "a\\\\nb"], ids=["deployed", "escaped-backslash"])
def test_decoding_is_idempotent(monkeypatch, raw):
    """Decoding leaves no escape behind, so a second pass changes nothing: a value python-dotenv
    (or an earlier pass) already decoded can never drift. A backslash before the escape is where
    a broader decoder (unicode_escape) drifts on the second pass."""
    once = _from_env(monkeypatch, raw)
    assert _from_env(monkeypatch, once) == once


# ------------------------------------------------------ the premise: why a validator


def test_the_process_environment_beats_the_env_file(tmp_path, monkeypatch):
    """pydantic-settings ranks the process env above its own .env read and takes the env
    value verbatim. Under a unit both carry the prefix and systemd's export wins, so the
    decoding has to sit where every source's value passes (a field validator), not in any
    one reader."""
    env_file = _env_file(tmp_path, f'{KEY}="from the .env"')
    _clear_env(monkeypatch)
    monkeypatch.setenv(KEY, "from the environment")
    assert Settings(_env_file=env_file).kb_embed_query_prefix == "from the environment"

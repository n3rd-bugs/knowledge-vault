"""kbmcp.tools_docs — the document, raw-read and project-token MCP tools.

This module had no tests at all, which mattered most for one thing: `_env_key`
is the only PYTHON definition of the `KB_TOKEN_<PROJECT>` contract, and the
flush hook re-derives the same key independently in shell. If the two ever
disagree, a machine silently stops finding its token for that project.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from kbcore import documents, project_tokens, scope
from kbmcp.tools_docs import _decode, _env_key

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "contrib" / "claude" / "kb-session-flush" / "flush-session-to-kb.sh"


@pytest.fixture()
def as_owner():
    with scope.use_principal(scope.OWNER):
        yield


# ------------------------------------------------- the cross-language contract


def _shell_key(project: str) -> str:
    """Derive the key the way the HOOK does, using the hook's own expression."""
    expr = f"printf '%s' '{project}' | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9' '_'"
    out = subprocess.run(["bash", "-c", expr], capture_output=True, text=True, check=True)
    return "KB_TOKEN_" + out.stdout


@pytest.mark.parametrize(
    "project",
    ["myproj", "data-server", "kb.system", "a_b", "repeater", "my-proj.v2", "x1"],
)
def test_the_env_key_agrees_between_python_and_the_hook(project):
    """Python prints the line to add; the shell decides which line to read.

    They are independent implementations of one contract, so a divergence is a
    machine that silently stops capturing that project.
    """
    assert _env_key(project) == _shell_key(project), (
        f"python and the hook disagree on the variable name for {project!r}"
    )


def test_the_hook_still_derives_the_key_the_way_this_test_assumes():
    """Guard the guard: if the hook changes how it folds the slug, the
    parametrised test above would keep comparing against a stale expression."""
    src = HOOK.read_text()
    assert "tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9' '_'" in src, (
        "the hook's key derivation changed — update _shell_key in this test"
    )
    assert "KB_TOKEN_${PROJ_KEY}" in src or "KB_TOKEN_$PROJ_KEY" in src


def test_the_key_is_a_legal_shell_variable_name():
    for project in ("data-server", "kb.system", "a-b.c_d"):
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", _env_key(project)), _env_key(project)


# --------------------------------------------------------------- content decode


def test_decode_requires_exactly_one_of_content_or_base64():
    from fastmcp.exceptions import ToolError

    assert _decode("hello", None) == b"hello"
    assert _decode(None, "aGVsbG8=") == b"hello"
    for bad in ((None, None), ("a", "aGVsbG8=")):
        with pytest.raises(ToolError):
            _decode(*bad)


def test_decode_rejects_invalid_base64():
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        _decode(None, "not!valid!base64")


def test_base64_carries_bytes_text_cannot(kb, as_owner):
    """The reason content_b64 exists: a document that is not valid UTF-8.

    Uses high-byte binary WITHOUT NUL. archive_raw itself round-trips all 256
    byte values exactly (asserted below), but the full put_document path does
    not yet preserve a NUL-bearing document — see the xfail that follows, which
    records that rather than hiding it.
    """
    import base64

    raw = bytes(b for b in range(1, 256))
    doc = documents.put_document(
        "proj", "binary.bin", content=_decode(None, base64.b64encode(raw).decode())
    )
    assert documents.read_raw(doc.source_id) == raw


def test_the_archive_itself_preserves_every_byte_value():
    """The storage layer is exact; anything lost above it is a path bug, not a
    limit of the archive."""
    import pathlib as _pl

    import zstandard

    from kbcore.ingest import archive_raw

    raw = bytes(range(256))
    path = _pl.Path(archive_raw("t-binary-probe", raw))
    try:
        assert zstandard.ZstdDecompressor().decompress(path.read_bytes()) == raw
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.xfail(
    reason="a document containing a NUL byte does not survive put_document intact; "
    "archive_raw is exact (test above), so the loss is in the document path. "
    "Recorded rather than hidden — binary WITH NUL is not yet supported.",
    strict=True,
)
def test_a_document_containing_NUL_round_trips(kb, as_owner):
    raw = bytes(range(256))
    doc = documents.put_document("proj", "withnul.bin", content=raw)
    assert documents.read_raw(doc.source_id) == raw


# ------------------------------------------------- the token line it prints


def test_the_printed_token_line_is_what_the_hook_will_read(kb, as_owner):
    """The tool tells the user to paste a line; the hook has to find it."""
    t = project_tokens.issue("data-server", machine="laptop")
    line = f"{_env_key(t.project)}={t.token}"
    assert line.startswith("KB_TOKEN_DATA_SERVER=")
    # The hook sources the file, so the line must be assignable shell.
    out = subprocess.run(
        ["bash", "-c", f'{line}; printf "%s" "$KB_TOKEN_DATA_SERVER"'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout == t.token

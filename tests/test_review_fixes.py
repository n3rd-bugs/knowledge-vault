"""Regression tests for the Phase 1 adversarial-review findings."""

from __future__ import annotations

import time

import pytest

from kbcore.chunk import MAX_TOKENS, chunk_markdown, count_tokens
from kbcore.errors import InvalidOperation
from kbcore.ingest import ingest, normalize_title
from kbcore.items import get_item
from kbcore.scrub import scrub
from kbcore.search import search
from kbcore.supersede import supersede

# --- chunker -----------------------------------------------------------------


def test_inner_fence_does_not_close_outer_fence():
    # A ```python open inside a ```` outer fence must not terminate it.
    outer = "````markdown\nExample doc:\n```python\nprint('hi')\n```\nmore text\n````"
    chunks = chunk_markdown("Doc", f"intro\n\n{outer}\n\nafter")
    holding = [c for c in chunks if "````markdown" in c]
    assert len(holding) == 1
    assert "more text\n````" in holding[0], "outer fence stays intact to its real close"


def test_fence_close_requires_bare_marker():
    # "``` trailing words" is not a valid close; the next bare ``` is.
    body = "```\ncode line\n``` not a close\nstill code\n```\n\nprose after"
    chunks = chunk_markdown("Doc", body)
    fenced = [c for c in chunks if "code line" in c]
    assert len(fenced) == 1
    assert "still code" in fenced[0]


def test_oversize_paragraph_is_split_not_unbounded():
    wall = " ".join(f"word{i}" for i in range(4000))  # one giant paragraph, no blanks
    chunks = chunk_markdown("Doc", wall)
    assert len(chunks) > 1, "wall of text must be split"
    assert all(count_tokens(c) <= MAX_TOKENS + 50 for c in chunks)  # +breadcrumb slack


def test_no_pure_overlap_trailing_chunk():
    paras = "\n\n".join(f"Sentence {i}. " * 30 for i in range(12))
    chunks = chunk_markdown("Doc", paras)
    for a, b in zip(chunks, chunks[1:], strict=False):
        stripped = b.split("\n\n", 1)[1]  # drop breadcrumb
        assert stripped not in a, "chunk must not be a strict subset of its predecessor"


# --- scrub -------------------------------------------------------------------


def test_no_double_redaction_of_specific_tokens():
    fake = "ghp_" + "0" * 36
    clean, redactions = scrub(f"token={fake}")
    assert clean == "token=[REDACTED:github_token]"
    assert redactions == {"github_token": 1}, "credential pattern must not re-redact"


def test_private_key_scan_is_bounded():
    pathological = ("-----BEGIN RSA PRIVATE KEY-----\n" + "A" * 100) * 300  # no END anywhere
    start = time.monotonic()
    scrub(pathological)
    assert time.monotonic() - start < 2.0, "pathological BEGIN spam must not hang ingest"


# --- ingest ------------------------------------------------------------------


def test_title_is_canonicalized_single_line():
    assert normalize_title("A\n\nB") == "A B"
    assert normalize_title("  spaced   out\ttitle ") == "spaced out title"


@pytest.mark.usefixtures("kb")
def test_newline_title_cannot_alias_another_item():
    a = ingest(project="p", type="note", title="A\n\nB", body="C")
    b = ingest(project="p", type="note", title="A", body="B\n\nC")
    assert a is not None and b is not None, "distinct items must both ingest"


@pytest.mark.usefixtures("kb")
def test_markdown_snippet_keeps_fence_atomic():
    fence = "```js\nconst x = require('docx');\nbuild();\n```"
    filler = ("Long explanation sentence about the snippet. " * 60).strip()
    item_id = ingest(
        project="p",
        type="snippet",
        title="snippet with fence",
        body=f"{filler}\n\n{fence}\n\n{filler}",
    )
    from kbcore import db

    with db.connect() as conn:
        chunk_texts = [
            r[0]
            for r in conn.execute(
                "SELECT text FROM chunks WHERE item_id = %s ORDER BY ord", (item_id,)
            ).fetchall()
        ]
    assert any(fence in t for t in chunk_texts), "fence must survive intact in one chunk"


# --- supersede ---------------------------------------------------------------


@pytest.mark.usefixtures("kb")
def test_supersede_carries_code_ref_to_replacement():
    old = ingest(
        project="p",
        type="gotcha",
        title="anchored gotcha",
        body="original wisdom",
        code_ref={"repo": "r", "path": "src/x.c", "lines": "1-5"},
    )
    new = supersede(old, new_body="corrected wisdom", reason="was wrong")
    replacement = get_item(new)
    assert replacement.code_ref is not None
    assert replacement.code_ref.path == "src/x.c"
    assert "redactions" not in replacement.meta


@pytest.mark.usefixtures("kb")
def test_supersede_rejects_non_active_replacement():
    a = ingest(project="p", type="note", title="a", body="version a")
    b = ingest(project="p", type="note", title="b", body="version b")
    supersede(a, new_item_id=b, reason="b wins")
    c = ingest(project="p", type="note", title="c", body="version c")
    with pytest.raises(InvalidOperation):
        supersede(c, new_item_id=a, reason="a is already superseded — no cycles")


# --- search ------------------------------------------------------------------


@pytest.mark.usefixtures("kb")
def test_search_returns_at_most_k_after_global_trust_sort():
    for i in range(6):
        ingest(
            project="p",
            type="note",
            title=f"widget note {i}",
            body=f"All about the widget frobnicator, variant {i}.",
        )
    hits = search("widget frobnicator", k=3)
    assert len(hits) == 3
    assert hits[0].score >= hits[-1].score

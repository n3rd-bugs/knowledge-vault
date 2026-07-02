"""Real-DB tests: idempotent ingest, hybrid search filters, supersede visibility.

Run against the compose Postgres in the isolated kb_test schema; Ollama is
mocked (deterministic embeddings) per CLAUDE.md.
"""

from __future__ import annotations

import pytest

from kbcore import db, graph
from kbcore.errors import InvalidOperation, NotFound
from kbcore.ingest import ingest
from kbcore.items import get_item
from kbcore.search import search
from kbcore.supersede import supersede

pytestmark = pytest.mark.usefixtures("kb")

GOTCHA = dict(
    project="motor-ctl",
    type="gotcha",
    title="DRV8706 SPI polarity gotcha",
    body="The DRV8706 needs SPI mode 1; mode 0 reads all zeros.",
    tags=["protocol:spi", "hw:drv8706"],
)


def _counts() -> tuple[int, int, int]:
    with db.connect() as conn:
        items = conn.execute("SELECT count(*) FROM items").fetchone()[0]
        sources = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
        chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    return items, sources, chunks


def test_ingest_is_idempotent():
    first = ingest(**GOTCHA)
    assert first is not None
    after_first = _counts()
    second = ingest(**GOTCHA)
    assert second is None
    assert _counts() == after_first, "re-ingest must add zero rows"


def test_ingest_scrubs_before_storing():
    fake = "ghp_" + "0" * 36
    item_id = ingest(project="p", type="note", title="leaky note", body=f"token {fake} leaked")
    item = get_item(item_id)
    assert fake not in item.body
    assert item.meta["redactions"] == {"github_token": 1}


def test_search_finds_by_keyword_and_filters_by_tags():
    ingest(**GOTCHA)
    ingest(
        project="docx-tools",
        type="snippet",
        title="docx footer snippet",
        body="Add a Footer with PageNumber.CURRENT in the docx npm package.",
        tags=["lang:js"],
    )
    hits = search("SPI polarity", tags=["protocol:spi"])
    assert hits and hits[0].title == GOTCHA["title"]
    assert all("protocol:spi" in h.tags for h in hits)
    assert search("SPI polarity", tags=["protocol:i2c"]) == []


def test_search_project_filter_and_boost():
    ingest(**GOTCHA)
    other = dict(
        GOTCHA,
        project="other-proj",
        title="DRV8706 SPI polarity gotcha (copy)",
        body=GOTCHA["body"] + " Seen again on the second board.",
    )
    ingest(**other)
    scoped = search("SPI polarity", project="other-proj")
    assert {h.project for h in scoped} == {"other-proj"}
    boosted = search("SPI polarity", boost_project="other-proj")
    assert boosted[0].project == "other-proj", "+0.05 boost must float the project"


def test_supersede_hides_old_from_default_search_and_flag_recovers_it():
    old_id = ingest(**GOTCHA)
    new_id = supersede(
        old_id,
        new_body="Correction: DRV8706 wants SPI mode 1 (CPOL=0, CPHA=1); verified on scope.",
        reason="scoped the bus, original note wording was ambiguous",
    )
    default_ids = {h.item_id for h in search("DRV8706 SPI")}
    assert old_id not in default_ids
    assert new_id in default_ids
    with_old = {h.item_id for h in search("DRV8706 SPI", include_superseded=True)}
    assert old_id in with_old
    old = get_item(old_id)
    assert old.status == "superseded" and old.superseded_by == new_id
    assert any(r.rel == "supersedes" for r in old.relations)
    # raw body untouched (Invariant 1)
    assert old.body == "The DRV8706 needs SPI mode 1; mode 0 reads all zeros."


def test_supersede_guards():
    item_id = ingest(**GOTCHA)
    with pytest.raises(InvalidOperation):
        supersede(item_id, reason="no replacement given")
    with pytest.raises(NotFound):
        supersede(999999, new_body="x", reason="missing old item")
    supersede(item_id, new_body="replacement text for the gotcha", reason="r")
    with pytest.raises(InvalidOperation):
        supersede(item_id, new_body="again", reason="already superseded")


def test_link_fixes_relation():
    bug = ingest(project="p", type="bug", title="UART drops bytes", body="RX overrun at 115200.")
    fix = ingest(project="p", type="note", title="UART fix", body="Ring buffer in the RX ISR.")
    graph.link(fix, "fixes", bug)
    assert any(r.rel == "fixes" and r.dst_id == bug for r in get_item(fix).relations)
    with pytest.raises(InvalidOperation):
        graph.link(fix, "not-a-rel", bug)


def test_code_ref_roundtrip():
    item_id = ingest(
        project="motor-ctl",
        type="gotcha",
        title="anchored",
        body="Some knowledge about code.",
        code_ref={"repo": "motor-ctl", "path": "src/drv8706.c", "lines": "88-131"},
    )
    item = get_item(item_id)
    assert item.code_ref is not None
    assert item.code_ref.render() == "↳ motor-ctl:src/drv8706.c:88-131"
    hits = search("knowledge about code")
    assert hits and hits[0].code_ref and hits[0].code_ref.path == "src/drv8706.c"

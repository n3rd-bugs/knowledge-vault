"""kb reprocess — replay retained session raws through the CURRENT pipeline.

Extractor + embeddings are mocked (CLAUDE.md: mock only Ollama); the DB is the
real compose Postgres in kb_test. Guarantees under test: content-hash
idempotency (a v1-identical re-run is a no-op — Invariant 3), new-not-supersede
(Invariant 5), shadow writes NOTHING, session-only selection, filter behavior,
and per-source error isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kbcore import db, llm, reprocess
from kbcore import ingest as ingest_mod
from kbcore.config import settings
from kbcore.errors import InvalidOperation
from kbworker.sessions import extract_and_ingest

pytestmark = pytest.mark.usefixtures("kb")


@pytest.fixture(autouse=True)
def _tmp_raw(tmp_path, monkeypatch):
    """Keep raw archives off the real ~/kb during tests."""
    monkeypatch.setattr(settings, "kb_raw", tmp_path / "kbraw")


TEXT = (
    "We chose SPI mode 1 because mode 0 reads all zeros on the DRV8706.\n\n"
    "Also num_ctx must be 16384 or Ollama silently truncates the transcript."
)
URI = "/tmp/fake/session.jsonl:0-500"

V1_ITEMS = [
    {
        "type": "decision",
        "title": "SPI mode 1",
        "body": "Use SPI mode 1; mode 0 reads zeros.",
        "quote": "mode 0 reads all zeros on the DRV8706",
        "tags": ["hw:drv8706"],
    },
    {
        "type": "gotcha",
        "title": "num_ctx trap",
        "body": "Set num_ctx=16384 explicitly.",
        "quote": "num_ctx must be 16384",
        "tags": ["tool:ollama"],
    },
]
NEW_ITEM = {
    "type": "gotcha",
    "title": "silent truncation",
    "body": "Ollama truncates input silently at the runtime default context.",
    "quote": "Ollama silently truncates the transcript",
    "tags": ["tool:ollama"],
}


def _mock_extractor(monkeypatch, items):
    monkeypatch.setattr(llm, "extract_items", lambda t, **kw: [dict(i) for i in items])


def _seed_v1(monkeypatch, project="proj", uri=URI, text=TEXT):
    """Simulate the ORIGINAL v1 ingestion: archives raw, stamps meta.extractor."""
    _mock_extractor(monkeypatch, V1_ITEMS)
    saved, rejected = extract_and_ingest(text, project, uri)
    assert saved == len(V1_ITEMS) and rejected == 0


def _counts() -> tuple[int, int, int]:
    with db.connect() as conn:
        items = conn.execute("SELECT count(*) FROM items").fetchone()[0]
        sources = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
        review = conn.execute("SELECT count(*) FROM review_queue").fetchone()[0]
    return items, sources, review


def test_v1_identical_rerun_is_noop(monkeypatch):
    """THE idempotency guarantee: same extractor output → content-hash dedupe → no-op."""
    _seed_v1(monkeypatch)
    before = _counts()

    _mock_extractor(monkeypatch, V1_ITEMS)  # v1-identical re-run
    result = reprocess.reprocess(shadow=False)

    assert result["sources_seen"] == 1, "one raw window = ONE group (not one per old item)"
    assert result["saved"] == 0
    assert result["errors"] == 0
    assert _counts() == before, "identical re-run writes zero items/sources/review rows"


def test_richer_rerun_lands_one_new_unverified_item(monkeypatch):
    _seed_v1(monkeypatch)
    with db.connect() as conn:
        orig = conn.execute("SELECT id FROM items ORDER BY id").fetchall()
    orig_ids = [r[0] for r in orig]
    rows = reprocess.reprocessable_sources()
    src_id = rows[0]["source_id"]

    _mock_extractor(monkeypatch, [*V1_ITEMS, NEW_ITEM])  # pipeline v2 finds one extra
    result = reprocess.reprocess(shadow=False)
    assert result["saved"] == 1 and result["errors"] == 0

    with db.connect() as conn:
        new_rows = conn.execute(
            "SELECT i.id, i.status, i.meta, s.trust FROM items i "
            "JOIN sources s ON s.id = i.source_id WHERE i.id > %s ORDER BY i.id",
            (orig_ids[-1],),
        ).fetchall()
    assert len(new_rows) == 1, "exactly ONE new item lands"
    new_id, status, meta, trust = new_rows[0]
    assert status == "active" and trust == "unverified"
    # Provenance survives on the ITEM meta, not just the review detail.
    assert meta["reprocessed_from"] == src_id
    assert meta["prev_pipeline"] == settings.kb_extract_model
    assert "reprocessed_at" in meta
    assert meta["quote"] == NEW_ITEM["quote"], "pipeline-owned meta keys are untouched"

    with db.connect() as conn:
        rq = conn.execute(
            "SELECT reason, detail FROM review_queue WHERE item_id = %s", (new_id,)
        ).fetchall()
    assert len(rq) == 1 and rq[0][0] == "unverified_extraction"
    assert rq[0][1]["reprocessed_from"] == src_id

    # Invariant 5 / never-supersede: originals stay active and unchained.
    with db.connect() as conn:
        originals = conn.execute(
            "SELECT status, superseded_by FROM items WHERE id = ANY(%s)", (orig_ids,)
        ).fetchall()
    assert all(st == "active" and sb is None for st, sb in originals)


def test_shadow_writes_nothing_but_reports_counts(monkeypatch):
    _seed_v1(monkeypatch)
    before = _counts()

    _mock_extractor(monkeypatch, [*V1_ITEMS, NEW_ITEM])
    result = reprocess.reprocess()  # default shadow=True — SAFE

    assert result["shadow"] is True
    assert _counts() == before, "shadow mode writes zero items/sources/review rows"
    assert result["would_save"] == 1, "only the genuinely new item counts as new"
    assert result["dedup"] == 2, "both v1 items collide with existing content hashes"
    assert result["saved"] == 0 and result["errors"] == 0


def test_shadow_uses_ensemble_when_live(monkeypatch):
    """Shadow mirrors the CURRENT write path: ensemble-live → run_ensemble, no v1 call."""
    _seed_v1(monkeypatch)
    monkeypatch.setattr(settings, "kb_extract_ensemble", "granite4.1:8b")
    monkeypatch.setattr(settings, "kb_ensemble_shadow", False)

    def boom(*a, **kw):
        raise AssertionError("v1 extractor must not run in ensemble-live shadow")

    monkeypatch.setattr(llm, "extract_items", boom)
    from kbworker import ensemble

    def fake_run(text, project, uri, **kw):
        return {
            "survivors": [{"item": dict(NEW_ITEM), "repair": None}],
            "unanchored": 3,
            "rejected_reps": 1,
        }

    monkeypatch.setattr(ensemble, "run_ensemble", fake_run)
    before = _counts()
    result = reprocess.reprocess()  # shadow
    assert _counts() == before
    assert result["would_save"] == 1 and result["dedup"] == 0
    assert result["rejected"] == 4


def test_session_only_and_on_disk_selection(monkeypatch):
    _seed_v1(monkeypatch)
    # A git source WITH a raw archive and a manual source must never be selected.
    ingest_mod.ingest(
        "proj",
        "note",
        "Repo doc",
        "git doc body",
        kind="git",
        uri="repo/readme.md",
        raw_content=b"raw doc bytes",
    )
    ingest_mod.ingest("proj", "note", "Manual note", "manual body", kind="manual")

    rows = reprocess.reprocessable_sources()
    assert len(rows) == 1 and rows[0]["uri"] == URI
    assert len(rows[0]["item_ids"]) == 2, "joined to the window's original items"
    assert rows[0]["prev_extractor"] == settings.kb_extract_model

    # reprocess_source refuses a non-session source outright.
    with db.connect() as conn:
        git_id = conn.execute("SELECT id FROM sources WHERE kind = 'git'").fetchone()[0]
    with pytest.raises(InvalidOperation):
        reprocess.reprocess_source(git_id, shadow=True)

    # A session source whose archive vanished from disk drops out of the listing.
    Path(rows[0]["raw_path"]).unlink()
    assert reprocess.reprocessable_sources() == []


def test_project_extractor_and_before_filters(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(settings, "kb_extract_model", "old-model:7b")  # stamps meta.extractor
        _seed_v1(m, project="proj-a")
    other = {
        "type": "note",
        "title": "DMA",
        "body": "Ring buffers are tricky.",
        "quote": "DMA ring buffers being tricky",
        "tags": [],
    }
    _mock_extractor(monkeypatch, [other])
    extract_and_ingest(
        "Entirely different transcript about DMA ring buffers being tricky.",
        "proj-b",
        "/tmp/fake/other.jsonl:0-99",
    )

    assert {r["project"] for r in reprocess.reprocessable_sources()} == {"proj-a", "proj-b"}

    rows = reprocess.reprocessable_sources(project="proj-a")
    assert [r["project"] for r in rows] == ["proj-a"]
    assert rows[0]["prev_extractor"] == "old-model:7b"

    rows = reprocess.reprocessable_sources(extractor="old-model:7b")
    assert len(rows) == 1 and rows[0]["project"] == "proj-a"
    assert reprocess.reprocessable_sources(extractor="never-used:1b") == []

    assert reprocess.reprocessable_sources(before="2000-01-01") == []
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert len(reprocess.reprocessable_sources(before=future)) == 2

    assert len(reprocess.reprocessable_sources(limit=1)) == 1


def test_missing_raw_file_error_is_isolated(monkeypatch):
    """An archive that vanishes between listing and replay logs, counts as an
    error, and the run CONTINUES to the remaining sources."""
    _seed_v1(monkeypatch)
    second = {
        "type": "note",
        "title": "adc",
        "body": "ADC reference note.",
        "quote": "ADC reference",
        "tags": [],
    }
    _mock_extractor(monkeypatch, [second])
    extract_and_ingest(
        "Second transcript with a durable note about the ADC reference.",
        "proj",
        "/tmp/fake/second.jsonl:0-77",
    )
    rows = reprocess.reprocessable_sources()
    assert len(rows) == 2
    Path(rows[0]["raw_path"]).unlink()  # TOCTOU: gone AFTER the listing
    monkeypatch.setattr(reprocess, "reprocessable_sources", lambda **kw: rows)

    result = reprocess.reprocess(shadow=True)
    assert result["sources_seen"] == 2
    assert result["errors"] == 1
    assert result["dedup"] == 1, "the surviving source was still processed"
    assert result["per_source"][0]["source_id"] == rows[0]["source_id"]
    assert "error" in result["per_source"][0]


def test_corrupt_archive_error_is_isolated(monkeypatch):
    """A corrupt on-disk archive (exists, not zstd) is isolated the same way —
    through the REAL listing path, no monkeypatching."""
    _seed_v1(monkeypatch)
    second = {
        "type": "note",
        "title": "adc",
        "body": "ADC reference note.",
        "quote": "ADC reference",
        "tags": [],
    }
    _mock_extractor(monkeypatch, [second])
    extract_and_ingest(
        "Second transcript with a durable note about the ADC reference.",
        "proj",
        "/tmp/fake/second.jsonl:0-77",
    )
    rows = reprocess.reprocessable_sources()
    Path(rows[0]["raw_path"]).write_bytes(b"definitely not a zstd frame")

    result = reprocess.reprocess(shadow=True)
    assert result["sources_seen"] == 2
    assert result["errors"] == 1
    assert result["dedup"] == 1, "run continued past the corrupt archive"


def test_cli_shadow_default_and_apply(monkeypatch):
    from kbcli.main import app

    _seed_v1(monkeypatch)
    before = _counts()
    _mock_extractor(monkeypatch, [*V1_ITEMS, NEW_ITEM])

    r = CliRunner().invoke(app, ["reprocess"])
    assert r.exit_code == 0, r.output
    assert "shadow" in r.output and "would save 1 new item(s)" in r.output
    assert "2 dedup no-op(s)" in r.output
    assert _counts() == before, "CLI default is dry-run: nothing written"

    r = CliRunner().invoke(app, ["reprocess", "--apply"])
    assert r.exit_code == 0, r.output
    assert "saved 1 new unverified item(s)" in r.output
    items, sources, review = _counts()
    assert (items, sources, review) == (before[0] + 1, before[1] + 1, before[2] + 1)

"""Phase 3 — inbox watcher: drop→searchable, dedupe, PDF/DOCX parse paths.

Drives ``watcher.process_file`` directly (no watchdog, no sleeps) against a
tmp inbox/raw via monkeypatched settings — never the real ~/kb (deliverable 8).
Ollama is mocked by the autouse ``fake_embeddings`` fixture.
"""

from __future__ import annotations

import io
import itertools
from pathlib import Path
from types import SimpleNamespace

import pytest

from kbcore import db
from kbcore.config import settings
from kbcore.search import search
from kbworker import watcher
from kbworker.watcher_parsers import ParsedFile

pytestmark = pytest.mark.usefixtures("kb")


@pytest.fixture()
def inbox_env(tmp_path, monkeypatch):
    """Point settings.inbox / settings.raw at throwaway tmp dirs (never ~/kb)."""
    inbox = tmp_path / "inbox"
    raw = tmp_path / "raw"
    inbox.mkdir()
    raw.mkdir()
    monkeypatch.setattr(settings, "kb_inbox", inbox)
    monkeypatch.setattr(settings, "kb_raw", raw)
    return inbox, raw


def _item_count() -> int:
    with db.connect() as conn:
        return conn.execute("SELECT count(*) FROM items").fetchone()[0]


def _make_pdf(text: str) -> bytes:
    """Minimal single-page PDF with extractable text (pypdf reads it, §7.1)."""
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
    ]
    stream = b"BT /F1 24 Tf 72 700 Td (" + text.encode("latin-1") + b") Tj ET"
    objs.append(b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream")
    objs.append(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(pdf))
        pdf += str(i).encode() + b" 0 obj" + body + b"endobj\n"
    xref_pos = len(pdf)
    n = len(objs) + 1
    pdf += b"xref\n0 " + str(n).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        pdf += f"{off:010d} 00000 n \n".encode()
    pdf += (
        b"trailer<</Size "
        + str(n).encode()
        + b"/Root 1 0 R>>\nstartxref\n"
        + str(xref_pos).encode()
        + b"\n%%EOF"
    )
    return bytes(pdf)


def _make_docx(text: str) -> bytes:
    from docx import Document

    doc = Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_drop_note_becomes_searchable_and_archived(inbox_env):
    inbox, raw = inbox_env
    project_dir = inbox / "aquamonix"
    project_dir.mkdir()
    dropped = project_dir / "drv-note.md"
    dropped.write_text("# DRV8706 gotcha\n\nThe DRV8706 needs SPI mode 1 on the register map.")

    item_id = watcher.process_file(dropped)

    assert item_id is not None
    assert not dropped.exists(), "inbox file must be removed after ingest"
    archives = list((raw / "aquamonix").glob("*.zst"))
    assert archives, "original bytes must be archived zstd under KB_RAW/<project>/"

    hits = search("DRV8706 register map")
    assert hits and hits[0].item_id == item_id
    top = hits[0]
    assert top.project == "aquamonix"
    assert top.trust == "verified"
    assert top.source_uri == "aquamonix/drv-note.md"


def test_redrop_identical_content_is_a_no_op(inbox_env):
    inbox, _ = inbox_env
    project_dir = inbox / "proj"
    project_dir.mkdir()
    body = "Reusable snippet: enable the watchdog before the main loop."

    first = project_dir / "note.md"
    first.write_text(body)
    assert watcher.process_file(first) is not None
    after_first = _item_count()

    # Re-drop identical content: ingest dedupes, still removes the inbox file.
    again = project_dir / "note.md"
    again.write_text(body)
    assert watcher.process_file(again) is None
    assert not again.exists()
    assert _item_count() == after_first, "identical re-drop must add zero rows"


def test_pdf_parse_path(inbox_env):
    inbox, raw = inbox_env
    project_dir = inbox / "aquamonix"
    project_dir.mkdir()
    dropped = project_dir / "spec.pdf"
    dropped.write_bytes(_make_pdf("Aquamonix DNP3 holding register map spec"))

    item_id = watcher.process_file(dropped)

    assert item_id is not None
    assert not dropped.exists()
    assert list((raw / "aquamonix").glob("*.zst"))
    hits = search("DNP3 holding register")
    assert hits and hits[0].item_id == item_id


def test_docx_parse_path(inbox_env):
    inbox, _ = inbox_env
    project_dir = inbox / "punchtech"
    project_dir.mkdir()
    dropped = project_dir / "notion.docx"
    dropped.write_bytes(_make_docx("Repeater OTA rollback runbook for the punchtech mesh"))

    item_id = watcher.process_file(dropped)

    assert item_id is not None
    assert not dropped.exists()
    hits = search("OTA rollback runbook")
    assert hits and hits[0].item_id == item_id


def test_source_file_routes_to_snippet(inbox_env):
    inbox, _ = inbox_env
    project_dir = inbox / "motor-ctl"
    project_dir.mkdir()
    dropped = project_dir / "spi.c"
    dropped.write_text("void drv_spi_write(uint8_t reg) {\n    // set CPHA=1\n}\n")

    item_id = watcher.process_file(dropped)

    assert item_id is not None
    from kbcore.items import get_item

    assert get_item(item_id).type == "snippet"


def test_top_level_drop_lands_in_unsorted(inbox_env):
    inbox, _ = inbox_env
    dropped = inbox / "loose.txt"
    dropped.write_text("a loose thought about ring buffers and UART overruns")

    item_id = watcher.process_file(dropped)

    assert item_id is not None
    from kbcore.items import get_item

    assert get_item(item_id).project == "unsorted"


def test_wait_stable_false_on_growing_file(tmp_path, monkeypatch):
    # Defect #8: a file that never stops growing must return False (leave it for
    # a later poll) instead of returning True after exhausting the tries.
    monkeypatch.setattr(watcher.time, "sleep", lambda _s: None)  # no real waiting
    sizes = itertools.count(100, 10)  # strictly increasing forever
    monkeypatch.setattr(Path, "stat", lambda self, *a, **k: SimpleNamespace(st_size=next(sizes)))
    assert watcher._wait_stable(tmp_path / "growing.bin") is False


def test_safe_process_skips_unstable_file(inbox_env, monkeypatch):
    # Defect #8: when the stability guard says "not settled", the file is left
    # in place and nothing is ingested.
    inbox, _ = inbox_env
    proj = inbox / "p"
    proj.mkdir()
    f = proj / "note.md"
    f.write_text("partial content still being written")
    monkeypatch.setattr(watcher, "_wait_stable", lambda _p: False)

    watcher._safe_process(f)

    assert f.exists(), "unstable file must be left in the inbox for a later poll"
    assert _item_count() == 0


def test_dup_drop_with_different_raw_is_archived(inbox_env):
    # Defect #9: a re-drop whose raw bytes differ but scrub to the same body
    # dedupes as an item yet its distinct original is still archived.
    inbox, raw = inbox_env
    proj = inbox / "cfgproj"
    proj.mkdir()
    tok1, tok2 = "ghp_" + "0" * 36, "ghp_" + "1" * 36
    f = proj / "cfg.md"

    f.write_text(f"API token={tok1} for the config service")
    assert watcher.process_file(f) is not None
    assert len(list((raw / "cfgproj").glob("*.zst"))) == 1

    f.write_text(f"API token={tok2} for the config service")  # same path, different bytes
    assert watcher.process_file(f) is None, "scrubs to an already-ingested body → dedupe"
    assert len(list((raw / "cfgproj").glob("*.zst"))) == 2, "distinct original preserved"
    assert not f.exists()


def test_empty_text_file_is_archived_not_ingested(inbox_env, monkeypatch):
    # Defect #10: a text-less file (scanned PDF etc.) must not create a
    # chunk-less, search-invisible item; archive the raw and clear the inbox.
    inbox, raw = inbox_env
    proj = inbox / "scanned"
    proj.mkdir()
    f = proj / "image.pdf"
    f.write_bytes(b"%PDF-fake-scanned-bytes")
    monkeypatch.setattr(
        watcher,
        "parse",
        lambda _p: ParsedFile(
            raw=b"%PDF-fake-scanned-bytes", text="   \n  ", type="note", is_source=False
        ),
    )

    assert watcher.process_file(f) is None
    assert _item_count() == 0
    assert list((raw / "scanned").glob("*.zst")), "raw archived even without extractable text"
    assert not f.exists()


def test_reposcan_due_runs_at_startup_then_by_interval():
    # Defect #11: due immediately at startup (last is None), then by elapsed.
    assert watcher._reposcan_due(None, 5.0) is True
    assert watcher._reposcan_due(100.0, 100.0 + watcher.REPOSCAN_INTERVAL - 1) is False
    assert watcher._reposcan_due(100.0, 100.0 + watcher.REPOSCAN_INTERVAL) is True

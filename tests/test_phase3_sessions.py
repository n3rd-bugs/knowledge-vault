"""Phase 3 — session collector: quote-grounding rejection + checkpoint resume.

The extractor (kbcore.llm) is mocked per CLAUDE.md (mock only Ollama); the DB is
the real compose Postgres in kb_test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kbcore import db, llm
from kbcore.checkpoints import set_checkpoint
from kbcore.config import settings
from kbcore.items import get_item
from kbworker import sessions
from kbworker.sessions import ingest_session

pytestmark = pytest.mark.usefixtures("kb")


@pytest.fixture(autouse=True)
def _tmp_raw(tmp_path, monkeypatch):
    """Keep raw archives and session lock files off the real ~/kb during tests."""
    monkeypatch.setattr(settings, "kb_raw", tmp_path / "kbraw")


def _write_jsonl(path: Path, objs: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for obj in objs:
            fh.write(json.dumps(obj) + "\n")


def _user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def test_hallucinated_quote_rejected_grounded_saved(tmp_path, monkeypatch):
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            _user("Why did we choose SPI mode 1 for the DRV8706?"),
            _assistant("Because mode 0 reads all zeros; the DRV8706 needs CPHA=1."),
        ],
    )

    def fake_extract(text: str) -> list[dict]:
        return [
            {
                "type": "decision",
                "title": "SPI mode 1 for DRV8706",
                "body": "Use SPI mode 1 (CPHA=1); mode 0 reads all zeros.",
                "quote": "the DRV8706 needs CPHA=1",  # verbatim substring
                "tags": ["hw:drv8706", "protocol:spi"],
            },
            {
                "type": "note",
                "title": "hallucination",
                "body": "invented claim",
                "quote": "the moon is made of green cheese",  # NOT in transcript
                "tags": [],
            },
        ]

    monkeypatch.setattr(llm, "extract_items", fake_extract)

    summary = ingest_session(
        transcript,
        project="motor-ctl",
        repo="/home/usama/UpWork/motor-ctl",
        branch="main",
        commit="a1b2c3d",
    )

    assert summary.saved == 1
    assert summary.rejected == 1
    assert summary.offset == transcript.stat().st_size

    with db.connect() as conn:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT i.id FROM items i JOIN sources s ON s.id = i.source_id "
                "WHERE s.kind = 'session'"
            ).fetchall()
        ]
    assert len(ids) == 1, "only the grounded item is stored"
    item = get_item(ids[0])
    assert item.trust == "unverified"
    assert item.meta["quote"] == "the DRV8706 needs CPHA=1"
    assert item.meta["extractor"] == settings.kb_extract_model
    assert item.meta.get("branch") == "main"
    assert item.code_ref is not None
    assert item.code_ref.repo == "motor-ctl"  # basename of --repo
    assert item.code_ref.path == "."
    assert item.code_ref.commit == "a1b2c3d"

    with db.connect() as conn:
        queue = conn.execute("SELECT item_id, reason, detail FROM review_queue").fetchall()
    assert len(queue) == 1
    q_item, reason, detail = queue[0]
    assert q_item == item.item_id
    assert reason == "unverified_extraction"
    assert detail["rejected_count"] == 1


def test_checkpoint_resumes_mid_file(tmp_path, monkeypatch):
    transcript = tmp_path / "growing.jsonl"
    _write_jsonl(transcript, [_user("First question about SPI timing.")])

    seen: list[str] = []

    def recording_extract(text: str) -> list[dict]:
        seen.append(text)
        return []  # no items — we only care what text the extractor is handed

    monkeypatch.setattr(llm, "extract_items", recording_extract)

    s1 = ingest_session(transcript, project="p")
    size1 = transcript.stat().st_size
    assert s1.offset == size1
    assert seen == ["First question about SPI timing."]

    # Append a new line; only the NEW content should reach the extractor.
    _write_jsonl(transcript, [_assistant("Second answer about DMA ring buffers.")])
    size2 = transcript.stat().st_size
    s2 = ingest_session(transcript, project="p")
    assert s2.offset == size2 > size1
    assert seen[1] == "Second answer about DMA ring buffers."
    assert "First question" not in seen[1], "old content must not be re-read"

    # Re-run with nothing appended: a pure no-op, extractor untouched.
    s3 = ingest_session(transcript, project="p")
    assert s3.offset == size2
    assert s3.saved == 0
    assert len(seen) == 2, "no new bytes ⇒ extractor is not called again"


def test_missing_transcript_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "extract_items", lambda text: [])
    summary = ingest_session(tmp_path / "does-not-exist.jsonl", project="p")
    assert summary.saved == 0 and summary.rejected == 0


def test_half_written_trailing_line_is_not_lost(tmp_path, monkeypatch):
    # Defect #12: consume only up to the last complete line so a partially
    # written trailing line is recovered (never lost) once the writer finishes.
    transcript = tmp_path / "t.jsonl"
    line1 = json.dumps(_user("Complete first message about I2C pullups."))
    full_line2 = json.dumps(_assistant("Second message about DMA now complete."))
    partial = full_line2[:20]  # truncated, no newline yet
    transcript.write_bytes((line1 + "\n" + partial).encode())

    seen: list[str] = []
    monkeypatch.setattr(llm, "extract_items", lambda t: seen.append(t) or [])

    s1 = ingest_session(transcript, project="p")
    assert seen == ["Complete first message about I2C pullups."]
    assert s1.offset == len(line1) + 1, "checkpoint stops at the last complete line"

    # Writer completes line2 (append the remainder + newline).
    with transcript.open("ab") as fh:
        fh.write((full_line2[20:] + "\n").encode())
    seen.clear()
    s2 = ingest_session(transcript, project="p")
    assert seen == ["Second message about DMA now complete."], "completed line recovered"
    assert s2.offset == transcript.stat().st_size


def test_uri_uses_abspath_and_dedupes_on_retry(tmp_path, monkeypatch):
    # Defect #14: uri keyed by the canonical abs path + consumed range, so a
    # relative/absolute invocation (or a crash-retry) yields identical dedupe keys.
    transcript = tmp_path / "conv.jsonl"
    _write_jsonl(transcript, [_user("Decided to use mode 1 on the DRV8706 SPI bus.")])
    key = str(transcript.resolve())
    grounded = {
        "type": "decision",
        "title": "t",
        "body": "b",
        "quote": "mode 1 on the DRV8706",
        "tags": [],
    }
    monkeypatch.setattr(llm, "extract_items", lambda t: [grounded])

    weird = tmp_path / "sub" / ".." / "conv.jsonl"  # resolves to the same file
    assert ingest_session(weird, project="p").saved == 1

    with db.connect() as conn:
        uris = [
            r[0] for r in conn.execute("SELECT uri FROM sources WHERE kind='session'").fetchall()
        ]
    assert len(uris) == 1
    assert uris[0].startswith(key + ":"), "uri keyed by resolved abs path"

    # Simulate a crash-retry: reset the checkpoint, re-run with the absolute path.
    set_checkpoint(key, 0)
    ingest_session(str(transcript), project="p")
    with db.connect() as conn:
        n = conn.execute("SELECT count(*) FROM sources WHERE kind='session'").fetchone()[0]
    assert n == 1, "identical window re-read → identical uri → content-hash dedupe"


def test_shrunk_transcript_resets_and_reprocesses(tmp_path, monkeypatch):
    # Defect #16: a truncated/rotated transcript (size < checkpoint) must reset
    # to 0 and reprocess, not wedge forever on an empty read past EOF.
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(transcript, [_user("A durable note about the SPI clock divider.")])
    key = str(transcript.resolve())
    set_checkpoint(key, 10_000)  # checkpoint way past current EOF

    grounded = {
        "type": "note",
        "title": "n",
        "body": "b",
        "quote": "SPI clock divider",
        "tags": [],
    }
    monkeypatch.setattr(llm, "extract_items", lambda t: [grounded])

    summary = ingest_session(transcript, project="p")
    assert summary.saved == 1
    assert summary.offset == transcript.stat().st_size


def test_item_and_review_row_are_atomic(tmp_path, monkeypatch):
    # Defect #15: item insert + review-queue insert share one transaction — if
    # the queue insert fails, the unverified item must NOT persist.
    transcript = tmp_path / "atomic.jsonl"
    _write_jsonl(transcript, [_user("A grounded decision about the ADC reference.")])
    grounded = {
        "type": "decision",
        "title": "t",
        "body": "b",
        "quote": "grounded decision about the ADC reference",
        "tags": [],
    }
    monkeypatch.setattr(llm, "extract_items", lambda t: [grounded])

    def boom(*a, **k):
        raise RuntimeError("review-queue insert failed")

    monkeypatch.setattr(sessions, "queue_review_in", boom)

    with pytest.raises(RuntimeError):
        ingest_session(transcript, project="p")

    with db.connect() as conn:
        items = conn.execute(
            "SELECT count(*) FROM items i JOIN sources s ON s.id = i.source_id "
            "WHERE s.kind = 'session'"
        ).fetchone()[0]
        rq = conn.execute("SELECT count(*) FROM review_queue").fetchone()[0]
    assert items == 0, "item insert rolled back with the failing review-queue insert"
    assert rq == 0


def test_stored_quote_is_scrubbed(tmp_path, monkeypatch):
    # Defect #18: a secret pasted in chat must not become searchable via meta.quote.
    fake = "ghp_" + "0" * 36
    transcript = tmp_path / "leak.jsonl"
    _write_jsonl(transcript, [_assistant(f"Set the token to {fake} in the env.")])
    grounded = {
        "type": "gotcha",
        "title": "t",
        "body": "b",
        "quote": f"Set the token to {fake} in the env.",
        "tags": [],
    }
    monkeypatch.setattr(llm, "extract_items", lambda t: [grounded])

    ingest_session(transcript, project="p")

    with db.connect() as conn:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT i.id FROM items i JOIN sources s ON s.id = i.source_id "
                "WHERE s.kind = 'session'"
            ).fetchall()
        ]
    assert len(ids) == 1
    stored_quote = get_item(ids[0]).meta["quote"]
    assert fake not in stored_quote
    assert "[REDACTED:github_token]" in stored_quote

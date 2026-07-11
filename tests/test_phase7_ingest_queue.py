"""Phase 7 — queued remote session/memory ingestion (Invariant 8).

Store+scrub at submit (no GPU); the 14B runs only in the nightly consolidation
drain. Real DB in kb_test; the extractor (llm.extract_items) is mocked — the real
14B is never called.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from kbcore import db, ingest_queue, llm
from kbcore.config import settings
from kbcore.errors import InvalidOperation
from kbcore.items import get_item
from kbworker import consolidate

TEST_TOKEN = "kbtest-" + "0" * 32
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture(autouse=True)
def _tmp_raw(tmp_path, monkeypatch):
    """Keep raw archives off the real ~/kb during drain tests."""
    monkeypatch.setattr(settings, "kb_raw", tmp_path / "kbraw")


# --- enqueue: scrub, idempotency, validation ---------------------------------


def test_enqueue_scrubs_and_is_idempotent(kb):
    fake = "ghp_" + "0" * 36
    content = f"We set the token to {fake} then moved on."
    qid = ingest_queue.enqueue("p", content, kind="session", repo="/x/r", commit="deadbee").id
    assert qid is not None

    with db.connect() as conn:
        stored, meta = conn.execute(
            "SELECT content, meta FROM ingest_queue WHERE id = %s", (qid,)
        ).fetchone()
    assert fake not in stored, "raw secret must NEVER persist"
    assert "[REDACTED:github_token]" in stored
    assert meta["redactions"] == {"github_token": 1}
    assert meta["repo"] == "/x/r" and meta["commit"] == "deadbee"

    # identical content → no-op (dedupe on scrubbed hash)
    assert ingest_queue.enqueue("p", content, kind="session").id is None
    with db.connect() as conn:
        n = conn.execute("SELECT count(*) FROM ingest_queue").fetchone()[0]
    assert n == 1


def test_enqueue_rejects_bad_input(kb):
    with pytest.raises(InvalidOperation):
        ingest_queue.enqueue("p", "   ")  # empty
    with pytest.raises(InvalidOperation):
        ingest_queue.enqueue("p", "x" * (ingest_queue.MAX_CONTENT_BYTES + 1))  # oversized
    with pytest.raises(InvalidOperation):
        ingest_queue.enqueue("p", "hello", kind="bogus")  # bad kind
    with pytest.raises(InvalidOperation):
        ingest_queue.enqueue("", "hello")  # no project


def test_enqueue_does_not_call_the_extractor(kb, monkeypatch):
    def boom(_text):
        raise AssertionError("extractor called at submit — that would burn GPU!")

    monkeypatch.setattr(llm, "extract_items", boom)
    assert ingest_queue.enqueue("p", "some content", kind="session").id is not None


# --- nightly drain -----------------------------------------------------------


def _grounded_and_hallucinated(text):
    return [
        {
            "type": "note",
            "title": "grounded",
            "body": "a real grounded note",
            "quote": text[:15],  # always a verbatim substring
            "tags": ["hw:tps55288"],
        },
        {
            "type": "note",
            "title": "halluc",
            "body": "fabricated",
            "quote": "THIS QUOTE IS NOT IN THE TEXT",
            "tags": [],
        },
    ]


def test_drain_extracts_grounded_marks_rows_and_is_noop_second_time(kb, monkeypatch):
    monkeypatch.setattr(llm, "extract_items", _grounded_and_hallucinated)
    transcript = json.dumps(
        {"type": "user", "message": {"content": "We chose SPI mode 1 for the DRV8706 bus."}}
    )
    ingest_queue.enqueue(
        "motor-ctl", transcript, kind="session", repo="/h/motor-ctl", commit="abc1234"
    )
    ingest_queue.enqueue(
        "notes", "Remember: the pump uses a 24V rail and trips at 30V.", kind="memory"
    )

    drained, saved = consolidate._drain_ingest_queue()
    assert drained == 2 and saved == 2

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT status, saved_count, rejected_count FROM ingest_queue ORDER BY id"
        ).fetchall()
    assert all(r == ("extracted", 1, 1) for r in rows), "both rows extracted, 1 saved + 1 rejected"

    with db.connect() as conn:
        items = conn.execute(
            "SELECT i.meta, s.trust, s.uri FROM items i JOIN sources s ON s.id = i.source_id "
            "WHERE s.kind = 'session' ORDER BY i.id"
        ).fetchall()
    assert len(items) == 2
    for meta, trust, _uri in items:
        assert trust == "unverified" and "quote" in meta
    session_item = next(m for m, _t, uri in items if uri.startswith("queue:session:"))
    assert session_item["code_ref"]["repo"] == "motor-ctl"
    assert session_item["code_ref"]["commit"] == "abc1234"

    with db.connect() as conn:
        details = [
            r[0]
            for r in conn.execute(
                "SELECT detail FROM review_queue WHERE reason = 'unverified_extraction'"
            ).fetchall()
        ]
    assert len(details) == 2
    assert all(d["rejected_count"] == 1 and "ingest_queue_id" in d for d in details)

    # second drain: rows already 'extracted' → nothing pending
    assert consolidate._drain_ingest_queue() == (0, 0)


def test_consolidate_main_drains_and_reports(kb, monkeypatch):
    monkeypatch.setattr(
        llm,
        "extract_items",
        lambda text: [{"type": "note", "title": "n", "body": "b", "quote": text[:10], "tags": []}],
    )
    monkeypatch.setattr(llm, "summarize_items", lambda topic, items: "summary")
    monkeypatch.setattr(llm, "items_contradict", lambda *a, **k: False)
    ingest_queue.enqueue("p", "some memory content to extract from tonight", kind="memory")

    result = consolidate.main()
    assert result["queued_drained"] == 1 and result["queued_items_saved"] == 1

    report = get_item(result["report_item_id"])
    assert report.meta["queued_drained"] == 1 and report.meta["queued_items_saved"] == 1

    with db.connect() as conn:
        extracted = conn.execute(
            "SELECT count(*) FROM ingest_queue WHERE status = 'extracted'"
        ).fetchone()[0]
    assert extracted == 1
    assert consolidate.main()["queued_drained"] == 0, "second run has nothing to drain"


# --- REST endpoint (mirrors the MCP tool) ------------------------------------


def test_rest_ingest_session_enqueues_without_extractor(kb, monkeypatch):
    def boom(_text):
        raise AssertionError("extractor called at submit!")

    monkeypatch.setattr(llm, "extract_items", boom)
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbrest.server import build_app

    client = TestClient(build_app())
    body = {"project": "p", "transcript": "hello memory dump", "kind": "memory"}
    r = client.post("/api/ingest-session", headers=AUTH, json=body)
    assert r.status_code == 202
    assert isinstance(r.json()["queue_id"], int) and r.json()["duplicate"] is False

    dup = client.post("/api/ingest-session", headers=AUTH, json=body)
    assert dup.status_code == 202
    assert dup.json()["duplicate"] is True and dup.json()["queue_id"] is None

    # unauthenticated → 401 (same bearer as the rest of the facade)
    assert client.post("/api/ingest-session", json=body).status_code == 401


# --- migration 002 -----------------------------------------------------------


def test_migration_002_applied_and_idempotent(kb):
    from kbcore.migrate import migrate

    with db.connect() as conn:
        assert conn.execute("SELECT to_regclass('ingest_queue')").fetchone()[0] is not None
    assert migrate() == [], "re-running migrations is a no-op"


# --- review-fix regressions -------------------------------------------------


def test_assemble_text_memory_keeps_all_lines_despite_a_json_line(kb):
    from kbworker.sessions import assemble_text

    blob = 'Postgres runs on port 5432.\n{"role":"user","content":"hi"}\nThe pump trips at 30V.\n'
    # memory: never JSONL-collapsed
    out = assemble_text(blob, "memory")
    assert "5432" in out and "30V" in out
    # session that is mostly prose: also preserved (one stray JSON line doesn't win)
    out2 = assemble_text(blob, "session")
    assert "5432" in out2 and "30V" in out2


def test_assemble_text_real_transcript_still_assembles(kb):
    from kbworker.sessions import assemble_text

    tx = (
        '{"type":"user","message":{"content":[{"type":"text","text":"question about SPI"}]}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text","text":"answer about SPI"}]}}\n'
    )
    out = assemble_text(tx, "session")
    assert "question about SPI" in out and "answer about SPI" in out


def test_enqueue_rejects_reserved_project(kb):
    with pytest.raises(InvalidOperation, match="reserved"):
        ingest_queue.enqueue("kb-system", "some conversation text", kind="session")


def test_enqueue_scrubs_meta_source_uri(kb):
    fake = "ghp_" + "0" * 36
    qid = ingest_queue.enqueue("p", "hello world", kind="session", source_uri=f"leaked {fake}").id
    with db.connect() as conn:
        meta = conn.execute("SELECT meta FROM ingest_queue WHERE id=%s", (qid,)).fetchone()[0]
    assert fake not in str(meta)
    assert "[REDACTED:github_token]" in meta["source_uri"]


def test_enqueue_rejects_when_backlog_full(kb, monkeypatch):
    monkeypatch.setattr(ingest_queue, "MAX_PENDING", 1)
    ingest_queue.enqueue("p", "first submission text", kind="session")
    with pytest.raises(InvalidOperation, match="queue is full"):
        ingest_queue.enqueue("p", "second distinct submission text", kind="session")


# --- large-session chunked flush (owner request: up to 1 GB) -------------------------


def test_text_windows_line_boundaries_and_cap():
    from kbworker.sessions import _text_windows

    text = "".join(f"line {i} " + "x" * 90 + "\n" for i in range(50))  # ~5KB
    wins = _text_windows(text, 1000)
    assert len(wins) > 3
    assert "".join(wins) == text, "windows reassemble losslessly"
    for w in wins:
        assert len(w.encode()) <= 1000
        assert w.endswith("\n"), "windows cut on line boundaries"


def test_text_windows_monster_line_hard_split():
    from kbworker.sessions import _text_windows

    text = "é" * 2000  # multibyte, single line, > cap
    wins = _text_windows(text, 1000)
    assert len(wins) >= 4
    assert "".join(wins) == text, "hard split loses no characters"
    for w in wins:
        assert len(w.encode()) <= 1000


def test_text_windows_total_cap(monkeypatch):
    from kbworker import sessions

    monkeypatch.setattr(sessions, "MAX_DELTA_BYTES", 5_000)
    import pytest as _pytest

    from kbcore.errors import InvalidOperation

    with _pytest.raises(InvalidOperation):
        sessions._text_windows("x" * 6_000, 1000)


def test_local_queue_flush_chunks_large_delta(kb, tmp_path, monkeypatch):
    """A >window transcript enqueues as MULTIPLE rows and advances the checkpoint
    (previously it raised on the 1MB row cap and wedged forever)."""
    import json as _json

    from kbcore import db, ingest_queue
    from kbworker import sessions

    monkeypatch.setattr(sessions, "QUEUE_WINDOW_BYTES", 2_000)  # small for the test
    t = tmp_path / "big.jsonl"
    with t.open("w") as f:
        for i in range(60):
            f.write(
                _json.dumps(
                    {"type": "user", "message": {"role": "user", "content": f"m{i} " + "y" * 80}}
                )
                + "\n"
            )
    s = sessions.ingest_session(t, "bigflush", queue=True)
    assert s.saved >= 2, "delta split into multiple queue rows"
    assert s.offset == t.stat().st_size, "checkpoint advanced past the whole delta"
    with db.connect() as conn:
        parts = conn.execute(
            "SELECT count(*) FROM ingest_queue WHERE project = 'bigflush' "
            "AND meta->>'source_uri' LIKE '%#p%'"
        ).fetchone()[0]
    assert parts == s.saved, "each row carries its per-part uri (dedupe-safe)"
    # re-run is a no-op (checkpoint advanced)
    s2 = sessions.ingest_session(t, "bigflush", queue=True)
    assert s2.saved == 0
    assert ingest_queue.queue_depth() >= parts

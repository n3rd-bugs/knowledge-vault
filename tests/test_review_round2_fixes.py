"""Fixes for the second-pass (resumed) multi-agent review findings."""

from __future__ import annotations

from kbcore import db
from kbcore.scrub import scrub


def test_scrub_strips_nul():
    clean, red = scrub("before\x00after\x00end")
    assert "\x00" not in clean
    assert clean == "beforeafterend"
    assert red["nul_stripped"] == 2


def test_transient_extractor_outage_leaves_row_pending(kb, monkeypatch):
    """A transient ExtractorUnavailable must NOT mark the row failed (which would
    strand it forever behind the content-hash dedupe)."""
    from kbcore import ingest_queue
    from kbcore.errors import ExtractorUnavailable
    from kbworker import consolidate, sessions

    qid = ingest_queue.enqueue("drainproj", "User: x\nAssistant: y", kind="session")
    assert qid is not None

    def boom(*a, **k):
        raise ExtractorUnavailable("ollama restarting")

    monkeypatch.setattr(sessions, "extract_and_ingest", boom)
    consolidate._drain_ingest_queue()
    with db.connect() as conn:
        status = conn.execute("SELECT status FROM ingest_queue WHERE id = %s", (qid,)).fetchone()[0]
    assert status == "pending", "transient outage must leave the row retriable"


def test_permanent_error_still_marks_failed(kb, monkeypatch):
    from kbcore import ingest_queue
    from kbworker import consolidate, sessions

    qid = ingest_queue.enqueue("drainproj2", "User: x\nAssistant: y", kind="session")

    def boom(*a, **k):
        raise ValueError("genuinely malformed")

    monkeypatch.setattr(sessions, "extract_and_ingest", boom)
    consolidate._drain_ingest_queue()
    with db.connect() as conn:
        status = conn.execute("SELECT status FROM ingest_queue WHERE id = %s", (qid,)).fetchone()[0]
    assert status == "failed", "a real defect still fails the row"


def test_whitespace_only_window_skipped(kb, tmp_path, monkeypatch):
    import json as _json

    from kbcore import db as _db
    from kbworker import sessions

    monkeypatch.setattr(sessions, "QUEUE_WINDOW_BYTES", 2000)
    t = tmp_path / "ws.jsonl"
    lines = [{"type": "user", "message": {"role": "user", "content": "real content here"}}]
    # a giant whitespace-only assistant "line" that forms its own window
    lines.append({"type": "assistant", "message": {"role": "assistant", "content": " " * 5000}})
    with t.open("w") as f:
        for ln in lines:
            f.write(_json.dumps(ln) + "\n")
    s = sessions.ingest_session(t, "wsproj", queue=True)
    with _db.connect() as conn:
        rows = conn.execute("SELECT content FROM ingest_queue WHERE project = 'wsproj'").fetchall()
    assert all(c.strip() for (c,) in rows), "no whitespace-only rows enqueued"
    assert s.saved == len(rows)


def test_failed_row_self_heals_on_retry(kb, monkeypatch):
    """A failed row is re-drained and recovers when the cause clears — no
    operator requeue needed (standalone self-heal)."""
    from kbcore import ingest_queue
    from kbworker import consolidate, sessions

    qid = ingest_queue.enqueue("healproj", "User: a\nAssistant: b", kind="session")
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("a transient db hiccup")
        return (1, 0)

    monkeypatch.setattr(sessions, "extract_and_ingest", flaky)
    consolidate._drain_ingest_queue()  # attempt 1 fails
    with db.connect() as conn:
        st, att = conn.execute(
            "SELECT status, attempts FROM ingest_queue WHERE id = %s", (qid,)
        ).fetchone()
    assert st == "failed" and att == 1
    consolidate._drain_ingest_queue()  # retried, now succeeds
    with db.connect() as conn:
        st = conn.execute("SELECT status FROM ingest_queue WHERE id = %s", (qid,)).fetchone()[0]
    assert st == "extracted", "the failed row self-healed without any manual requeue"


def test_failed_row_retries_are_bounded(kb, monkeypatch):
    from kbcore import ingest_queue
    from kbworker import consolidate, sessions

    qid = ingest_queue.enqueue("healproj2", "User: a\nAssistant: b", kind="session")

    def always_bad(*a, **k):
        raise ValueError("genuinely malformed forever")

    monkeypatch.setattr(sessions, "extract_and_ingest", always_bad)
    for _ in range(6):
        consolidate._drain_ingest_queue()
    with db.connect() as conn:
        st, att = conn.execute(
            "SELECT status, attempts FROM ingest_queue WHERE id = %s", (qid,)
        ).fetchone()
    assert st == "failed" and att == ingest_queue.MAX_ATTEMPTS, "retries stop after MAX_ATTEMPTS"

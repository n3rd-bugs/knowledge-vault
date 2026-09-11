"""Wave S item S3 — flush stamps -> ``meta.flush`` (hybrid_v1 §1.10 cases 11-12, 422 shapes).

Stamps are TELEMETRY: stored when consistent with the content, counted and
dropped otherwise, never part of the dedupe hash and never a reason to refuse.
Real DB in kb_test only.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from kbcore import db, ingest_queue, queue_guard
from kbcore.config import settings
from kbcore.models import FlushStamp

pytestmark = pytest.mark.usefixtures("kb")

TEST_TOKEN = "kbtest-" + "0" * 32
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}
URI = "session:0a1b2c3d-stamps"
TEXT = "user: where do offsets live?\nassistant: state/*.offset, never deleted.\n"
RAW = len(TEXT.encode())


@pytest.fixture(autouse=True)
def _guard_on(monkeypatch):
    monkeypatch.setattr(settings, "kb_ingest_resend_guard", True)
    monkeypatch.setattr(settings, "kb_ingest_xsession_guard", True)
    queue_guard.COUNTERS["flush_stamp_inconsistent"] = 0
    with db.transaction() as conn:  # schema-QUALIFIED, like conftest's kb fixture
        assert conn.execute("SELECT current_schema()").fetchone()[0] == "kb_test"
        conn.execute("TRUNCATE kb_test.ingest_resend_events")
    yield


def _meta(qid: int) -> dict:
    with db.connect() as conn:
        return conn.execute("SELECT meta FROM ingest_queue WHERE id = %s", (qid,)).fetchone()[0]


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbrest.server import build_app

    return TestClient(build_app())


def _body(**flush) -> dict:
    return {"project": "proj-a", "transcript": TEXT, "session_uri": URI, **flush}


# (11) Inv 3 --------------------------------------------------------------------------


def test_the_same_chunk_with_different_offsets_is_one_row(monkeypatch):
    client = _client(monkeypatch)
    first = client.post(
        "/api/ingest-session",
        headers=AUTH,
        json=_body(flush_offset=0, flush_bytes=RAW, flush_size=RAW * 4),
    )
    second = client.post(
        "/api/ingest-session",
        headers=AUTH,
        json=_body(flush_offset=100, flush_bytes=RAW, flush_size=RAW * 4),
    )
    assert first.status_code == 202 and isinstance(first.json()["queue_id"], int)
    assert second.status_code == 202 and second.json()["duplicate"] is True
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM ingest_queue").fetchone()[0] == 1
    assert _meta(first.json()["queue_id"])["flush"]["offset"] == 0


# (12) storage, consistency, overlap ---------------------------------------------------


def test_a_consistent_stamp_is_stored_with_iso_utc_mtime():
    stamp = FlushStamp(
        offset=10,
        bytes=RAW,
        size=RAW + 10,
        event="Stop",
        mtime=1_700_000_000,
        box="rm1",
        project_by="gitfirst",
        branch_of="abcd1234",
    )
    qid = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI, flush=stamp).id
    assert _meta(qid)["flush"] == {
        "offset": 10,
        "bytes": RAW,
        "size": RAW + 10,
        "event": "Stop",
        "mtime": "2023-11-14T22:13:20+00:00",
        "box": "rm1",
        "project_by": "gitfirst",
        "branch_of": "abcd1234",
    }
    assert "overlap" not in _meta(qid)
    assert queue_guard.COUNTERS["flush_stamp_inconsistent"] == 0


@pytest.mark.parametrize(
    "stamp",
    [
        FlushStamp(offset=0, bytes=RAW + 100, size=RAW * 4),  # bytes off by 100
        FlushStamp(offset=RAW * 4, bytes=RAW, size=RAW * 4),  # offset + bytes > size
    ],
)
def test_an_inconsistent_stamp_is_dropped_and_counted_but_the_row_is_stored(stamp):
    qid = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI, flush=stamp).id
    assert qid is not None
    assert "flush" not in _meta(qid) and "overlap" not in _meta(qid)
    assert queue_guard.COUNTERS["flush_stamp_inconsistent"] == 1


def test_overlap_is_computed_per_principal_from_stamped_rows():
    from kbcore import scope

    ingest_queue.enqueue(
        "proj-a", TEXT, source_uri=URI, flush=FlushStamp(offset=0, bytes=RAW, size=RAW * 4)
    )
    second = "user: second chunk\nassistant: covers the first one's tail.\n"
    raw2 = len(second.encode())
    half = RAW // 2
    covered = ingest_queue.enqueue(
        "proj-a", second, source_uri=URI, flush=FlushStamp(offset=half, bytes=raw2, size=RAW * 4)
    ).id
    assert _meta(covered)["overlap"] == {"covered_end": RAW, "bytes": RAW - half}
    # Another principal's stamped rows are not this box's coverage: no overlap.
    other = scope.Principal(
        name="box-b", read_projects=frozenset({"proj-b"}), write_projects=frozenset({"proj-b"})
    )
    third = "user: third chunk\nassistant: a different box entirely.\n"
    with scope.use_principal(other):
        qid = ingest_queue.enqueue(
            "proj-b",
            third,
            source_uri=URI,
            flush=FlushStamp(offset=half, bytes=len(third.encode()), size=RAW * 4),
        ).id
    assert "overlap" not in _meta(qid) and "flush" in _meta(qid)


# 422 shapes + all-or-nothing --------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"flush_offset": -1, "flush_bytes": RAW, "flush_size": RAW},
        {"flush_offset": 0, "flush_bytes": RAW, "flush_size": RAW, "flush_event": "Pre Compact"},
        {"flush_offset": 0, "flush_bytes": RAW, "flush_size": RAW, "flush_box": "b" * 65},
        {"flush_offset": 0, "flush_bytes": RAW, "flush_size": RAW, "flush_project_by": "Record"},
    ],
)
def test_malformed_stamps_are_422(monkeypatch, bad):
    client = _client(monkeypatch)
    assert client.post("/api/ingest-session", headers=AUTH, json=_body(**bad)).status_code == 422
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM ingest_queue").fetchone()[0] == 0


def test_a_partial_stamp_is_dropped_whole_not_refused(monkeypatch):
    client = _client(monkeypatch)
    r = client.post("/api/ingest-session", headers=AUTH, json=_body(flush_offset=0))
    assert r.status_code == 202 and isinstance(r.json()["queue_id"], int)
    assert "flush" not in _meta(r.json()["queue_id"])
    assert queue_guard.COUNTERS["flush_stamp_inconsistent"] == 0


def test_box_label_is_scrubbed_like_every_meta_string():
    fake = "ghp_" + "0" * 36
    stamp = FlushStamp(offset=0, bytes=RAW, size=RAW, box=fake)
    qid = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI, flush=stamp).id
    assert fake not in str(_meta(qid)) and "[REDACTED" in _meta(qid)["flush"]["box"]


# preserve fragment: a same-project re-arm keeps the first-seen telemetry -----------------


def test_a_same_project_rearm_carries_flush_and_overlap_forward():
    stamp = FlushStamp(offset=0, bytes=RAW, size=RAW * 4, box="rm1")
    qid = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI, flush=stamp).id
    with db.transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET status = 'failed', attempts = 3, "
            'meta = meta || \'{"overlap": {"covered_end": 5, "bytes": 5}}\' WHERE id = %s',
            (qid,),
        )
    # An unstamped resubmission (an older hook) must not wipe the row's telemetry ...
    res = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI)
    assert res.id == qid and res.requeued is True
    meta = _meta(qid)
    assert meta["flush"]["box"] == "rm1" and meta["overlap"] == {"covered_end": 5, "bytes": 5}
    # ... and a re-stamped one keeps the FIRST-seen stamp (the row's own history).
    with db.transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET status = 'failed', attempts = 3 WHERE id = %s", (qid,)
        )
    restamp = FlushStamp(offset=0, bytes=RAW, size=RAW * 4, box="x")
    ingest_queue.enqueue("proj-a", TEXT, source_uri=URI, flush=restamp)
    assert _meta(qid)["flush"]["box"] == "rm1"


def test_a_stamped_rearm_does_not_overlap_with_itself():
    """coverage_end excludes the chunk's OWN content_hash group: re-arming a
    terminally failed stamped row must not report a 100% overlap with itself."""
    stamp = FlushStamp(offset=0, bytes=RAW, size=RAW * 4)
    qid = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI, flush=stamp).id
    with db.transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET status = 'failed', attempts = 3 WHERE id = %s", (qid,)
        )
    res = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI, flush=stamp)
    assert res.id == qid and res.requeued is True
    assert _meta(qid)["flush"]["offset"] == 0 and "overlap" not in _meta(qid)
    # Other stamped chunks of the same session still count as coverage.
    second = "user: a later chunk\nassistant: overlapping the first one's tail.\n"
    cov = ingest_queue.enqueue(
        "proj-a",
        second,
        source_uri=URI,
        flush=FlushStamp(offset=RAW // 2, bytes=len(second.encode()), size=RAW * 4),
    ).id
    assert _meta(cov)["overlap"] == {"covered_end": RAW, "bytes": RAW - RAW // 2}


@pytest.mark.parametrize(
    "bad",
    [
        {"event": "Pre Compact"},
        {"event": "x" * 17},
        {"box": "b" * 65},
        {"box": "rm1;drop"},
        {"project_by": "Record"},
        {"branch_of": "a/b"},
        {"mtime": -1},
        {"offset": -1},
    ],
)
def test_flush_stamp_shapes_are_closed_in_kbcore(bad):
    """kbcore is the authority on the VALUE shapes (the REST schema repeats them), so
    a non-REST constructor cannot store an arbitrary string under meta.flush."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FlushStamp(**{"offset": 0, "bytes": RAW, "size": RAW, **bad})

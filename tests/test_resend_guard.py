"""Wave S item S2 — the server-side resend guard (hybrid_v1 §1.10 cases 1-10, 13-17).

Real DB in kb_test only. A transcript chunk's (scrubbed session uri, content_hash)
is refused across projects, behind a mined twin, and across session uris from
the same submitter; every refusal is answered no-oracle and recorded ONLY in
the submitter's ledger row. Root cause KB 33783; design KB 33784.
"""

from __future__ import annotations

import logging

import pytest
from starlette.testclient import TestClient

from kbcore import db, ingest_queue, queue_guard, scope, stats
from kbcore.config import settings
from kbcore.errors import InvalidOperation

pytestmark = pytest.mark.usefixtures("kb")

TEST_TOKEN = "kbtest-" + "0" * 32
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}
URI = "session:0a1b2c3d-guard"
TEXT = "user: how do we bind the port?\nassistant: KB_BIND only, never 0.0.0.0.\n"
DUP_BODY = {"queue_id": None, "duplicate": True, "requeued": False, "permanently_failed": False}


@pytest.fixture(autouse=True)
def _guard_on(monkeypatch):
    """Pin the knobs to their CODE defaults (never the box's .env) and clear the ledger."""
    monkeypatch.setattr(settings, "kb_ingest_resend_guard", True)
    monkeypatch.setattr(settings, "kb_ingest_xsession_guard", True)
    _truncate_ledger()
    yield


def _truncate_ledger() -> None:
    """Schema-QUALIFIED like conftest's kb fixture (the 2026-07-21 live-schema lesson);
    ingest_resend_events is not yet in conftest._KB_TEST_TABLES (outside A's files)."""
    with db.transaction() as conn:
        assert conn.execute("SELECT current_schema()").fetchone()[0] == "kb_test"
        conn.execute("TRUNCATE kb_test.ingest_resend_events")


def _rows() -> int:
    with db.connect() as conn:
        return int(conn.execute("SELECT count(*) FROM ingest_queue").fetchone()[0])


def _row(qid: int) -> dict:
    with db.connect() as conn:
        r = conn.execute(
            "SELECT status, attempts, chunk_key, meta, project FROM ingest_queue WHERE id = %s",
            (qid,),
        ).fetchone()
    return {"status": r[0], "attempts": r[1], "chunk_key": r[2], "meta": r[3], "project": r[4]}


def _ledger() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT principal, uri, refused_rows, refused_bytes, last_kind, last_project, "
            "last_box, last_canonical FROM ingest_resend_events ORDER BY principal, uri"
        ).fetchall()
    keys = ("principal", "uri", "rows", "bytes", "kind", "project", "box", "canonical")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def _set(qid: int, sql: str) -> None:
    with db.transaction() as conn:
        conn.execute(f"UPDATE ingest_queue SET {sql} WHERE id = %s", (qid,))  # noqa: S608 — test const


def _grantee(name: str, *projects: str) -> scope.Principal:
    return scope.Principal(
        name=name, read_projects=frozenset(projects), write_projects=frozenset(projects)
    )


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbrest.server import build_app

    return TestClient(build_app())


# (1) + Inv 3 ------------------------------------------------------------------


def test_same_bytes_same_uri_under_two_projects_is_one_row_and_idempotent():
    a = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI).id
    assert a is not None
    res = ingest_queue.enqueue("proj-b", TEXT, source_uri=URI)
    assert res == ingest_queue.EnqueueResult(None, requeued=False, resend_of=a)
    assert _rows() == 1
    # The canonical carries its chunk_key from its own INSERT; the refusal wrote NOTHING on it.
    canon = _row(a)
    assert canon["chunk_key"] == queue_guard.chunk_key(URI, _hash(TEXT))
    assert "resend" not in canon["meta"]
    # Inv 3: resending again changes no row; the ledger row is UPSERTed, not duplicated.
    assert ingest_queue.enqueue("proj-b", TEXT, source_uri=URI).id is None
    assert _rows() == 1
    (led,) = _ledger()
    assert led["principal"] == "owner" and led["uri"] == URI and led["canonical"] == a
    assert led["rows"] == 2 and led["bytes"] == 2 * len(TEXT.encode())
    assert led["kind"] == queue_guard.KIND_HASH and led["project"] == "proj-b"


def _hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


# (2) no-oracle REST body -------------------------------------------------------


def test_rest_cross_project_duplicate_body_equals_same_project_duplicate_body(monkeypatch):
    client = _client(monkeypatch)
    body = {"project": "proj-a", "transcript": TEXT, "session_uri": URI}
    assert client.post("/api/ingest-session", headers=AUTH, json=body).status_code == 202
    same = client.post("/api/ingest-session", headers=AUTH, json=body)
    cross = client.post("/api/ingest-session", headers=AUTH, json={**body, "project": "proj-b"})
    assert same.status_code == cross.status_code == 202
    assert same.json() == cross.json() == DUP_BODY
    assert len(cross.json()) == 4  # never a cross_project flag, canonical id or project
    assert _rows() == 1


# (3) (4) terminally failed canonical --------------------------------------------


def test_same_project_resubmission_of_a_terminally_failed_row_still_rearms():
    a = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI).id
    _set(a, "status = 'failed', attempts = 3")
    res = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI)
    assert res.id == a and res.requeued is True
    assert _row(a)["status"] == "pending" and _ledger() == []


def test_cross_project_resend_of_a_terminally_failed_canonical_is_refused(caplog):
    a = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI).id
    _set(a, "status = 'failed', attempts = 3")
    with caplog.at_level(logging.WARNING, logger="kbcore.ingest_queue"):
        res = ingest_queue.enqueue("proj-b", TEXT, source_uri=URI)
    assert res.id is None and res.resend_of == a and res.requeued is False
    assert _row(a)["status"] == "failed" and _row(a)["attempts"] == 3  # no re-arm under B
    assert any("terminally failed canonical" in r.getMessage() for r in caplog.records)
    assert _ledger()[0]["rows"] == 1 and _ledger()[0]["canonical"] == a


# (5) scope: only session chunks ---------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "uri"),
    [
        ("session", "doc://proj-a/readme.md"),
        ("session", "/home/u/.claude/projects/x/abc.jsonl:0-100#p0"),
        ("memory", URI),
        ("session", "session:"),
        ("session", None),
    ],
)
def test_untouched_shapes_still_get_one_row_per_project(kind, uri):
    a = ingest_queue.enqueue("proj-a", TEXT, kind=kind, source_uri=uri).id
    b = ingest_queue.enqueue("proj-b", TEXT, kind=kind, source_uri=uri).id
    assert a is not None and b is not None and _rows() == 2
    assert _row(a)["chunk_key"] is None and _row(b)["chunk_key"] is None
    assert _ledger() == []


# (6) pre-020 canonical -----------------------------------------------------------


def test_a_pre020_canonical_with_null_chunk_key_still_refuses_and_is_not_stamped():
    a = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI).id
    _set(a, "chunk_key = NULL")
    assert ingest_queue.enqueue("proj-b", TEXT, source_uri=URI).resend_of == a
    # Amendment 1: a refusal never writes the canonical — historical keys are the repair's.
    assert _row(a)["chunk_key"] is None and _rows() == 1


# (7) (14) race on the chunk_key arbiter ----------------------------------------------


@pytest.mark.parametrize("uri", [URI, f"session:sid-ghp_{'0' * 36}"])
def test_losing_the_chunk_key_arbiter_refuses_and_leaves_the_transaction_usable(monkeypatch, uri):
    a = ingest_queue.enqueue("proj-a", TEXT, source_uri=uri).id
    monkeypatch.setattr(queue_guard, "find_prior", lambda *_a, **_k: None)
    res = ingest_queue.enqueue("proj-b", TEXT, source_uri=uri)
    assert res.id is None and res.resend_of == a and _rows() == 1
    (led,) = _ledger()  # written AFTER the failed INSERT in the same transaction
    assert led["kind"] == queue_guard.KIND_RACE and led["canonical"] == a
    # V2-m4: the arbiter fires only because chunk_key is built from the SCRUBBED uri.
    assert "ghp_0" not in led["uri"] and led["uri"] == _row(a)["meta"]["source_uri"]
    if uri != URI:
        assert "[REDACTED:github_token]" in led["uri"]
    assert ingest_queue.enqueue("proj-b", TEXT + "more\n", source_uri=uri).id is not None


def test_race_with_no_row_behind_the_key_is_a_uniform_400(monkeypatch):
    ingest_queue.enqueue("proj-a", TEXT, source_uri=URI)
    monkeypatch.setattr(queue_guard, "find_prior", lambda *_a, **_k: None)
    monkeypatch.setattr(queue_guard, "prior_by_chunk_key", lambda *_a, **_k: None)
    with pytest.raises(InvalidOperation):
        ingest_queue.enqueue("proj-b", TEXT, source_uri=URI)
    client = _client(monkeypatch)
    r = client.post(
        "/api/ingest-session",
        headers=AUTH,
        json={"project": "proj-b", "transcript": TEXT, "session_uri": URI},
    )
    assert r.status_code == 400
    assert r.json() == {"error": "submission could not be queued; retry"}
    assert _ledger() == []


# (8) kill switch -------------------------------------------------------------------


def test_kill_switch_restores_two_rows(monkeypatch):
    monkeypatch.setattr(settings, "kb_ingest_resend_guard", False)
    a = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI).id
    b = ingest_queue.enqueue("proj-b", TEXT, source_uri=URI).id
    assert a is not None and b is not None and _rows() == 2
    assert _row(a)["chunk_key"] is None and _row(b)["chunk_key"] is None
    assert _ledger() == []


# (9) the drain sees ONE group -----------------------------------------------------------


def test_a_project_hopping_resend_yields_one_group_and_one_window_set():
    from kbworker.prep import _assemble_jobs

    chunks = [TEXT, "user: and the token?\nassistant: hashed at rest, shown once.\n"]
    ids = [ingest_queue.enqueue("proj-a", c, source_uri=URI).id for c in chunks]
    for c in chunks:
        assert ingest_queue.enqueue("proj-b", c, source_uri=URI).id is None
    groups = ingest_queue.pending_groups(quiet_minutes=0)
    assert len(groups) == 1 and [r["id"] for r in groups[0]["rows"]] == ids
    jobs = _assemble_jobs(groups)
    assert jobs and {tuple(j["ids"]) for j in jobs} == {tuple(ids)}


# (10) 'duplicate' rows + migration 020 -----------------------------------------------------


def test_duplicate_status_is_accepted_and_invisible_everywhere():
    a = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI).id
    _set(a, "status = 'duplicate', duplicate_of = 1")  # the CHECK widened by 020 accepts it
    assert ingest_queue.queue_depth() == 0
    assert stats.stats()["ingest_queue_pending"] == 0
    assert ingest_queue.pending() == [] and ingest_queue.pending_groups(quiet_minutes=0) == []
    assert ingest_queue.requeue_failed() == []
    assert ingest_queue.requeue_extracted(project="proj-a") == []
    assert ingest_queue.near_deferral_cap_groups() == 0
    assert _row(a)["status"] == "duplicate"


def test_migration_020_applied_on_kb_test():
    from kbcore.migrate import migrate

    with db.connect() as conn:
        assert conn.execute("SELECT to_regclass('ingest_resend_events')").fetchone()[0]
        idx = {
            r[0]
            for r in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
                "AND tablename = 'ingest_queue'"
            ).fetchall()
        }
    assert {"ingest_queue_chunk_key_uidx", "ingest_queue_session_uri_idx"} <= idx
    assert migrate() == []


# (13) scrubbed uri -----------------------------------------------------------------------


def test_the_guard_probes_and_records_the_scrubbed_uri():
    fake = "ghp_" + "0" * 36
    leaky = f"session:sid-{fake}"
    a = ingest_queue.enqueue("proj-a", TEXT, source_uri=leaky).id
    assert ingest_queue.enqueue("proj-b", TEXT, source_uri=leaky).resend_of == a
    (led,) = _ledger()
    assert fake not in led["uri"] and "[REDACTED:github_token]" in led["uri"]
    assert _row(a)["meta"]["source_uri"] == led["uri"]


# (15) mined twin under B refuses A's re-arm ------------------------------------------------


def test_an_extracted_twin_under_another_project_refuses_the_rearm(monkeypatch):
    monkeypatch.setattr(settings, "kb_ingest_resend_guard", False)
    a = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI).id
    b = ingest_queue.enqueue("proj-b", TEXT, source_uri=URI).id
    monkeypatch.setattr(settings, "kb_ingest_resend_guard", True)
    ingest_queue.mark_extracted(b, saved=3, rejected=0)
    _set(a, "status = 'failed', attempts = 3")
    res = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI)
    assert res.id is None and res.resend_of == b
    assert _row(a)["status"] == "failed" and _row(a)["attempts"] == 3
    assert _ledger()[0]["kind"] == queue_guard.KIND_EXTRACTED


def test_a_same_project_resubmission_of_a_mined_chunk_is_refused_and_ledgered():
    """Appendix F 2b is literally 'any project': a same-project resend of an already
    EXTRACTED chunk is refused (it was a silent live duplicate before) and ledgered
    under kind 'extracted'. Pinned so a change of mind is a deliberate edit."""
    a = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI).id
    ingest_queue.mark_extracted(a, saved=1, rejected=0)
    res = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI)
    assert res == ingest_queue.EnqueueResult(None, requeued=False, resend_of=a)
    assert _rows() == 1 and _row(a)["status"] == "extracted"
    (led,) = _ledger()
    assert led["kind"] == queue_guard.KIND_EXTRACTED and led["project"] == "proj-a"


# (16) hash-xsession ------------------------------------------------------------------------


def test_same_bytes_under_a_new_session_uri_from_the_same_principal_is_refused(monkeypatch):
    a = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI).id
    res = ingest_queue.enqueue("proj-a", TEXT, source_uri="session:branch-copy")
    assert res.id is None and res.resend_of == a
    assert _ledger()[0]["kind"] == queue_guard.KIND_XSESSION
    assert _ledger()[0]["uri"] == "session:branch-copy"  # the ledger keys the RESENT uri
    # A different principal is a different box: its own row.
    with scope.use_principal(_grantee("box-b", "proj-c")):
        assert ingest_queue.enqueue("proj-c", TEXT, source_uri="session:other-box").id
    # Gate off: the same principal's copy is stored too (a fresh project, so today's
    # per-project UNIQUE is not what answers — that stays a live duplicate).
    monkeypatch.setattr(settings, "kb_ingest_xsession_guard", False)
    assert ingest_queue.enqueue("proj-d", TEXT, source_uri="session:branch-two").id
    assert _rows() == 3


# (17) the ledger carries the SUBMITTER ---------------------------------------------------------


def test_the_ledger_row_is_written_as_the_submitter_never_on_the_canonical():
    from kbcore.models import FlushStamp

    a = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI).id
    before = _row(a)
    stamp = FlushStamp(offset=0, bytes=len(TEXT.encode()), size=4096, box="rm1")
    with scope.use_principal(_grantee("box-b", "proj-b")):
        res = ingest_queue.enqueue("proj-b", TEXT, source_uri=URI, flush=stamp)
    assert res.id is None and res.resend_of == a
    (led,) = _ledger()
    assert led["principal"] == "box-b" and led["box"] == "rm1" and led["project"] == "proj-b"
    assert _row(a) == before  # status, attempts, chunk_key, meta, project: untouched


def test_the_ledger_scrubs_a_token_shaped_box_label():
    """Rule 12: ``last_box`` is printed by the digest, and ``ghp_`` + 36 alnum fits the
    REST ``flush_box`` pattern — the ledger writer scrubs it like meta.flush.box."""
    from kbcore.models import FlushStamp

    fake = "ghp_" + "0" * 36
    a = ingest_queue.enqueue("proj-a", TEXT, source_uri=URI).id
    stamp = FlushStamp(offset=0, bytes=len(TEXT.encode()), size=4096, box=fake)
    with scope.use_principal(_grantee("box-b", "proj-b")):
        assert ingest_queue.enqueue("proj-b", TEXT, source_uri=URI, flush=stamp).resend_of == a
    (led,) = _ledger()
    assert fake not in led["box"] and "[REDACTED" in led["box"]
    with db.connect() as conn:
        dump = str(conn.execute("SELECT row_to_json(e) FROM ingest_resend_events e").fetchall())
    assert fake not in dump

"""Shared fixtures: real compose Postgres in an isolated kb_test schema
(created/dropped per session), Ollama mocked with deterministic embeddings
(CLAUDE.md: real-DB tests, mock only Ollama).
"""

from __future__ import annotations

import hashlib
import math
import os
import random

import pytest

from kbcore import db, embed
from kbcore.config import settings
from kbcore.migrate import migrate

# Arm the connection-layer live-data guard for the WHOLE test process, at import
# time before any fixture or DB check runs: db.connect() now REFUSES any 'public'
# connection unless allow_public=True. This is the hard backstop that makes it
# impossible for a stray/misordered test to TRUNCATE live data (it truncated the
# live corpus twice via the schema-pointer window). Only the kb_test setup DDL
# below is allowed to touch public, and it only creates/drops the kb_test schema.
os.environ["KB_TEST_GUARD"] = "1"


def _db_available() -> bool:
    # The import-time reachability probe legitimately hits public (SELECT 1), so
    # it opts in explicitly; it never writes.
    try:
        conn = db.connect(schema="public", allow_public=True)
        try:
            return conn.execute("SELECT 1").fetchone()[0] == 1
        finally:
            conn.close()
    except Exception:
        return False


DB_UP = _db_available()


def fake_vector(text: str) -> list[float]:
    """Deterministic pseudo-embedding: seeded by content hash, unit-normalized.

    Identical text ⇒ identical vector; different text ⇒ (almost surely)
    near-orthogonal vectors. Keyword search carries relevance in tests; the
    vector leg just has to be deterministic and well-formed.
    """
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
    rng = random.Random(seed)
    v = [rng.gauss(0, 1) for _ in range(settings.kb_embed_dim)]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch):
    """Mock Ollama for every test: embed_texts returns deterministic vectors."""
    monkeypatch.setattr(embed, "embed_texts", lambda texts: [fake_vector(t) for t in texts])


@pytest.fixture(autouse=True)
def _neutral_feature_flags(monkeypatch):
    """Pin production feature flags to their code DEFAULTS for every test.

    settings reads .env, so a live deployment flag (ensemble live-mode, rerank
    sidecar URL, embedder query prefix) would silently reroute the code paths
    under test — the ensemble .env flip broke 13 v1-path tests exactly this
    way. Tests that exercise a flag set it explicitly via monkeypatch.
    """
    monkeypatch.setattr(settings, "kb_extract_ensemble", "")
    monkeypatch.setattr(settings, "kb_ensemble_shadow", True)
    monkeypatch.setattr(settings, "kb_rerank_url", "")
    monkeypatch.setattr(settings, "kb_embed_query_prefix", "")
    monkeypatch.setattr(settings, "kb_ensemble_secondary_judge", "")


@pytest.fixture(autouse=True)
def _reset_scope():
    """Reset the per-request principal scope between tests (per-project sharing).

    A test (or an in-process ``build_app()``) may flip server-mode or leave a
    principal cached; without this, a later kbcore-direct test could fail-closed or
    see a stale grantee. Guarantees every test starts as the implicit OWNER.
    """
    from kbcore import principals, scope

    scope._reset_server_mode()
    principals._clear_cache()
    yield
    scope._reset_server_mode()
    principals._clear_cache()


@pytest.fixture(scope="session", autouse=True)
def _force_test_schema():
    """Belt-and-suspenders isolation (autouse): point ``settings.kb_db_schema`` at
    ``kb_test`` for the ENTIRE session, independent of any fixture request.

    The kb/kb_schema fixtures below still create + migrate kb_test on demand, but
    that flip only happened when a test asked for them. A DB-touching test that
    forgot the fixture ran SQL against the live ``public`` schema — the exact
    failure mode that once truncated live data (a sub-agent-written test). With
    this autouse guard the pointer is NEVER ``public`` during tests: a stray
    write hits a possibly-missing ``kb_test`` (loud error), never live data.
    """
    original = settings.kb_db_schema
    settings.kb_db_schema = "kb_test"
    try:
        yield
    finally:
        settings.kb_db_schema = original


def _admin_ddl(sql: str) -> None:
    """Run one autocommit DDL statement against public — the ONLY sanctioned
    public access in the test process (creates/drops the kb_test schema; never
    touches public DATA). Uses the schema param so the global pointer is never
    flipped to 'public' (that flip was the truncation window)."""
    conn = db.connect(schema="public", allow_public=True)
    conn.autocommit = True
    try:
        conn.execute(sql)
    finally:
        conn.close()


@pytest.fixture(scope="session")
def kb_schema():
    """Session-scoped isolated schema kb_test with the full migrated layout."""
    if not DB_UP:
        pytest.skip("compose Postgres not reachable")
    # kb_test must exist before any kb_test-targeted connect (db.connect refuses a
    # missing non-public schema). Created via the sanctioned admin DDL path — the
    # process-wide schema pointer stays 'kb_test' (set by _force_test_schema)
    # throughout, so no window ever resolves a test connection to public.
    _admin_ddl("DROP SCHEMA IF EXISTS kb_test CASCADE")
    _admin_ddl("CREATE SCHEMA kb_test")
    try:
        migrate()  # targets settings.kb_db_schema == 'kb_test'
        yield
    finally:
        _admin_ddl("DROP SCHEMA IF EXISTS kb_test CASCADE")


@pytest.fixture()
def kb(kb_schema):
    """Function-scoped clean slate inside kb_test (truncate, keep layout)."""
    with db.transaction() as conn:
        # Belt-and-suspenders: NEVER TRUNCATE unless we are provably inside
        # kb_test. current_schema() is the first existing schema in search_path,
        # so this aborts loudly rather than wiping live public data.
        current = conn.execute("SELECT current_schema()").fetchone()[0]
        if current != "kb_test":
            raise RuntimeError(
                f"live-data guard: refusing TRUNCATE, current_schema()={current!r} is not 'kb_test'"
            )
        conn.execute(
            """
            TRUNCATE access_log, review_queue, relations, entities, chunks, items,
                     sources, projects, session_checkpoints, ingest_queue,
                     principal_grants, principals
                     RESTART IDENTITY CASCADE
            """
        )
    yield

"""Shared fixtures: real compose Postgres in an isolated kb_test schema
(created/dropped per session), Ollama mocked with deterministic embeddings
(CLAUDE.md: real-DB tests, mock only Ollama).
"""

from __future__ import annotations

import hashlib
import math
import random

import pytest

from kbcore import db, embed
from kbcore.config import settings
from kbcore.migrate import migrate


def _db_available() -> bool:
    try:
        return db.ping()
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


@pytest.fixture(scope="session")
def kb_schema():
    """Session-scoped isolated schema kb_test with the full migrated layout."""
    if not DB_UP:
        pytest.skip("compose Postgres not reachable")
    # Create the schema through a PUBLIC-targeted connection: db.connect() now
    # refuses a MISSING non-public schema (it would otherwise fall through to live
    # public data), so kb_test must exist before any kb_test-targeted connect.
    settings.kb_db_schema = "public"
    conn = db.connect()
    conn.autocommit = True
    conn.execute("DROP SCHEMA IF EXISTS kb_test CASCADE")
    conn.execute("CREATE SCHEMA kb_test")
    conn.close()
    settings.kb_db_schema = "kb_test"
    try:
        migrate()
        yield
    finally:
        settings.kb_db_schema = "public"
        conn = db.connect()
        conn.autocommit = True
        conn.execute("DROP SCHEMA IF EXISTS kb_test CASCADE")
        conn.close()


@pytest.fixture()
def kb(kb_schema):
    """Function-scoped clean slate inside kb_test (truncate, keep layout)."""
    with db.transaction() as conn:
        conn.execute(
            """
            TRUNCATE access_log, review_queue, relations, entities, chunks, items,
                     sources, projects, session_checkpoints, ingest_queue,
                     principal_grants, principals
                     RESTART IDENTITY CASCADE
            """
        )
    yield

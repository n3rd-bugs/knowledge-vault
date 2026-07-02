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


@pytest.fixture(scope="session")
def kb_schema():
    """Session-scoped isolated schema kb_test with the full migrated layout."""
    if not DB_UP:
        pytest.skip("compose Postgres not reachable")
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
                     sources, projects, session_checkpoints RESTART IDENTITY CASCADE
            """
        )
    yield

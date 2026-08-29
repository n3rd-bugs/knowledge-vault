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
def _ranking_dials_at_code_defaults(monkeypatch):
    """Tests must not inherit the box's .env ranking dials (same hermeticity
    gotcha as _no_real_notifications). Validation week 2026-08-14 flipped
    KB_ID_LEG_RARE_TOKENS / KB_TRUST_FACTOR / KB_COLLAPSE_ALTERNATIVES /
    KB_TAIL_TRIAGE_SHARE live on this box, and three dial tests' "off" phases
    promptly failed by inheriting them. Every test sees the CODE defaults; a
    test that wants a dial on sets it deliberately (as the wave-3 suite does).
    """
    from kbcore.config import settings as _s

    monkeypatch.setattr(_s, "kb_id_leg_rare_tokens", False)
    monkeypatch.setattr(_s, "kb_trust_factor", 1.0)
    monkeypatch.setattr(_s, "kb_collapse_alternatives", False)
    monkeypatch.setattr(_s, "kb_tail_triage_share", 0.0)
    monkeypatch.setattr(_s, "kb_miss_relevance_floor", 0.0)


@pytest.fixture(autouse=True)
def _no_real_notifications(monkeypatch):
    """Tests must NEVER reach the owner's real ntfy channel.

    consolidate.main() ends with a digest push, and settings load the REAL .env
    (the known test-hermeticity gotcha) — so every integration test driving
    main() published an all-zero 'KB nightly' digest to the owner's phone. A
    parallel agent wave re-running those suites produced 101 pushes in 6 hours
    at one per ~6s (2026-08-03). Blank the channel config for every test; the
    push helper then no-ops. Tests that WANT to exercise push_digest set their
    own fake URL explicitly.
    """
    from kbcore.config import settings as _s

    monkeypatch.setattr(_s, "kb_notify_url", "")
    monkeypatch.setattr(_s, "kb_notify_topic", "")
    monkeypatch.setattr(_s, "kb_notify_token", "")


@pytest.fixture(autouse=True)
def _sentinel_floors_at_code_defaults(monkeypatch):
    """Tests must not inherit the box's .env eval floors (same hermeticity gotcha
    as _ranking_dials_at_code_defaults). Review #4's P0 fix anchored
    KB_EVAL_MIN_HIT5=0.97 / KB_EVAL_MIN_MRR=0.85 in the live .env (2026-08-16),
    and the eval_sentinel suite's epsilon/floor fixtures promptly failed by
    inheriting them. Every test sees the CODE defaults; a test probing the floor
    sets it deliberately."""
    from kbcore.config import settings as _s

    monkeypatch.setattr(_s, "kb_eval_min_hit5", 0.75)
    monkeypatch.setattr(_s, "kb_eval_min_mrr", 0.5)


@pytest.fixture(autouse=True)
def _no_weekly_report(monkeypatch):
    """The weekly trend report (kbworker.weekly) is day-gated on the REAL calendar
    (kb_weekly_day), so on its configured day every test driving push_run_digest
    would suddenly read the DB and mint a kb-system report item — the same
    hermeticity class as _no_real_notifications. OFF for every test; the weekly
    suite flips the knob (and pins `now`) explicitly.
    """
    from kbcore.config import settings as _s

    monkeypatch.setattr(_s, "kb_weekly_report", False)


@pytest.fixture(autouse=True)
def _no_ambient_search_miss_count(monkeypatch):
    """push_run_digest counts the day's zero-hit searches (kbcore.search_misses).
    search_misses is TELEMETRY and outlives the kb fixture's per-test truncate,
    so rows minted by one test's searches would leak a 'search misses: N' line
    into any later digest assertion. Pin the ambient count to 0; tests that
    exercise the line re-patch or stash the counts key themselves.
    """
    from kbcore import search_misses

    monkeypatch.setattr(search_misses, "count_since", lambda *a, **kw: 0)


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch):
    """Mock Ollama for every test: embed_texts returns deterministic vectors."""
    # **kw so the stub keeps matching the real signature: embed_texts gained a
    # timeout kwarg (the read path uses a shorter budget than the drain), and a
    # stub that silently rejects new kwargs turns a signature change into a
    # confusing TypeError in unrelated tests.
    monkeypatch.setattr(embed, "embed_texts", lambda texts, **kw: [fake_vector(t) for t in texts])


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


_KB_TEST_TABLES = (
    "access_log",
    "review_queue",
    "relations",
    "entities",
    "chunks",
    "items",
    "sources",
    "projects",
    "session_checkpoints",
    "ingest_queue",
    "principal_grants",
    "principals",
    "enrolment_codes",
    "search_misses",
)


@pytest.fixture(autouse=True)
def _isolated_raw_store(tmp_path_factory, monkeypatch):
    """Point KB_RAW at a per-session temp dir instead of the real ~/kb/raw.

    Two reasons, both learned the hard way:

    * The archive is CONTENT-ADDRESSED and archive_raw never overwrites an
      existing blob. So a blob written by a buggy run is reused by every later
      run, and a bug outlives the code that caused it — a NUL-byte document was
      "failing" long after the fix, because the test kept reading the mangled
      blob from before it.
    * Tests were writing into the production raw store, which is tarred into
      every off-site backup. Test fixtures do not belong in the owner's backups,
      and this is the same class of mistake as the live-schema truncations this
      repo has already suffered.
    """
    from kbcore.config import settings

    monkeypatch.setattr(settings, "kb_raw", tmp_path_factory.mktemp("kb_raw"))
    yield


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Point KB_STATE_DIR at a per-test temp dir instead of the real ~/kb/state.

    consolidate.main()'s tail now arms/disarms the AC-restore trigger
    (kbworker.powerguard.record_battery_pause), so any test driving main() with
    a battery stop would otherwise write a real battery-pause.json that the live
    kb-ac-restore.timer acts on — a test could literally restart the owner's
    nightly. Same hermeticity class as _isolated_raw_store.
    """
    monkeypatch.setattr(settings, "kb_state_dir", tmp_path / "kb_state")
    yield


@pytest.fixture()
def kb(kb_schema):
    """Function-scoped clean slate inside kb_test (truncate, keep layout).

    The TRUNCATE is FULLY SCHEMA-QUALIFIED (``kb_test.<table>``), the definitive
    close of the third-incident truncation (2026-07-21): concurrent pytest
    processes share one ``kb_test`` schema, and one process's session-teardown
    ``DROP SCHEMA kb_test`` could fire between another's ``current_schema()``
    check (passed — the schema still existed) and its TRUNCATE. With UNQUALIFIED
    names the now-missing kb_test made ``TRUNCATE items`` resolve through the
    ``search_path`` fallback to ``public.items`` and wipe live data. Qualifying
    every table means a vanished kb_test makes ``TRUNCATE kb_test.items`` ERROR
    ("relation does not exist") — loud, never a public write. The
    current_schema() + pointer asserts stay as belt-and-suspenders.
    """
    schema = settings.kb_db_schema
    if schema != "kb_test":  # the pointer must NEVER be public/anything-else here
        raise RuntimeError(f"live-data guard: settings.kb_db_schema={schema!r} is not 'kb_test'")
    with db.transaction() as conn:
        current = conn.execute("SELECT current_schema()").fetchone()[0]
        if current != "kb_test":
            raise RuntimeError(
                f"live-data guard: refusing TRUNCATE, current_schema()={current!r} is not 'kb_test'"
            )
        qualified = ", ".join(f"{schema}.{t}" for t in _KB_TEST_TABLES)
        conn.execute(f"TRUNCATE {qualified} RESTART IDENTITY CASCADE")  # noqa: S608 — schema const
    yield

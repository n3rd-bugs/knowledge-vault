"""Zero-hit search telemetry (review #4 Wave E, KB 21332; migration 018).

The KB records the searches it FAILED to answer — demand-side observability —
without ever letting telemetry break a search, pollute itself with eval
replays, or store unbounded query text. Real-DB tests in kb_test; Ollama is
the deterministic conftest stub (CLAUDE.md).
"""

from __future__ import annotations

import pytest

from kbcore import search_misses
from kbcore.db import connect, transaction
from kbcore.ingest import ingest
from kbcore.search import search
from tests.conftest import DB_UP

pytestmark = pytest.mark.skipif(not DB_UP, reason="compose Postgres not reachable")

# Captured at import time, BEFORE conftest's autouse hermeticity stub pins the
# ambient count to 0 (that stub protects OTHER suites' digest assertions).
_REAL_COUNT_SINCE = search_misses.count_since


def _rows() -> list[tuple[str, str | None, str]]:
    with connect() as conn:
        return conn.execute(
            "SELECT query, project_scope, tool FROM search_misses ORDER BY id"
        ).fetchall()


def test_zero_hit_search_records_the_miss(kb):
    hits = search("zorbltx-quux-never-stored", project="ghost-project", tool="mcp.kb_search")
    assert hits == []
    assert _rows() == [("zorbltx-quux-never-stored", "ghost-project", "mcp.kb_search")]


def test_eval_replays_never_record(kb):
    """tool=None is the eval-harness marker (same exemption as the access log)."""
    assert search("zorbltx-quux-never-stored", tool=None) == []
    assert _rows() == []


def test_a_search_with_hits_records_nothing(kb):
    ingest("p1", "note", "spi timing gotcha", "the spi bus needs a pulldown", uri="t:1")
    assert search("spi timing gotcha", tool="mcp.kb_search")
    assert _rows() == []


def test_query_text_is_bounded(kb):
    long_q = "zzq " + "x" * (5 * search_misses.MAX_QUERY_CHARS)
    assert search(long_q, tool="mcp.kb_search") == []
    (stored,) = [r[0] for r in _rows()]
    assert len(stored) == search_misses.MAX_QUERY_CHARS


def test_record_refuses_blank_query_and_none_tool(kb):
    assert search_misses.record("   ", None, "mcp.kb_search") is False
    assert search_misses.record("real query", None, None) is False
    assert _rows() == []


def test_record_is_best_effort_and_never_raises(kb, monkeypatch):
    """A broken telemetry write must cost the row, never the search (KB 21332)."""

    def boom(*a, **kw):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(search_misses, "transaction", boom)
    assert search_misses.record("q", None, "mcp.kb_search") is False
    # End to end: the zero-hit search still returns cleanly.
    assert search("zorbltx-never-stored", tool="mcp.kb_search") == []


def test_stats_and_top_misses_group_by_query(kb):
    for _ in range(3):
        search_misses.record("esp32 ledc fade", None, "mcp.kb_search")
    search_misses.record("systemd oneshot timer", "infra", "rest.search")
    stats = search_misses.stats(days=7)
    assert stats == {"total": 4, "distinct": 2}
    top = search_misses.top_misses(days=7, limit=10)
    assert top[0] == ("esp32 ledc fade", 3)
    assert top[1] == ("systemd oneshot timer", 1)
    # The digest's day-window counter sees them too (the real one — conftest's
    # hermeticity stub pins the module attribute to 0 for every other suite).
    assert _REAL_COUNT_SINCE(days=1) == 4


def test_sweep_old_prunes_only_aged_rows(kb):
    """The documented telemetry carve-out: >RETENTION_DAYS rows MAY be pruned;
    fresh rows and a non-positive horizon are untouched."""
    search_misses.record("fresh miss", None, "mcp.kb_search")
    search_misses.record("stale miss", None, "mcp.kb_search")
    with transaction() as conn:
        conn.execute(
            "UPDATE search_misses SET at = now() - make_interval(days => 120) "
            "WHERE query = 'stale miss'"
        )
    assert search_misses.sweep_old(0) == 0, "a zero horizon must never mean 'sweep everything'"
    assert search_misses.sweep_old() == 1
    assert [r[0] for r in _rows()] == ["fresh miss"]

"""Search-miss telemetry (Wave E KB 21332, migration 018; review #5 Wave F,
migration 019).

The KB records the searches it FAILED to answer — demand-side observability —
without ever letting telemetry break a search, pollute itself with eval
replays, store unbounded query text, or (review #5 sec-1/sec-2) hold a raw
secret or a multi-line report-forging query in either durable sink. The thin
instrument (subtype 'thin'/'calibration') is dial-gated and ships OFF. Real-DB
tests in kb_test; Ollama is the deterministic conftest stub (CLAUDE.md).
"""

from __future__ import annotations

import pytest

from kbcore import rerank, search_misses
from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.ingest import ingest
from kbcore.search import search
from tests.conftest import DB_UP

pytestmark = pytest.mark.skipif(not DB_UP, reason="compose Postgres not reachable")

# Captured at import time, BEFORE conftest's autouse hermeticity stub pins the
# ambient count to 0 (that stub protects OTHER suites' digest assertions).
_REAL_COUNT_SINCE = search_misses.count_since

GHP = "ghp_" + "0" * 36  # fake token shaped like a real one (CLAUDE.md hygiene)


@pytest.fixture(autouse=True)
def _hermetic_thin_dial(monkeypatch):
    """Pin the thin-miss dial to its code default and clear the thread-local
    rerank participation marker — it outlives tests (same hermeticity class as
    conftest's _ranking_dials_at_code_defaults; a prior suite's rerank stub
    could otherwise leak 'active' in and mint calibration rows here). The
    floor-ON calibration sampler counter is process-global for the same reason:
    reset it so every test's tick phase starts at zero (Wave G — the sampler is
    deterministic, so a pinned start makes every assertion exact)."""
    monkeypatch.setattr(settings, "kb_miss_relevance_floor", 0.0)
    monkeypatch.setattr(search_misses, "_calibration_seen", 0)
    rerank.reset_outcome()
    yield
    rerank.reset_outcome()


def _rows() -> list[tuple[str, str | None, str]]:
    with connect() as conn:
        return conn.execute(
            "SELECT query, project_scope, tool FROM search_misses ORDER BY id"
        ).fetchall()


def _full_rows() -> list[tuple[str, str, float | None, str | None]]:
    with connect() as conn:
        return conn.execute(
            "SELECT query, subtype, top_score, top_legs FROM search_misses ORDER BY id"
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
    assert stats == {"total": 4, "distinct": 2, "empty": 4, "thin": 0, "calibration": 0}
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


# --- review #5 sec-1/sec-2: scrub + flatten at the durable sinks --------------


def test_secret_query_scrubbed_at_both_sinks(kb):
    """Neither durable sink may hold a raw credential: search scrubs ONCE and
    feeds the same clean text to search_misses AND access_log (sec-1 — the
    access_log column retains indefinitely and was the primary live exposure)."""
    assert search(f"rotate {GHP} zorbltx-neverfound", tool="mcp.kb_search") == []
    assert _rows() == [("rotate [REDACTED:github_token] zorbltx-neverfound", None, "mcp.kb_search")]
    ingest("p1", "note", "spi timing gotcha", "the spi bus needs a pulldown", uri="t:1")
    assert search(f"spi timing gotcha {GHP}", tool="mcp.kb_search")
    with connect() as conn:
        logged = {r[0] for r in conn.execute("SELECT query FROM access_log").fetchall()}
    assert logged == {"spi timing gotcha [REDACTED:github_token]"}


def test_multiline_query_stores_single_line(kb):
    """sec-2 (this module's half): internal whitespace collapses to one line, so
    a crafted query cannot forge extra lines in the weekly report's miss table."""
    assert search("line one\nsecond line\t\tzorbltx-neverfound", tool="mcp.kb_search") == []
    assert _rows()[0][0] == "line one second line zorbltx-neverfound"


def test_record_scrubs_before_truncation(kb):
    """Defensive scrub at the sink runs BEFORE the length bound — truncation can
    never split a secret past its pattern (sec-1)."""
    q = "x" * (search_misses.MAX_QUERY_CHARS - 60) + f" {GHP} tail"
    assert search_misses.record(q, None, "t") is True
    stored = _rows()[0][0]
    assert "[REDACTED:github_token]" in stored
    assert "ghp_0" not in stored


# --- review #5 val-1: the relevance-based thin-miss instrument ----------------


def test_thin_miss_sidecar_absent_is_dial_gated(kb, monkeypatch):
    """Sidecar-absent path: a vector-only top hit records 'thin' ONLY while the
    floor dial is ON — the code default (0 = OFF) records nothing."""
    ingest("p1", "note", "croissant lamination", "butter fold schedule for croissants", uri="t:2")
    assert search("zorbltx flux capacitor", tool="mcp.kb_search")  # vector-only hits
    assert _rows() == [], "dial OFF: results-with-no-lexical-support record nothing"
    monkeypatch.setattr(settings, "kb_miss_relevance_floor", 0.05)
    assert search("zorbltx flux capacitor", tool="mcp.kb_search")
    assert _full_rows() == [("zorbltx flux capacitor", "thin", None, "vector")]


def test_lexically_supported_top_is_never_thin(kb, monkeypatch):
    """A keyword-leg top hit is not a miss on the sidecar-absent path, dial ON."""
    monkeypatch.setattr(settings, "kb_miss_relevance_floor", 0.05)
    ingest("p1", "note", "spi timing gotcha", "the spi bus needs a pulldown", uri="t:1")
    assert search("spi timing gotcha", tool="mcp.kb_search")
    assert _rows() == []


def test_thin_miss_sidecar_relevance_floor(kb, monkeypatch):
    """Sidecar-active path: top-1 relevance below the floor records 'thin' with
    the score; above the floor records nothing (dial ON)."""
    monkeypatch.setattr(settings, "kb_miss_relevance_floor", 0.05)
    legs = ([(7, "x")], [], [])
    rerank._note("active", 3e-05)
    assert search_misses.observe("weird rtos tick", None, "t", top_id=7, legs=legs)
    rerank._note("active", 0.99994)
    assert not search_misses.observe("weird rtos tick", None, "t", top_id=7, legs=legs)
    assert _full_rows() == [("weird rtos tick", "thin", 3e-05, "vector")]


def test_calibration_rows_when_floor_off_and_excluded_from_counts(kb):
    """Dial OFF + sidecar scored: the would-be score lands as 'calibration' —
    durable for the calibration week, invisible to every default miss count."""
    rerank._note("active", 0.42)
    assert search_misses.observe("cal query", None, "t", top_id=1, legs=([], [(1, "y")], []))
    search_misses.record("real empty miss", None, "t")
    assert _full_rows()[0] == ("cal query", "calibration", 0.42, "keyword")
    assert _REAL_COUNT_SINCE(days=1) == 1, "count_since keeps its Wave-E 'empty' meaning"
    assert _REAL_COUNT_SINCE(days=1, subtype="thin") == 0
    stats = search_misses.stats(days=1)
    assert stats == {"total": 1, "distinct": 1, "empty": 1, "thin": 0, "calibration": 1}
    assert search_misses.top_misses(days=1) == [("real empty miss", 1)]
    assert search_misses.top_misses(days=1, subtype="calibration") == [("cal query", 1)]


def test_search_with_sidecar_records_calibration_row(kb, monkeypatch):
    """End to end through search(): dial OFF + live sidecar logs the top-1
    relevance and the top hit's leg provenance (review #5 calibration aid)."""
    # The second item shares NO lexeme with the query, so the keyword leg (AND
    # or OR-fallback) carries only the first — no RRF tie, a deterministic top.
    ingest("p1", "note", "spi timing gotcha", "the spi bus needs a pulldown", uri="t:1")
    ingest("p1", "note", "i2c pullup note", "the i2c bus needs pullups", uri="t:3")
    monkeypatch.setattr(settings, "kb_rerank_url", "http://127.0.0.1:9")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "results": [
                    {"index": 0, "relevance_score": 0.61},
                    {"index": 1, "relevance_score": 0.02},
                ]
            }

    monkeypatch.setattr(rerank.httpx, "post", lambda *a, **kw: _Resp())
    hits = search("spi timing gotcha", tool="mcp.kb_search")
    assert hits and hits[0].title == "spi timing gotcha"
    assert _full_rows() == [("spi timing gotcha", "calibration", 0.61, "vector+keyword")]


# --- Wave G (review #6): the deterministic floor-ON calibration sampler -------


def test_floor_on_calibration_sampler_records_every_nth(kb, monkeypatch):
    """Floor ON + sidecar active: above-floor searches record nothing EXCEPT a
    deterministic 1-in-CALIBRATION_SAMPLE_EVERY 'calibration' row carrying the
    score — the histogram stays alive without another floor-off week."""
    monkeypatch.setattr(settings, "kb_miss_relevance_floor", 0.05)
    legs = ([(7, "x")], [], [])
    n = search_misses.CALIBRATION_SAMPLE_EVERY
    for i in range(1, n):
        rerank._note("active", 0.5)
        assert not search_misses.observe(f"warm q {i}", None, "t", top_id=7, legs=legs)
    assert _full_rows() == [], "ticks 1..N-1 record nothing above the floor"
    rerank._note("active", 0.5)
    assert search_misses.observe("sampled q", None, "t", top_id=7, legs=legs)
    assert _full_rows() == [("sampled q", "calibration", 0.5, "vector")]
    # Excluded from every miss surface, exactly like floor-off calibration rows.
    assert _REAL_COUNT_SINCE(days=1) == 0
    assert _REAL_COUNT_SINCE(days=1, subtype="thin") == 0
    assert search_misses.stats(days=1)["total"] == 0
    assert search_misses.top_misses(days=1) == []


def test_floor_on_sampler_thin_wins_a_due_tick(kb, monkeypatch):
    """A due tick that is ALSO a thin miss records only the thin row (which
    already carries the score) — never a double write."""
    monkeypatch.setattr(settings, "kb_miss_relevance_floor", 0.05)
    monkeypatch.setattr(
        search_misses, "_calibration_seen", search_misses.CALIBRATION_SAMPLE_EVERY - 1
    )
    rerank._note("active", 3e-05)
    assert search_misses.observe("thin on the tick", None, "t", top_id=7, legs=([(7, "x")], [], []))
    assert _full_rows() == [("thin on the tick", "thin", 3e-05, "vector")]


def test_sampler_ticks_only_for_sidecar_scored_floor_on_searches(kb, monkeypatch):
    """Neither floor-OFF calibration writes nor sidecar-absent thins advance the
    sampler — its denominator is exactly 'sidecar-scored under floor ON'."""
    legs = ([(7, "x")], [], [])
    rerank._note("active", 0.9)
    assert search_misses.observe("floor off cal", None, "t", top_id=7, legs=legs)  # floor OFF
    monkeypatch.setattr(settings, "kb_miss_relevance_floor", 0.05)
    rerank.reset_outcome()  # sidecar absent ('off') + floor ON -> heuristic thin path
    assert search_misses.observe("no sidecar", None, "t", top_id=7, legs=legs)
    assert search_misses._calibration_seen == 0, "neither path may tick the sampler"

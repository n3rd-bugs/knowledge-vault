"""Weekly trend report (review #4 strategy P1, KB 21332; kbworker.weekly).

Day-gated INSIDE the step (no new timer), knob-killable, double-fire-proof,
and wired as a guarded rider on the nightly digest. Real-DB tests in kb_test;
Ollama is the deterministic conftest stub (CLAUDE.md).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from kbcore import review, search_misses
from kbcore.config import settings
from kbcore.db import connect
from kbcore.ingest import ingest
from kbworker import digest as digest_mod
from kbworker import weekly
from tests.conftest import DB_UP

pytestmark = pytest.mark.skipif(not DB_UP, reason="compose Postgres not reachable")

A_SUNDAY = datetime(2026, 8, 16, 1, 30)  # local; weekday() == 6 == _DAYS['sun']
A_MONDAY = datetime(2026, 8, 17, 1, 30)


@pytest.fixture()
def weekly_on(monkeypatch):
    """Arm the knob the conftest hermeticity fixture blanks; pin the day to sun."""
    monkeypatch.setattr(settings, "kb_weekly_report", True)
    monkeypatch.setattr(settings, "kb_weekly_day", "sun")


def _nightly_report(day: str, **meta_extra):
    return ingest(
        "kb-system",
        "report",
        f"Consolidation report {day}",
        f"Nightly consolidation report\n\n- day: {day}",
        kind="chat",
        uri=f"report:{day}T02:00:00+00:00",
        trust="verified",
        meta={"consolidation": True, **meta_extra},
    )


# ----------------------------------------------------------------- gating


def test_the_kill_switch_and_the_day_gate(kb, weekly_on, monkeypatch):
    counts: dict = {}
    assert weekly.maybe_weekly(counts, now=A_MONDAY) is False, "not the configured day"
    monkeypatch.setattr(settings, "kb_weekly_report", False)
    assert weekly.maybe_weekly(counts, now=A_SUNDAY) is False, "kill switch"
    # Positive-only: a gated night adds NO counts key.
    assert counts == {}


def test_a_malformed_day_fails_toward_off(kb, weekly_on, monkeypatch, caplog):
    monkeypatch.setattr(settings, "kb_weekly_day", "sundae")
    counts: dict = {}
    assert weekly.maybe_weekly(counts, now=A_SUNDAY) is False
    assert counts == {}
    assert "KB_WEEKLY_DAY" in caplog.text


# ----------------------------------------------------------------- the run


def test_the_weekly_run_writes_the_item_and_pushes_ascii_under_20_lines(kb, weekly_on, monkeypatch):
    _nightly_report(
        "2026-08-10",
        queued_drained=3,
        queued_items_saved=2,
        review_triaged=100,
        eval_hit_at_5=0.95,
        eval_mrr=0.80,
        eval_top5_unverified_share=0.64,
    )
    _nightly_report(
        "2026-08-15",
        queued_drained=5,
        queued_items_saved=4,
        review_triaged=200,
        eval_hit_at_5=0.99,
        eval_mrr=0.90,
        eval_top5_unverified_share=0.61,
    )
    for _ in range(2):
        search_misses.record("esp32 ledc fade é", None, "mcp.kb_search")
    sent: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(
        weekly.notify, "push_digest", lambda lines, **kw: sent.append((lines, kw)) or True
    )

    counts: dict = {}
    assert weekly.maybe_weekly(counts, now=A_SUNDAY) is True
    assert counts["weekly_report_pushed"] is True
    assert counts["search_misses_swept"] == 0

    ((lines, kw),) = sent
    assert kw.get("title") == "KB weekly", "the weekly names its own ntfy title (ops-1)"
    assert lines[0].startswith("KB weekly ")
    assert len(lines) < weekly.PUSH_MAX_LINES
    assert all(line.isascii() for line in lines), "ntfy to a phone: pure ASCII"
    assert "7d: drained 8, saved 6, triaged 300" in lines
    assert "eval hit@5: 0.950 -> 0.990" in lines
    assert "top5 unverified: 0.640 -> 0.610" in lines
    assert "search misses: 2 (1 distinct)" in lines
    assert any("2x esp32 ledc fade" in line for line in lines)

    # The full table persisted as a kb-system report item, flagged weekly_report.
    item_id = counts["weekly_report_item_id"]
    with connect() as conn:
        body, meta = conn.execute(
            "SELECT body, meta FROM items WHERE id = %s", (item_id,)
        ).fetchone()
    assert body.startswith("KB weekly trend report")
    assert "search misses (7d): 2 total (empty 2, thin 0), 1 distinct" in body
    assert meta["weekly_report"] is True
    assert meta["nights"] == 2
    assert isinstance(meta["promoted_total"], int)


def test_promote_uptake_diffs_against_the_prior_weekly_checkpoint(kb, weekly_on, monkeypatch):
    """No resolved_at exists, so 'promoted in 7d' = total NOW minus the prior
    weekly item's persisted promoted_total (the eval-sentinel 'previous run'
    pattern — no new table)."""
    # Prior weekly checkpoint, minted >6 days ago so the rerun guard passes.
    prior_id = ingest(
        "kb-system",
        "report",
        "KB weekly report 2026-08-09",
        "KB weekly trend report",
        kind="chat",
        uri="weekly-report:2026-08-09T01:30:00+00:00",
        trust="verified",
        meta={"weekly_report": True, "promoted_total": 0},
    )
    from kbcore.db import transaction

    with transaction() as conn:
        conn.execute(
            "UPDATE items SET created_at = now() - make_interval(days => 7) WHERE id = %s",
            (prior_id,),
        )
    # One real promote through the owner-only chokepoint.
    item = ingest("p1", "note", "extracted fact", "body", trust="unverified", uri="t:x")
    qid = review.queue_review(item, "unverified_extraction")
    review.promote(qid)

    monkeypatch.setattr(weekly.notify, "push_digest", lambda lines, **kw: True)
    counts: dict = {}
    assert weekly.maybe_weekly(counts, now=A_SUNDAY) is True
    with connect() as conn:
        (meta,) = conn.execute(
            "SELECT meta FROM items WHERE id = %s", (counts["weekly_report_item_id"],)
        ).fetchone()
    assert meta["promoted_total"] == 1
    assert meta["promoted_7d"] == 1


def test_a_same_week_rerun_skips_without_a_counts_key(kb, weekly_on, monkeypatch):
    """The Sunday boot-catch-up double-fire guard: one weekly item per week."""
    monkeypatch.setattr(weekly.notify, "push_digest", lambda lines, **kw: True)
    first: dict = {}
    assert weekly.maybe_weekly(first, now=A_SUNDAY) is True
    again: dict = {}
    assert weekly.maybe_weekly(again, now=A_SUNDAY) is False
    assert "weekly_report_pushed" not in again, "a skip is not a failed push"
    with connect() as conn:
        (n,) = conn.execute(
            "SELECT count(*) FROM items WHERE project = 'kb-system' "
            "AND meta @> '{\"weekly_report\": true}'"
        ).fetchone()
    assert n == 1


def test_an_unconfigured_channel_still_writes_the_item(kb, weekly_on):
    """conftest blanks the ntfy channel: push False, report item still minted."""
    counts: dict = {}
    assert weekly.maybe_weekly(counts, now=A_SUNDAY) is False
    assert counts["weekly_report_pushed"] is False
    assert isinstance(counts["weekly_report_item_id"], int)


def test_retention_rides_the_weekly_pass(kb, weekly_on, monkeypatch):
    search_misses.record("ancient miss", None, "mcp.kb_search")
    from kbcore.db import transaction

    with transaction() as conn:
        conn.execute("UPDATE search_misses SET at = now() - make_interval(days => 120)")
    monkeypatch.setattr(weekly.notify, "push_digest", lambda lines, **kw: True)
    counts: dict = {}
    assert weekly.maybe_weekly(counts, now=A_SUNDAY) is True
    assert counts["search_misses_swept"] == 1


# ----------------------------------------------------------------- digest wiring


def test_push_run_digest_carries_the_weekly_rider(monkeypatch):
    called: list[dict] = []
    monkeypatch.setattr("kbworker.weekly.maybe_weekly", lambda counts: called.append(counts))
    assert digest_mod.push_run_digest({"queued_drained": 1}) is False  # channel blanked
    assert called and called[0]["queued_drained"] == 1


def test_a_broken_weekly_never_fails_the_digest(monkeypatch, caplog):
    def boom(counts):
        raise RuntimeError("weekly exploded")

    monkeypatch.setattr("kbworker.weekly.maybe_weekly", boom)
    assert digest_mod.push_run_digest({"queued_drained": 1}) is False
    assert "weekly report step failed" in caplog.text


def test_the_digest_line_counts_the_days_misses(monkeypatch):
    monkeypatch.setattr(search_misses, "count_since", lambda *a, **kw: 7)
    counts: dict = {"queued_drained": 1}
    digest_mod._note_search_misses(counts)
    assert counts["search_misses_24h"] == 7
    assert "empty-result searches: 7" in digest_mod.digest_lines(counts)


def test_no_misses_means_no_line_and_a_broken_count_costs_one_line_only(monkeypatch, caplog):
    assert not any(
        "searches:" in line
        for line in digest_mod.digest_lines({"queued_drained": 1, "search_misses_24h": 0})
    )

    def boom(*a, **kw):
        raise RuntimeError("no table")

    monkeypatch.setattr(search_misses, "count_since", boom)
    counts: dict = {"queued_drained": 1}
    digest_mod._note_search_misses(counts)
    assert "search_misses_24h" not in counts
    assert "search-miss count unavailable" in caplog.text

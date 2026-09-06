"""Wave H weekly instruments (review #8 H9, KB 31857; kbworker.weekly_instruments).

(a) the expiry ``eligible_remaining`` first -> last stocks line from a NESTED
key; (b) the typed-edge checkpoint diff — save_nudge.typed_edge_counts's first
caller — persisted in the weekly meta and rendered like promoted_total; (c) the
save-nudge fired INFO line (a journald instrument, never a counts key); (d) the
calibration n/p10/p50/p90 line from one grouped query; (e) decisive
suggestions + wall-blocked closed vs alternative_to linked; (f) ONE row per
LOCAL night with a runs column and a local header date.

Pure renderers are hermetic with a PINNED zone (never the box's); the run-level
tests use the real kb_test DB and conftest's deterministic embeddings.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from kbcore import db, graph, save_nudge, search_misses
from kbcore.config import settings
from kbcore.db import connect
from kbcore.ingest import ingest
from kbworker import weekly, weekly_instruments, weekly_reads, weekly_render
from tests.conftest import DB_UP

needs_db = pytest.mark.skipif(not DB_UP, reason="compose Postgres not reachable")
PKT = timezone(timedelta(hours=5))  # the deployment's zone, pinned
A_SUNDAY = datetime(2026, 8, 16, 1, 30)  # local; weekday() == 6 == _DAYS['sun']
MISS_STATS = {"total": 0, "distinct": 0, "empty": 0, "thin": 0}
NOW = datetime(2026, 8, 31, 1, 30, tzinfo=PKT)  # the Monday 01:30 weekly; night_of == 08-30


def _run(at: datetime, **meta):
    return (at, meta)


# A fragmented Saturday night — the 01:00 nightly (30 min, paused at 01:30
# local), its grid-restore resume at 03:00 local and a Sunday 10:00 local boot
# catch-up — plus a Sunday-EVENING 20:00 local run: four runs, two LOCAL nights
# under the start-keyed NOON rule (review #9a INS-1; re-pinned from the 06:00
# finish-keyed rule, under which the 10:00 catch-up was its own row). In UTC-date
# terms the two Saturday fragments fall on 08-29 while both Sunday runs are 08-30.
FRAGMENTED = [
    _run(
        datetime(2026, 8, 29, 20, 30, tzinfo=UTC),
        run_secs=1800,  # started 20:00Z = 01:00 local: the nightly's start keys the night
        queued_drained=3,
        queued_items_saved=2,
        review_triaged=100,
        review_expired=500,
        eval_hit_at_5=0.95,
        eval_mrr=0.90,
        review_queue_open=14_981,
        quiet_expiry_counts={"expired": 500, "eligible": 1267, "eligible_remaining": 767},
        wall_blocked_closed=2,
        contradiction_suggestions_pending=7,
    ),
    _run(
        datetime(2026, 8, 29, 22, 0, tzinfo=UTC),
        queued_drained=4,
        queued_items_saved=1,
        review_triaged=50,
        eval_hit_at_5=0.97,
        eval_mrr=0.91,
        review_queue_open=15_100,
        quiet_expiry_counts={"eligible_remaining": 600},
        wall_blocked_closed=1,
        contradiction_suggestions_pending=5,
    ),
    _run(
        datetime(2026, 8, 30, 5, 0, tzinfo=UTC),
        queued_drained=1,
        review_triaged=10,
        eval_hit_at_5=0.96,
        review_queue_open=15_200,
        quiet_expiry_counts={"eligible_remaining": 120},
    ),
    _run(
        datetime(2026, 8, 30, 15, 0, tzinfo=UTC),  # 20:00 local Sunday: opens the NEXT night
        queued_drained=2,
        review_triaged=5,
        eval_hit_at_5=0.99,
        eval_mrr=0.95,
        review_queue_open=15_300,
        quiet_expiry_counts={"eligible_remaining": 100},
    ),
]


# --- (f) rows by LOCAL night ------------------------------------------------------


def test_nights_group_fragments_and_file_pre_noon_runs_under_the_previous_night():
    grouped = weekly_instruments.nights(FRAGMENTED, PKT)
    assert [n for n, _ in grouped] == [date(2026, 8, 29), date(2026, 8, 30)]
    assert [len(runs) for _, runs in grouped] == [3, 1], (
        "the nightly, its resume AND the 10:00 catch-up are one night; the evening run is not"
    )
    assert weekly_instruments.nights_line(FRAGMENTED, PKT) == "nights reported: 2 (4 runs)"
    # The rule is LOCAL: pin a different zone and the grouping moves with it —
    # in UTC-5 the 15:00Z evening run is 10:00 local, i.e. still the morning
    # after the 29th's night, so all four runs fold into ONE night there.
    utc_minus5 = timezone(timedelta(hours=-5))
    assert [n for n, _ in weekly_instruments.nights(FRAGMENTED, utc_minus5)] == [date(2026, 8, 29)]


def test_night_rows_sum_flows_take_the_clean_runs_gauges_and_show_the_runs_column():
    rows = weekly_instruments.night_rows(FRAGMENTED, PKT)
    assert len(rows) == 2
    sat, sun = rows
    assert sat.startswith("2026-08-29   (3)  ")
    cells = sat.split()
    assert cells[2:6] == ["8", "3", "160", "500"], "flows SUM over the night's runs"
    assert cells[6:8] == ["0.970", "0.910"], (
        "eval columns come from the night's last fallback-free run CARRYING an mrr "
        "(the catch-up has none); review #9a INS-5"
    )
    assert cells[-1] == "-", "no run carries a rerank tally: the fb column is blank"
    assert sun.startswith("2026-08-30   (1)  ")
    assert sun.split()[2:6] == ["2", "-", "5", "-"], "absent keys render '-', never 0"


def test_report_body_and_push_use_local_nights_and_the_local_header_date():
    body = weekly_render._report_body(NOW, FRAGMENTED, MISS_STATS, [], 0, 0, None, 0, [], tz=PKT)
    lines = body.splitlines()
    assert "window: 7 nights 2026-08-23..2026-08-29; nights reported: 2 (4 runs)" in lines, (
        "the window is whole COMPLETED nights ending before night_of(now) (review #9a INS-6)"
    )
    header = next(ln for ln in lines if ln.startswith("night "))
    assert header.split()[:3] == ["night", "runs", "drained"] and header.split()[-1] == "fb"
    assert sum(ln.startswith("2026-08-29") for ln in lines) == 1, "one row per local night"
    assert weekly_instruments.FB_NOTE in lines
    push = weekly_render._push_lines(NOW, FRAGMENTED, MISS_STATS, [], 0, None, 42, tz=PKT)
    assert push[0] == "KB weekly 2026-08-31"
    assert push[1] == "nights reported: 2 (4 runs)"
    assert "7d: drained 10, saved 3, triaged 165" in push, "7d flows still sum every run"
    assert len(push) <= weekly_render.PUSH_MAX_LINES and all(ln.isascii() for ln in push)


# --- (a) eligible_remaining stocks line ---------------------------------------------


def test_expiry_eligible_left_reads_the_nested_key_first_to_last():
    series = weekly_instruments._nested_series(
        FRAGMENTED, "quiet_expiry_counts", "eligible_remaining"
    )
    assert series == [767.0, 600.0, 120.0, 100.0]
    body = weekly_render._report_body(NOW, FRAGMENTED, MISS_STATS, [], 0, 0, None, 0, [], tz=PKT)
    assert "  expiry eligible left:    767 -> 100" in body.splitlines()
    absent = weekly_render._report_body(NOW, [], MISS_STATS, [], 0, 0, None, 0, [], tz=PKT)
    assert "  expiry eligible left:    n/a" in absent.splitlines()
    assert (
        weekly_instruments._nested_series(
            [(NOW, {"quiet_expiry_counts": 5})], "quiet_expiry_counts", "x"
        )
        == []
    )


# --- (b) typed-edge checkpoint diff -------------------------------------------------


def test_typed_edge_diff_and_line_mint_then_diff_then_flag_new_rels():
    totals = {"fixes": 5, "depends_on": 2, "implements": 1, "alternative_to": 36}
    assert weekly_instruments.typed_edge_diff(totals, None) is None
    assert weekly_instruments._typed_edge_line(totals, None) == (
        "typed edges: checkpoint minted "
        "(totals fixes 5 depends_on 2 implements 1 alternative_to 36)"
    )
    prior = {"fixes": 3, "depends_on": 2, "implements": 0}  # a pre-alternative_to checkpoint
    diff = weekly_instruments.typed_edge_diff(totals, prior)
    assert diff == {"fixes": 2, "depends_on": 0, "implements": 1}
    assert weekly_instruments._typed_edge_line(totals, diff) == (
        "typed edges (7d): fixes +2 depends_on +0 implements +1 alternative_to 36 (new)"
    )
    assert weekly_instruments._typed_edge_line(None, None) == "typed edges (7d): n/a"
    shrunk = weekly_instruments.typed_edge_diff({"fixes": 1}, {"fixes": 4})
    assert shrunk == {"fixes": 0}, "a retirement never renders as negative growth"


# --- (d) calibration line, (e) contradiction lines -------------------------------------


def test_calibration_line_shapes():
    assert weekly_instruments._calibration_line(None, 0.05) == "calibration (7d): n/a"
    assert (
        weekly_instruments._calibration_line({"n": 0}, 0.05)
        == "calibration (7d): n=0; thin floor 0.05"
    )
    stats = {"n": 183, "p10": 0.1234, "p50": 0.5, "p90": 0.91}
    assert weekly_instruments._calibration_line(stats, 0.05) == (
        "calibration (7d): n=183, top-score p10/p50/p90 = 0.123/0.500/0.910; thin floor 0.05"
    )
    assert weekly_instruments._calibration_line({"n": 2, "p50": 0.3}, 0.0).endswith(
        "= -/0.300/-; thin floor 0"
    )


def test_contradiction_lines_from_report_data_and_the_typed_diff():
    lines = weekly_instruments._contradiction_lines(FRAGMENTED, {"alternative_to": 4})
    assert lines == [
        "decisive suggestions: 5",
        "wall-blocked closed 7d: 3 | alternative_to linked 7d: 4",
    ], "the newest gauge, the summed flow, the checkpoint diff"
    assert weekly_instruments._contradiction_lines([], None) == [
        "decisive suggestions: n/a",
        "wall-blocked closed 7d: 0 | alternative_to linked 7d: n/a",
    ]


# --- run level: the checkpoint persists and diffs; the query is real -----------------


@pytest.fixture()
def weekly_on(monkeypatch):
    monkeypatch.setattr(settings, "kb_weekly_report", True)
    monkeypatch.setattr(settings, "kb_weekly_day", "sun")
    monkeypatch.setattr(weekly.notify, "push_digest", lambda lines, **kw: True)


def _weekly_row(counts) -> tuple[str, dict]:
    with connect() as conn:
        return conn.execute(
            "SELECT body, meta FROM items WHERE id = %s", (counts["weekly_report_item_id"],)
        ).fetchone()


@needs_db
def test_first_weekly_mints_the_typed_edge_checkpoint(kb, weekly_on):
    counts: dict = {}
    assert weekly.maybe_weekly(counts, now=A_SUNDAY) is True
    body, meta = _weekly_row(counts)
    assert meta["typed_edge_totals"] == {
        "fixes": 0,
        "depends_on": 0,
        "implements": 0,
        "alternative_to": 0,
    }
    assert "typed_edges_7d" not in meta
    assert "typed edges: checkpoint minted" in body
    assert meta["local_nights"] == 0 and meta["nights"] == 0
    assert "calibration (7d): n=0; thin floor 0" in body, "conftest pins the floor at 0.0"


@needs_db
def test_weekly_diffs_typed_edges_against_the_prior_checkpoint(kb, weekly_on):
    prior_id = ingest(
        "kb-system",
        "report",
        "KB weekly report 2026-08-09",
        "KB weekly trend report",
        kind="chat",
        uri="weekly-report:2026-08-09T01:30:00+00:00",
        trust="verified",
        meta={
            "weekly_report": True,
            "promoted_total": 0,
            "typed_edge_totals": {
                "fixes": 0,
                "depends_on": 0,
                "implements": 0,
                "alternative_to": 0,
            },
        },
    )
    with db.transaction() as conn:
        conn.execute(
            "UPDATE items SET created_at = now() - make_interval(days => 7) WHERE id = %s",
            (prior_id,),
        )
    a = ingest("p", "bug", "bug a", "body a", uri="u:a")
    b = ingest("p", "note", "fix b", "body b", uri="u:b")
    c = ingest("q", "note", "twin c", "body c", uri="u:c")
    graph.link(b, "fixes", a)
    graph.link(c, "alternative_to", a)
    graph.link(c, "mentions", a)  # not a tracked edge

    counts: dict = {}
    assert weekly.maybe_weekly(counts, now=A_SUNDAY) is True
    body, meta = _weekly_row(counts)
    assert meta["typed_edge_totals"] == {
        "fixes": 1,
        "depends_on": 0,
        "implements": 0,
        "alternative_to": 1,
    }
    assert meta["typed_edges_7d"] == {
        "fixes": 1,
        "depends_on": 0,
        "implements": 0,
        "alternative_to": 1,
    }
    assert "typed edges (7d): fixes +1 depends_on +0 implements +0 alternative_to +1" in body
    assert "wall-blocked closed 7d: 0 | alternative_to linked 7d: 1" in body


@needs_db
def test_calibration_stats_is_one_grouped_query_over_scored_calibration_rows(kb):
    for i in range(1, 11):  # top scores 0.1 .. 1.0
        assert search_misses.record("q", None, "mcp.kb_search", "calibration", i / 10, "vector")
    search_misses.record("q", None, "mcp.kb_search", "thin", 0.01, "vector")  # not calibration
    search_misses.record("q", None, "mcp.kb_search", "calibration", None, "none")  # unscored
    stats = weekly._calibration_stats()
    assert stats["n"] == 10
    assert round(stats["p10"], 3) == 0.19 and round(stats["p50"], 3) == 0.55
    assert round(stats["p90"], 3) == 0.91
    with db.transaction() as conn:
        conn.execute("UPDATE search_misses SET at = now() - make_interval(days => 30)")
    assert weekly._calibration_stats()["n"] == 0, "windowed to the week"


def test_calibration_stats_fails_open(monkeypatch):
    def _boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(weekly_reads, "connect", _boom)
    assert weekly._calibration_stats() is None, "the seam re-export resolves to the same function"
    monkeypatch.setattr(weekly_reads.save_nudge, "typed_edge_counts", lambda rels: 1 / 0)
    assert weekly._typed_edge_totals() is None


# --- (c) the fired-nudge journal line + the rels widening ---------------------------------


def _vec(*head: float) -> str:
    v = [float(x) for x in head] + [0.0] * (settings.kb_embed_dim - len(head))
    return "[" + ",".join(repr(x) for x in v) + "]"


def _force_lead(item_id: int, vec: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE chunks SET embedding = %s::vector WHERE item_id = %s AND ord = 0",
            (vec, item_id),
        )


@needs_db
def test_save_nudge_logs_one_fired_line_and_no_counts_key(kb, caplog):
    caplog.set_level(logging.INFO, logger="kbcore.save_nudge")
    bug = ingest("p", "bug", "spi cs glitch", "body", uri="u:bug")
    _force_lead(bug, _vec(1.0, 0.0))
    saved = ingest("p", "bug", "spi chip-select races", "body", uri="u:saved")
    _force_lead(saved, _vec(1.0, 0.01))
    text = save_nudge.suffix("p", "bug", "spi chip-select races", saved, False)
    assert f"[{bug}] spi cs glitch" in text
    assert f"save-nudge fired item={saved} candidates=1" in caplog.text
    caplog.clear()
    lonely = ingest("q", "bug", "lonely", "body", uri="u:lonely")
    _force_lead(lonely, _vec(0.0, 1.0))
    assert save_nudge.suffix("q", "bug", "lonely", lonely, False) == ""
    assert "save-nudge fired" not in caplog.text, "no candidates, no fire, no line"


@needs_db
def test_typed_edge_counts_default_shape_unchanged_and_widened_on_request(kb):
    assert save_nudge.typed_edge_counts() == {"fixes": 0, "depends_on": 0, "implements": 0}
    widened = save_nudge.typed_edge_counts(rels=weekly_instruments.WEEKLY_EDGE_RELS)
    assert widened == {"fixes": 0, "depends_on": 0, "implements": 0, "alternative_to": 0}

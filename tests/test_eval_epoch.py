"""Eval EPOCH plumbing (E1, KB 31857 review #8 / KB 32942 review #9a).

golden.yaml changes land only at a review boundary and "re-anchor the floor
and reset the WoW baseline in the same commit". The epoch is the golden
file's content hash (kbcore.eval_epoch): every eval report records it, the
clean-baseline read (kbcore.eval_baseline) and the detector
(kbworker.eval_regression) compare only within it, the digest names the first
night of a new epoch, and the weekly trend never draws one line through two
golden sets (kbworker.eval_epoch_lines).

E2 (review #10): the key also covers the embed model and the CONFIGURED embed
host list (never a serving fallback, which ``eval_embed_host`` records), and
an open regression row from another epoch is resolved 'epoch changed' FIRST.

Real kb_test DB for the baseline/detector halves (seeded report items, as the
Wave H1 baseline tests do); the renderers are hermetic with a PINNED zone.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest

from kbcore import db, embed, embed_identity, eval_baseline, eval_epoch, eval_harness
from kbcore import review as review_mod
from kbcore.config import settings
from kbcore.ingest import ingest
from kbcore.items import get_item
from kbworker import (
    digest,
    eval_cases,
    eval_detector_io,
    eval_epoch_lines,
    eval_regression,
    eval_sentinel,
    run_report,
    weekly_instruments,
    weekly_render,
)
from kbworker.digest_lines import night_of
from tests.conftest import DB_UP, fake_vector

# conftest autouse-mocks embed.embed_texts; the serving-host tests need the real
# one (host selection lives there) — captured at import, before the fixture runs.
_REAL_EMBED_TEXTS = embed.embed_texts
_REAL_IDENTITY_GET = embed_identity._get  # conftest refuses it per test; restored on purpose

needs_db = pytest.mark.skipif(not DB_UP, reason="compose Postgres not reachable")
PKT = timezone(timedelta(hours=5))  # the deployment's zone, pinned (never the box's)
MISS_STATS = {"total": 0, "distinct": 0, "empty": 0, "thin": 0}
NOW = datetime(2026, 9, 20, 1, 4, tzinfo=PKT)  # a Sunday weekly instant; night_of == 09-19
E1 = "e1e1e1e1e1e1"
E0 = "000000000000"


def _report(**meta) -> int:
    return ingest(
        "kb-system",
        "report",
        "Consolidation report",
        "Nightly consolidation report\n\n- test",
        kind="chat",
        uri=f"report:{uuid.uuid4()}",
        trust="verified",
        meta={"consolidation": True, **meta},
    )


def _ok(**meta) -> int:
    return _report(**{"eval_status": "ok", "eval_hit_at_5": 0.98, "eval_mrr": 0.92, **meta})


def _rep(at: datetime, **meta):
    """A hermetic report meta as the sentinel writes it: ``eval_status='ok'`` beside
    the metrics unless the test says otherwise (an error night overrides it)."""
    return (at, {"eval_status": "ok", **meta})


# --- 1. the epoch id -----------------------------------------------------------------


def test_golden_hash_is_content_derived_stable_and_twelve_hex(tmp_path):
    golden = tmp_path / "golden.yaml"
    golden.write_text("- q: a\n  expect_substr: [x]\n", encoding="utf-8")
    first = eval_epoch.golden_hash(golden)
    assert len(first) == eval_epoch.HASH_HEX_CHARS == 12 and int(first, 16) >= 0
    assert eval_epoch.golden_hash(golden) == first, "same bytes, same epoch"
    golden.write_text("- q: a\n  expect_substr: [x]\n- q: b\n", encoding="utf-8")
    assert eval_epoch.golden_hash(golden) != first, "ANY change to the file opens a new epoch"
    assert eval_epoch.golden_hash(tmp_path / "absent.yaml") == eval_epoch.golden_hash(
        tmp_path / "also-absent.yaml"
    ), "a missing file hashes deterministically (empty bytes) instead of raising"
    live = eval_epoch.golden_hash()  # the default: the repo's eval/golden.yaml
    assert len(live) == 12 and live == eval_epoch.golden_hash()


def test_sentinel_records_the_golden_hash_next_to_eval_queries(monkeypatch):
    monkeypatch.setattr(eval_sentinel, "TRUST_METRIC_ENABLED", False)
    monkeypatch.setattr(eval_sentinel, "_detect_regression", lambda counts, h, m: None)
    cases = [{"q": "a", "expect_substr": ["x"]}, {"q": "b", "expect_substr": ["x"]}]
    monkeypatch.setattr(eval_sentinel.eval_harness, "load_golden", lambda golden_path=None: cases)
    monkeypatch.setattr(
        eval_sentinel.eval_harness,
        "evaluate",
        lambda golden=None, golden_path=None: ([("a", 1), ("b", 2)], 1.0, 0.75),
    )
    counts: dict = {}
    eval_sentinel.run_sentinel(counts)
    assert counts["eval_status"] == "ok" and counts["eval_queries"] == 2
    assert counts["eval_golden_hash"] == eval_epoch.golden_hash()


# --- 2. the epoch-confined baseline read ---------------------------------------------


@needs_db
def test_baseline_with_a_hash_skips_other_epochs_and_untracked_reports(kb, caplog):
    caplog.set_level(logging.INFO, logger="kbcore.eval_baseline")
    _ok(eval_mrr=0.9077)  # pre-E1: no key at all
    other = _ok(eval_mrr=0.95, eval_golden_hash=E0)  # another golden set
    prior = eval_baseline.previous_eval_metrics(E1)
    assert prior is None, "nothing in tonight's epoch: no WoW baseline"
    assert "golden epoch untracked, tonight is e1e1e1e1e1e1" in caplog.text
    assert "golden epoch 000000000000, tonight is e1e1e1e1e1e1" in caplog.text
    none_prior, skips = eval_baseline.read_baseline(E1)
    assert none_prior is None and skips == {"skipped_fallback": 0, "skipped_epoch": 2}
    # Untracked read: byte-identical to before — the newest ok report, no epoch key.
    assert eval_baseline.previous_eval_metrics() == {
        "hit_at_5": 0.98,
        "mrr": 0.95,
        "report_id": other,
        "skipped_fallback": 0,
    }
    mine = _ok(eval_mrr=0.91, eval_golden_hash=E1)
    _ok(eval_mrr=0.99, eval_golden_hash=E0)  # newer, but a different set: walked over
    assert eval_baseline.previous_eval_metrics(E1) == {
        "hit_at_5": 0.98,
        "mrr": 0.91,
        "report_id": mine,
        "skipped_fallback": 0,
        "skipped_epoch": 1,
    }


@needs_db
def test_baseline_epoch_and_fallback_skips_compose(kb):
    _ok(eval_mrr=0.90, eval_golden_hash=E1)
    _ok(eval_mrr=0.94, eval_golden_hash=E1, eval_rerank_tally={"active": 40, "fallback": 15})
    _ok(eval_mrr=0.97, eval_golden_hash=E0)
    prior = eval_baseline.previous_eval_metrics(E1)
    assert prior["mrr"] == 0.90 and prior["skipped_fallback"] == 1 and prior["skipped_epoch"] == 1


# --- 3. the detector: first night resets, second night compares ----------------------


@needs_db
def test_detector_marks_the_first_night_of_an_epoch_and_not_the_second(kb, caplog):
    _ok(eval_mrr=0.9077)  # the pre-E1 52-case world
    first = {"eval_regression": False, "eval_queries": 59, "eval_golden_hash": E1}
    with caplog.at_level(logging.INFO, logger="kbworker.eval_sentinel"):
        eval_sentinel._detect_regression(first, 0.98, 0.85)
    assert first["eval_epoch_reset"] == {"golden_hash": E1, "queries": 59, "skipped_epoch": 1}
    assert first["eval_regression"] is False, "no prior: a -0.06 MRR step is not a WoW drop"
    assert "eval epoch e1e1e1e1e1e1 (n=59): first night" in caplog.text
    assert "floors 0.75/0.50 still apply" in caplog.text
    _ok(eval_mrr=0.85, eval_golden_hash=E1)  # tonight's report, persisted as the run does
    second = {"eval_regression": False, "eval_queries": 59, "eval_golden_hash": E1}
    eval_sentinel._detect_regression(second, 0.98, 0.85)
    assert "eval_epoch_reset" not in second, "the second night has a same-epoch prior"
    assert second["eval_regression"] is False
    third = {"eval_regression": False, "eval_queries": 59, "eval_golden_hash": E1}
    eval_sentinel._detect_regression(third, 0.98, 0.78)  # -0.07 vs the epoch's own prior
    assert third["eval_regression"] is True and "eval_epoch_reset" not in third
    _qid, detail = review_mod.open_review_by_reason(eval_regression.REVIEW_REASON)
    assert detail["baseline_mrr"] == 0.85, "anchored to the epoch's prior, not the 52-case 0.9077"


@needs_db
def test_detector_floors_still_gate_the_reset_night_and_a_genuine_first_run_is_no_reset(kb):
    fresh = {"eval_regression": False, "eval_queries": 59, "eval_golden_hash": E1}
    eval_sentinel._detect_regression(fresh, 0.98, 0.92)
    assert "eval_epoch_reset" not in fresh, "no prior reports at all: a first run, not a reset"
    _ok(eval_mrr=0.92)  # untracked prior -> tonight is a reset night
    red = {"eval_regression": False, "eval_queries": 59, "eval_golden_hash": E1}
    eval_sentinel._detect_regression(red, 0.70, 0.40)  # under the 0.75/0.5 code-default floors
    assert red["eval_epoch_reset"]["skipped_epoch"] == 1
    assert red["eval_regression"] is True, "the anchored floors apply on the reset night"
    _qid, detail = review_mod.open_review_by_reason(eval_regression.REVIEW_REASON)
    assert detail["baseline_mrr"] == 0.5, "the anchor is the floor: no same-epoch prior to max with"


@needs_db
def test_detector_does_not_re_reset_when_the_epochs_only_nights_fell_back(kb, caplog):
    _ok(eval_mrr=0.9077)  # untracked prior: the pre-E1 world
    _ok(eval_mrr=0.94, eval_golden_hash=E1, eval_rerank_tally={"active": 48, "fallback": 11})
    # Night 2 of the epoch: no CLEAN prior yet, but night 1 exists in the epoch.
    assert eval_baseline.read_baseline(E1) == (None, {"skipped_fallback": 1, "skipped_epoch": 1})
    second = {"eval_regression": False, "eval_queries": 59, "eval_golden_hash": E1}
    with caplog.at_level(logging.INFO, logger="kbworker.eval_sentinel"):
        eval_sentinel._detect_regression(second, 0.98, 0.90)
    assert "eval_epoch_reset" not in second, "a fallback night 1 is still the epoch's night 1"
    assert "first night" not in caplog.text
    assert second["eval_regression"] is False, "no clean prior: floors only, and these clear them"
    assert not any("eval epoch" in ln for ln in digest.digest_lines(second))
    # The rule relies on read_baseline tallying a report under at most ONE skip with
    # the epoch filter first: an OTHER-epoch fallback night is skipped_epoch, never
    # skipped_fallback — so 'fallback skips == 0' really means 'no same-epoch report'.
    _ok(eval_mrr=0.9, eval_golden_hash=E0, eval_rerank_tally={"active": 40, "fallback": 5})
    assert eval_baseline.read_baseline(E1) == (None, {"skipped_fallback": 1, "skipped_epoch": 2})


@needs_db
def test_detector_without_a_hash_reads_the_unfiltered_baseline(kb):
    _ok(eval_mrr=0.92)
    counts = {"eval_regression": False}
    eval_sentinel._detect_regression(counts, 0.98, 0.80)  # -0.12 vs the untracked prior
    assert counts["eval_regression"] is True and "eval_epoch_reset" not in counts


# --- 4. the digest line ----------------------------------------------------------------


def test_digest_names_the_first_night_of_an_epoch_only_when_the_run_carries_the_key():
    counts = {"queued_drained": 3, "eval_status": "ok", "eval_golden_hash": E1}
    assert not any("eval epoch" in ln for ln in digest.digest_lines(counts)), (
        "a tracked but ordinary night contributes no epoch line"
    )
    counts["eval_epoch_reset"] = {"golden_hash": E1, "queries": 59, "skipped_epoch": 7}
    lines = digest.digest_lines(counts)
    assert "eval epoch e1e1e1e1e1e1 (n=59): first night - WoW baseline reset, floors only" in lines
    assert all(ln.isascii() for ln in lines), "ntfy to a phone: pure ASCII"
    assert lines.index("drained: 3") < lines.index(next(ln for ln in lines if "epoch" in ln)), (
        "a routine fact, after the flows"
    )
    assert eval_epoch_lines.eval_epoch_line({"eval_epoch_reset": "bogus"}) is None


def test_eval_red_names_mrr_and_the_floors_the_detector_compared_plus_the_cause():
    counts = {
        "eval_regression": True,
        "eval_hit_at_5": 0.9508,
        "eval_mrr": 0.8678,
        "eval_floor_hit_at_5": 0.97,  # tonight's compared floors, not the render-time settings
        "eval_floor_mrr": 0.86,
        "eval_regression_cause": ["floor_hit_at_5"],
        "eval_case_persistent": [
            {"case": 3, "satisfier": None, "nights": 4, "since": "2026-11-02"}
        ],
    }
    lines = digest.digest_lines(counts)
    assert lines[:2] == [
        "EVAL RED: hit@5 0.9508 (floor 0.97), MRR 0.8678 (floor 0.86) - floor_hit_at_5",
        "EVAL CASE #3 (?): red 4 nights since 2026-11-02",
    ]
    pre_e2 = digest.digest_lines({"eval_regression": True, "eval_hit_at_5": 0.5})
    assert pre_e2[0] == "EVAL RED: hit@5 0.5 (floor 0.75)", "a pre-E2 run renders byte-identically"
    assert eval_epoch_lines.eval_case_lines({"eval_case_persistent": "bogus"}) == []
    assert eval_epoch_lines.first_pass_lines({"eval_case_first_pass": [None, {"case": 2}]}) == [
        "eval case #2 (?): first pass after 3+ red nights"
    ]
    green = {"eval_regression": False, "eval_case_persistent": counts["eval_case_persistent"]}
    assert not any(ln.startswith("EVAL") for ln in digest.digest_lines(green)), "alerts need red"


# --- 5. the weekly series splits on the hash -----------------------------------------


def test_weekly_eval_series_read_the_latest_epoch_and_say_when_it_reset():
    day = timedelta(days=1)
    t0 = datetime(2026, 9, 12, 20, 0, tzinfo=UTC)  # 01:00 local Sep 13: night 09-12
    week = [
        _rep(t0, eval_hit_at_5=0.9808, eval_mrr=0.9077),  # untracked: the 52-case world
        _rep(t0 + day, eval_hit_at_5=0.9808, eval_mrr=0.9077),
        _rep(t0 + 2 * day, eval_hit_at_5=0.9808, eval_mrr=0.9269, eval_golden_hash=E0),
        _rep(t0 + 3 * day, eval_hit_at_5=0.9661, eval_mrr=0.8800, eval_golden_hash=E1),
        _rep(t0 + 4 * day, eval_hit_at_5=0.9831, eval_mrr=0.9000, eval_golden_hash=E1),
        _rep(
            t0 + 5 * day,
            eval_hit_at_5=1.0,
            eval_mrr=0.9500,
            eval_golden_hash=E1,
            eval_rerank_tally={"active": 40, "fallback": 19, "off": 0},
        ),
    ]
    assert eval_epoch_lines.epoch_groups(week) == [None, E0, E1]
    assert [m["eval_mrr"] for _a, m in eval_epoch_lines.epoch_reports(week)] == [0.88, 0.9, 0.95]
    assert [m["eval_mrr"] for _a, m in eval_epoch_lines.epoch_clean(week)] == [0.88, 0.9]
    assert eval_epoch_lines.epoch_suffix(week, PKT) == " (epoch reset 2026-09-15)"
    push = weekly_render._push_lines(NOW, week, MISS_STATS, [], 0, None, 1, tz=PKT)
    assert "eval mrr: 0.880 -> 0.900 (epoch reset 2026-09-15)" in push
    assert "eval hit@5: 0.966 -> 0.983" in push, "hit@5 reads the same epoch; the note rides mrr"
    assert all(ln.isascii() for ln in push) and len(push) <= weekly_render.PUSH_MAX_LINES
    # One epoch only (tracked or not): the series is what it always was, no note.
    untracked = week[:2]
    assert eval_epoch_lines.epoch_suffix(untracked, PKT) == ""
    push = weekly_render._push_lines(NOW, untracked, MISS_STATS, [], 0, None, 1, tz=PKT)
    assert "eval mrr: 0.908 -> 0.908" in push
    tracked = week[3:5]
    assert eval_epoch_lines.epoch_suffix(tracked, PKT) == ""
    push = weekly_render._push_lines(NOW, tracked, MISS_STATS, [], 0, None, 1, tz=PKT)
    assert "eval mrr: 0.880 -> 0.900" in push
    assert eval_epoch_lines.epoch_reports([]) == [] and eval_epoch_lines.epoch_suffix([]) == ""


# --- 6. epochs are defined over EVALUATED reports only (review finding P2) ------------

T0 = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)  # 01:00 local Sep 14: night 09-13
DAY = timedelta(days=1)


def _e1_nights() -> list:
    """Two clean E1 nights, 09-14 and 09-15 local."""
    return [
        _rep(T0 + DAY, eval_hit_at_5=0.9661, eval_mrr=0.8800, eval_golden_hash=E1),
        _rep(T0 + 2 * DAY, eval_hit_at_5=0.9831, eval_mrr=0.9000, eval_golden_hash=E1),
    ]


def test_a_trailing_error_night_neither_opens_a_group_nor_selects_the_pre_epoch_one():
    e1 = _e1_nights()
    error = _rep(T0 + 3 * DAY, eval_status="error", queued_drained=4)  # no hash, no metrics
    window = e1 + [error]
    assert eval_epoch_lines.epoch_groups(window) == [E1], "an error night is no epoch"
    assert eval_epoch_lines.epoch_reports(window) == e1
    suffix = eval_epoch_lines.epoch_suffix
    assert suffix(window, PKT) == suffix(e1, PKT) == "", "unchanged by the error night"
    push = weekly_render._push_lines(NOW, window, MISS_STATS, [], 0, None, 1, tz=PKT)
    assert "eval mrr: 0.880 -> 0.900" in push and "eval hit@5: 0.966 -> 0.983" in push
    # Pre-fix, reports[-1] carried no key, so the key-less group was 'latest': the
    # series trended nothing (n/a) under a bogus ' (epoch reset 2026-09-16)'.
    assert not any("epoch reset" in ln for ln in push)


def test_an_error_night_inside_a_single_epoch_is_no_reset():
    first, second = _e1_nights()
    mid_error = _rep(first[0] + timedelta(hours=12), eval_status="error")
    skipped = _rep(first[0] + timedelta(hours=13), eval_status="skipped")
    window = [first, mid_error, skipped, second]
    assert eval_epoch_lines.epoch_groups(window) == [E1]
    assert eval_epoch_lines.epoch_suffix(window, PKT) == ""
    assert [m["eval_mrr"] for _a, m in eval_epoch_lines.epoch_clean(window)] == [0.88, 0.9]


def test_a_trailing_disabled_night_keeps_the_latest_epoch_and_its_reset_night():
    pre = _rep(T0, eval_hit_at_5=0.9808, eval_mrr=0.9077)  # untracked: the 52-case world
    window = [pre, *_e1_nights(), _rep(T0 + 3 * DAY, eval_status="disabled")]
    assert eval_epoch_lines.epoch_groups(window) == [None, E1]
    assert [m["eval_mrr"] for _a, m in eval_epoch_lines.epoch_reports(window)] == [0.88, 0.9]
    assert eval_epoch_lines.epoch_suffix(window, PKT) == " (epoch reset 2026-09-14)"
    push = weekly_render._push_lines(NOW, window, MISS_STATS, [], 0, None, 1, tz=PKT)
    assert "eval mrr: 0.880 -> 0.900 (epoch reset 2026-09-14)" in push
    nothing_evaluated = [_rep(T0, eval_status="error"), _rep(T0 + DAY, eval_status="disabled")]
    assert eval_epoch_lines.epoch_reports(nothing_evaluated) == [], "n/a, as before"
    assert eval_epoch_lines.epoch_suffix(nothing_evaluated, PKT) == ""


# --- 7. the reset night is keyed on the run's START, like the table rows (P3) ---------


def test_the_reset_night_is_keyed_on_the_runs_start_like_the_table_rows():
    # The epoch's first report FINISHED 12:30 local Sep 15 (07:30 UTC), past the noon
    # rollover, but the 6 h run STARTED 06:30 local: night 09-14 — the row
    # weekly_instruments.nights files it under. Keyed on the finish it read 09-15,
    # a row the table does not have.
    pre = _rep(T0, eval_hit_at_5=0.9808, eval_mrr=0.9077)
    first = _rep(
        datetime(2026, 9, 15, 7, 30, tzinfo=UTC),
        run_secs=6 * 3600,
        eval_hit_at_5=0.9661,
        eval_mrr=0.88,
        eval_golden_hash=E1,
    )
    ((table_night, _runs),) = weekly_instruments.nights([first], PKT)
    assert table_night.isoformat() == "2026-09-14"
    assert night_of(first[0], PKT).isoformat() == "2026-09-15", "the finish is the next night"
    assert eval_epoch_lines.epoch_suffix([pre, first], PKT) == " (epoch reset 2026-09-14)"
    push = weekly_render._push_lines(NOW, [pre, first], MISS_STATS, [], 0, None, 1, tz=PKT)
    assert "eval mrr: 0.880 (epoch reset 2026-09-14)" in push


# --- 8. E2: the key covers the embedder; a serving fallback is recorded, not keyed ----

REMOTE = "http://kbuser:s3cret@10.0.0.254:11434"  # credentials in the URL: never rendered
LOCAL = "http://127.0.0.1:11434"


def _golden(tmp_path):
    golden = tmp_path / "golden.yaml"
    golden.write_text("- q: a b c d\n  expect_substr: [x]\n", encoding="utf-8")
    return golden


def test_the_epoch_key_is_golden_bytes_model_and_configured_hosts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kb_embed_url", REMOTE)
    monkeypatch.setattr(settings, "ollama_url", LOCAL + "/")
    golden = _golden(tmp_path)
    assert eval_epoch.embed_hosts_key() == "http://10.0.0.254:11434," + LOCAL
    expected = hashlib.sha256(
        golden.read_bytes()
        + b"\x00"
        + settings.kb_embed_model.encode()
        + b"\x00"
        + eval_epoch.embed_hosts_key().encode()
    ).hexdigest()[:12]
    assert eval_epoch.golden_hash(golden) == expected, "the pinned E2 formula"
    assert eval_epoch.golden_hash(golden) != hashlib.sha256(golden.read_bytes()).hexdigest()[:12]


def test_the_key_changes_with_the_model_or_configured_host_but_not_a_serving_fallback(
    tmp_path, monkeypatch
):
    embed.reset_host_health()
    monkeypatch.setattr(settings, "kb_embed_url", REMOTE)
    monkeypatch.setattr(settings, "ollama_url", LOCAL)
    golden = _golden(tmp_path)
    key = eval_epoch.golden_hash(golden)
    embed._mark_down(settings.embed_url)  # the remote is asleep: tonight's embeds fall back
    try:
        assert eval_epoch.golden_hash(golden) == key, "a serving fallback is not an epoch"
    finally:
        embed.reset_host_health()
    monkeypatch.setattr(settings, "kb_embed_url", "http://kbuser:rotated@10.0.0.254:11434")
    assert eval_epoch.golden_hash(golden) == key, "a rotated URL password is not a ranking input"
    monkeypatch.setattr(settings, "kb_embed_url", "http://10.0.0.77:11434")
    moved = eval_epoch.golden_hash(golden)
    assert moved != key, "a re-pointed KB_EMBED_URL opens a new epoch"
    monkeypatch.setattr(settings, "kb_embed_url", "")
    assert eval_epoch.golden_hash(golden) not in (key, moved), "single-host is its own key"
    monkeypatch.setattr(settings, "kb_embed_url", REMOTE)
    assert eval_epoch.golden_hash(golden) == key
    # Swapped RELATIVE to whatever the box configures (TA-13: never a hard-coded name).
    monkeypatch.setattr(settings, "kb_embed_model", settings.kb_embed_model + "-swapped")
    assert eval_epoch.golden_hash(golden) != key, "an embed model swap opens a new epoch"


def test_the_serving_host_is_recorded_per_query_embed_without_credentials(monkeypatch):
    embed.reset_host_health()
    monkeypatch.setattr(embed, "embed_texts", _REAL_EMBED_TEXTS)
    monkeypatch.setattr(settings, "kb_embed_url", REMOTE)
    monkeypatch.setattr(settings, "ollama_url", LOCAL)
    dead = {REMOTE}

    def fake_post(url, **kw):
        host = url.rsplit("/api/", 1)[0]
        if host in dead:
            raise httpx.ConnectTimeout("box is asleep")
        vec = [0.1] * settings.kb_embed_dim
        return httpx.Response(200, json={"embeddings": [vec]}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    try:
        embed.embed_query("first probe")
        assert embed.last_query_host() == LOCAL, "the fallback served it"
        dead.clear()
        embed.reset_host_health()
        embed.embed_query("second probe")
        assert embed.last_query_host() == "http://10.0.0.254:11434", "no userinfo, ever"
        dead.add(REMOTE)  # the remote dies: the next DOCUMENT embed is served locally ...
        embed.embed_texts(["a document chunk"])
        assert embed._served.host == LOCAL, "fixture: the document embed moved hosts"
        assert embed.last_query_host() == "http://10.0.0.254:11434", "... and is no query embed"
    finally:
        embed.reset_host_health()


def _hosts(monkeypatch, dead: set[str]) -> None:
    """The REAL embed_texts over a fake transport; hosts in ``dead`` time out."""
    embed.reset_host_health()
    monkeypatch.setattr(embed, "embed_texts", _REAL_EMBED_TEXTS)
    monkeypatch.setattr(settings, "kb_embed_url", REMOTE)
    monkeypatch.setattr(settings, "ollama_url", LOCAL)

    def fake_post(url, **kw):
        if url.rsplit("/api/", 1)[0] in dead:
            raise httpx.ConnectTimeout("box is asleep")
        vecs = [fake_vector(t) for t in kw["json"]["input"]]
        return httpx.Response(200, json={"embeddings": vecs}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)


def test_a_document_embed_on_another_host_never_moves_the_query_host(monkeypatch):
    dead = {REMOTE}
    _hosts(monkeypatch, dead)
    try:
        embed.embed_query("first probe")
        assert embed.last_query_host() == LOCAL
        dead.clear()
        embed.reset_host_health()
        embed.embed_texts(["a document chunk"])  # served by the REMOTE this time
        assert embed.last_query_host() == LOCAL
    finally:
        embed.reset_host_health()


def test_a_test_double_after_a_real_embed_records_no_stale_host(monkeypatch):
    """embed_query resets the served host first: a double that records none reads None."""
    _hosts(monkeypatch, set())
    try:
        embed.embed_query("served by the remote")
        assert embed.last_query_host() == "http://10.0.0.254:11434"
        monkeypatch.setattr(
            embed, "embed_texts", lambda texts, **kw: [fake_vector(t) for t in texts]
        )
        embed.embed_query("served by a test double")
        assert embed.last_query_host() is None, "only a host that served THIS call is recorded"
    finally:
        embed.reset_host_health()


def test_public_host_drops_userinfo_query_and_fragment():
    assert embed.public_host("http://u:p@h.example:1/x?token=abc#frag") == "http://h.example:1/x"
    assert embed.public_host(LOCAL) == LOCAL


def test_replay_keys_record_the_serving_host_runs_in_serving_order():
    def case(q, host):
        return eval_harness.CaseResult(q, 1, True, top_ids=[7], embed_host=host)

    r, loc = "http://r:1", "http://l:2"
    one = eval_cases.replay_keys([case("a", r), case("b", r)])
    assert one["eval_embed_host"] == r, "a healthy night reads one host"
    mixed = [case("a", r), case("b", loc), case("c", r)]
    assert eval_cases.replay_keys(mixed)["eval_embed_host"] == f"{r},{loc},{r}", (
        "A,B,A is a replay that came BACK to the primary — not one that ended on the fallback"
    )
    flappy = [case(str(i), r if i % 2 == 0 else loc) for i in range(9)]  # 9 runs
    assert eval_cases.EMBED_HOST_RUNS_MAX == 6
    assert eval_cases.replay_keys(flappy)["eval_embed_host"] == ",".join([r, loc] * 3) + ",+3"
    assert "eval_embed_host" not in eval_cases.replay_keys([case("a", None)]), "none recorded"


def test_the_fingerprint_is_one_query_embed_and_fails_open(monkeypatch, caplog):
    calls: list[list[str]] = []
    real = embed.embed_texts  # conftest's deterministic fake

    def counting(texts, **kw):
        calls.append(list(texts))
        return real(texts, **kw)

    monkeypatch.setattr(embed, "embed_texts", counting)
    keys = eval_cases.replay_keys([])
    assert len(calls) == 1, "exactly one extra embed call per eval run"
    fp = keys["eval_embed_fingerprint"]
    assert len(fp) == 12 and int(fp, 16) >= 0 and fp == embed.query_fingerprint()
    assert calls[0] == [embed.FINGERPRINT_PROBE], "the FIXED probe, as a query"
    monkeypatch.setattr(settings, "kb_embed_query_prefix", "Instruct: x\nQuery: ")
    assert embed.query_fingerprint() != fp, "the query prefix is part of the identity"

    def down(texts, **kw):
        raise RuntimeError("embedder down")

    monkeypatch.setattr(embed, "embed_texts", down)
    assert "eval_embed_fingerprint" not in eval_cases.replay_keys([])
    assert "embed fingerprint unavailable" in caplog.text


# --- 9. E2: an open row from another epoch closes 'epoch changed' ---------------------


E2 = "e2e2e2e2e2e2"


def _row_detail(qid: int) -> tuple[bool, dict]:
    with db.connect() as conn:
        return conn.execute(
            "SELECT resolved, detail FROM review_queue WHERE id = %s", (qid,)
        ).fetchone()


@needs_db
@pytest.mark.parametrize("old_hash", [None, E1], ids=["untracked-row", "other-epoch-row"])
def test_an_open_row_from_another_epoch_closes_and_its_anchor_is_not_inherited(kb, old_hash):
    stale = {"baseline_hit_at_5": 0.9836, "baseline_mrr": 0.8607}  # an echo night's anchor
    if old_hash:
        stale["golden_hash"] = old_hash
    qid = review_mod.queue_review(None, eval_regression.REVIEW_REASON, stale)
    _ok(eval_hit_at_5=0.98, eval_mrr=0.92)  # pre-E2: untracked
    tonight = {"eval_regression": False, "eval_queries": 61, "eval_golden_hash": E2}
    eval_sentinel._detect_regression(tonight, 0.9508, 0.8678)  # under the stale anchor
    assert tonight["eval_regression_epoch_closed"] == qid
    resolved, detail = _row_detail(qid)
    assert resolved is True and detail["resolution"] == "epoch changed"
    assert datetime.fromisoformat(detail["resolved_at"]).tzinfo is not None, "UTC stamp"
    assert detail["baseline_hit_at_5"] == 0.9836, "a flip + stamp: the old detail is kept"
    assert tonight["eval_regression"] is False, "no same-epoch prior: the floors alone gate"
    assert review_mod.open_review_by_reason(eval_regression.REVIEW_REASON) is None
    # A real trip tonight mints a NEW row anchored on THIS epoch (max(prior, floor)).
    red = {"eval_regression": False, "eval_queries": 61, "eval_golden_hash": E2}
    eval_sentinel._detect_regression(red, 0.70, 0.45)
    new_qid, new = review_mod.open_review_by_reason(eval_regression.REVIEW_REASON)
    assert new_qid != qid and new["golden_hash"] == E2
    assert (new["baseline_hit_at_5"], new["baseline_mrr"]) == (0.75, 0.5), "not 0.9836/0.8607"
    assert new["cause"] == ["floor_hit_at_5", "floor_mrr"] and "persistent_cases" not in new
    assert "eval_regression_epoch_closed" not in red, "a same-epoch row is never closed"


@needs_db
def test_the_epoch_close_runs_first_even_on_a_fallback_night(kb):
    qid = review_mod.queue_review(None, eval_regression.REVIEW_REASON, {"baseline_mrr": 0.9})
    stalled = {
        "eval_regression": False,
        "eval_golden_hash": E2,
        "eval_rerank_tally": {"active": 40, "fallback": 21, "off": 0},
    }
    eval_sentinel._detect_regression(stalled, 0.60, 0.40)
    assert stalled["eval_regression_epoch_closed"] == qid and _row_detail(qid)[0] is True
    assert stalled["eval_regression_measurement"]["cause"] == ["floor_hit_at_5", "floor_mrr"]
    assert review_mod.open_review_by_reason(eval_regression.REVIEW_REASON) is None
    # A hash-less run is not an epoch: it never closes a row (and 0.95 < 0.99 keeps
    # the ordinary auto-resolve out of it too).
    anchor = {"baseline_hit_at_5": 0.99, "baseline_mrr": 0.99}
    keep = review_mod.queue_review(None, eval_regression.REVIEW_REASON, anchor)
    untracked: dict = {"eval_regression": False}
    eval_sentinel._detect_regression(untracked, 0.95, 0.95)
    assert _row_detail(keep)[0] is False and "eval_regression_epoch_closed" not in untracked


# --- 10. review #10 fix round: identity riders, the close loop, the digest cap ---------


def test_the_fingerprint_canonical_form_is_pinned(monkeypatch):
    """TA-7: rounded to 4 decimals, -0.0 folded into 0.0, comma-joined, sha256[:12]."""
    monkeypatch.setattr(embed, "embed_query", lambda text: [0.123449, -0.00001, 0.5])
    canon = b"0.1234,0.0000,0.5000"  # -0.00001 rounds to -0.0, which must read 0.0000
    assert hashlib.sha256(canon).hexdigest()[:12] == "6e119499b7c8"
    assert embed.query_fingerprint() == "6e119499b7c8"
    monkeypatch.setattr(embed, "embed_query", lambda text: [0.12344, 0.00001, 0.50004])
    assert embed.query_fingerprint() == "6e119499b7c8", "sub-grid noise: the same value"


@needs_db
def test_the_sentinel_records_the_serving_host_end_to_end_without_credentials(kb, monkeypatch):
    """TA-3: the real embed path through the harness, the sentinel and the report body."""
    item = ingest("hw-lab", "note", "Widget calibration", "widget calibration: median of three")
    _hosts(monkeypatch, set())  # the credentialed REMOTE serves every query
    golden = [{"q": "widget calibration", "expect_item_id": item}]
    monkeypatch.setattr(eval_sentinel.eval_harness, "load_golden", lambda golden_path=None: golden)
    monkeypatch.setattr(eval_sentinel, "TRUST_METRIC_ENABLED", False)
    try:
        counts: dict = {}
        eval_sentinel.run_sentinel(counts)
    finally:
        embed.reset_host_health()
    public = "http://10.0.0.254:11434"
    assert counts["eval_status"] == "ok" and counts["eval_embed_host"] == public
    assert counts["eval_embed_fingerprint_host"] == public, "the probe's own host"
    assert counts["eval_embed_hosts_key"] == f"{public},{LOCAL}"
    assert counts["eval_embed_model"] == settings.kb_embed_model
    assert "eval_embed_runtime" not in counts, "conftest refuses the identity GETs: fail-open"
    report = get_item(run_report._write_report(dict(counts)))
    assert report.meta["eval_embed_host"] == public, "the owner reads the identity in meta"
    assert report.meta["eval_embed_hosts_key"] == f"{public},{LOCAL}"
    # FB-1: the identity's words and hosts are search vocabulary — never body lines.
    assert "eval_embed" not in report.body and "10.0.0.254" not in report.body
    assert "s3cret" not in report.body and "kbuser" not in report.body, "no userinfo, ever"
    assert "s3cret" not in json.dumps(report.meta), "no userinfo in meta either"


def test_the_key_inputs_are_what_the_epoch_key_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kb_embed_url", REMOTE)
    monkeypatch.setattr(settings, "ollama_url", LOCAL)
    inputs = eval_epoch.key_inputs()
    assert inputs == {
        "eval_embed_model": settings.kb_embed_model,
        "eval_embed_hosts_key": "http://10.0.0.254:11434," + LOCAL,
    }
    golden = _golden(tmp_path)
    model, hosts = inputs["eval_embed_model"], inputs["eval_embed_hosts_key"]
    parts = (golden.read_bytes(), model.encode(), hosts.encode())  # the key's own order
    assert eval_epoch.golden_hash(golden) == hashlib.sha256(b"\x00".join(parts)).hexdigest()[:12]


_TAGS = {
    "models": [
        {"name": "MODEL-large:latest", "digest": "e" * 64},  # TA2-4: a same-PREFIX decoy first
        {"name": "other-model:latest", "digest": "f" * 64},
        {"name": "MODEL:latest", "digest": "790764642607" + "a" * 52},
    ]
}


def _identity_server(monkeypatch, *, version: object = "0.40.11", tags: dict | None = None):
    """Restore the REAL _get (conftest refuses it) over a fake httpx.get; returns its log."""
    seen: list[tuple[str, object]] = []
    monkeypatch.setattr(embed_identity, "_get", _REAL_IDENTITY_GET)
    body = {"version": version}
    listing = tags if tags is not None else _TAGS

    def fake_get(url, *, timeout=None):
        seen.append((url, timeout))
        payload = body if url.endswith("/api/version") else listing
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    return seen


def test_the_runtime_names_the_server_build_and_model_digest(monkeypatch):
    monkeypatch.setattr(settings, "kb_embed_url", REMOTE)
    monkeypatch.setattr(settings, "ollama_url", LOCAL)
    monkeypatch.setattr(settings, "kb_embed_model", "MODEL")
    seen = _identity_server(monkeypatch)
    got = embed_identity.runtime("http://10.0.0.254:11434")
    assert got == "ollama 0.40.11 MODEL:latest@790764642607", "model + ':latest', 12 hex"
    assert [u for u, _t in seen] == [REMOTE + "/api/version", REMOTE + "/api/tags"], (
        "the CONFIGURED (credentialed) URL whose public form served the probe"
    )
    for _url, timeout in seen:
        assert isinstance(timeout, httpx.Timeout)
        assert max(timeout.connect, timeout.read, timeout.write, timeout.pool) <= 3.0
    monkeypatch.setattr(settings, "kb_embed_model", "MODEL:latest")  # an exact name matches too
    assert embed_identity.runtime(LOCAL) == "ollama 0.40.11 MODEL:latest@790764642607"


@pytest.mark.parametrize(
    "server",
    [{"version": 3}, {"tags": {"models": [{"name": "other", "digest": "f" * 64}]}}],
    ids=["bad-version", "model-not-listed"],
)
def test_the_runtime_fails_open_with_one_warning(monkeypatch, caplog, server):
    monkeypatch.setattr(settings, "kb_embed_model", "MODEL")
    _identity_server(monkeypatch, **server)
    with caplog.at_level(logging.WARNING, logger="kbcore.embed_identity"):
        assert embed_identity.runtime(embed.public_host(settings.embed_url)) is None
        assert embed_identity.runtime("http://not-configured:1") is None
    assert len(caplog.records) == 2 and all(r.levelno == logging.WARNING for r in caplog.records)


def test_replay_keys_carry_the_probe_host_and_its_runtime(monkeypatch):
    monkeypatch.setattr(settings, "kb_embed_model", "MODEL")
    dead = {REMOTE}
    _hosts(monkeypatch, dead)  # the probe falls back to LOCAL — the replay's host was REMOTE
    _identity_server(monkeypatch)
    try:
        r = eval_harness.CaseResult("a", 1, True, top_ids=[7], embed_host="http://10.0.0.254:11434")
        keys = eval_cases.replay_keys([r])
    finally:
        embed.reset_host_health()
    assert keys["eval_embed_host"] == "http://10.0.0.254:11434"
    assert keys["eval_embed_fingerprint_host"] == LOCAL, "the host that served THE PROBE"
    assert keys["eval_embed_runtime"] == "ollama 0.40.11 MODEL:latest@790764642607"
    assert len(keys["eval_embed_fingerprint"]) == 12


@needs_db
def test_a_close_night_that_also_trips_opens_a_new_row(kb, caplog):
    """F6 (i) / TA-1: E2 night 1 — the stale row closes AND tonight trips, in ONE call."""
    stale = {"baseline_hit_at_5": 0.9836, "baseline_mrr": 0.8607, "golden_hash": E1}
    qid = review_mod.queue_review(None, eval_regression.REVIEW_REASON, stale)
    tonight = {"eval_regression": False, "eval_golden_hash": E2}
    with caplog.at_level(logging.WARNING, logger="kbworker.eval_sentinel"):
        eval_sentinel._detect_regression(tonight, 0.70, 0.45)
    assert tonight["eval_regression_epoch_closed"] == qid and tonight["eval_regression"] is True
    assert "eval_regression_epoch_closed_count" not in tonight, "one row closed"
    new_qid, new = review_mod.open_review_by_reason(eval_regression.REVIEW_REASON)
    assert new_qid != qid and new["golden_hash"] == E2
    assert (new["baseline_hit_at_5"], new["baseline_mrr"]) == (0.75, 0.5), "max(prior, floor)"
    assert "recovery anchor 0.750/0.500 for a new row" in caplog.text
    assert "0.984" not in caplog.text and "0.861" not in caplog.text, "never the stale anchor"
    closing = [r for r in caplog.records if "resolved 'epoch changed'" in r.getMessage()]
    assert len(closing) == 1 and closing[0].levelno == logging.WARNING
    assert E1 in closing[0].getMessage() and E2 in closing[0].getMessage(), "both hashes named"


@needs_db
def test_the_close_loop_closes_every_other_epoch_row_and_keeps_tonights(kb):
    """F5 (invariants F3 inverted): the oldest-row read no longer hides a newer row."""
    reason = eval_regression.REVIEW_REASON
    untracked = review_mod.queue_review(None, reason, {"baseline_mrr": 0.9})
    other = review_mod.queue_review(None, reason, {"baseline_mrr": 0.9, "golden_hash": E1})
    mine = review_mod.queue_review(
        None, reason, {"baseline_hit_at_5": 0.9, "baseline_mrr": 0.9, "golden_hash": E2}
    )
    counts = {"eval_regression": False, "eval_golden_hash": E2}
    eval_sentinel._detect_regression(counts, 0.10, 0.10)  # under both floors: a trip
    assert counts["eval_regression_epoch_closed"] == untracked, "the FIRST closed qid"
    assert counts["eval_regression_epoch_closed_count"] == 2
    assert _row_detail(untracked)[0] is True and _row_detail(other)[0] is True
    assert _open_ids() == [mine], "tonight's row is the night's open row: no duplicate minted"
    assert _row_detail(mine)[1]["baseline_hit_at_5"] == 0.9, "dedup: never rewritten"


@needs_db
def test_the_close_loop_is_bounded(kb, monkeypatch, caplog):
    reason = eval_regression.REVIEW_REASON
    monkeypatch.setattr(eval_detector_io, "EPOCH_CLOSE_MAX", 2)
    rows = [review_mod.queue_review(None, reason, {"golden_hash": E1}) for _ in range(3)]
    counts = {"eval_regression": False, "eval_golden_hash": E2}
    with caplog.at_level(logging.WARNING, logger="kbworker.eval_sentinel"):
        eval_sentinel._detect_regression(counts, 0.10, 0.10)
    assert counts["eval_regression_epoch_closed_count"] == 2
    assert rows[2] in _open_ids() and "waits" in caplog.text, "the rest close on later nights"
    new_qid, new = review_mod.open_review_by_reason(reason)
    assert new_qid == rows[2], "the oldest open row is still the other-epoch one"
    assert counts["eval_regression"] is True and len(_open_ids()) == 2, "a fresh row, own anchor"


def _open_ids() -> list[int]:
    with db.connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT id FROM review_queue WHERE resolved = false AND reason = %s ORDER BY id",
                (eval_regression.REVIEW_REASON,),
            ).fetchall()
        ]


def test_the_case_lines_are_capped_so_other_alerts_survive_the_digest_cap():
    """F3 (R3 inverted): 24 persistent cases no longer crowd EXPIRY/drain/swap out."""
    counts = {
        "eval_regression": True,
        "eval_hit_at_5": 0.6,
        "eval_mrr": 0.5,
        "eval_regression_cause": ["floor_hit_at_5", "persistent"],
        "eval_case_persistent": [
            {"case": i, "satisfier": 100 + i, "since": "2026-11-02", "nights": 3}
            for i in range(1, 25)
        ],
        "untriaged_expiring_7d": 40,
        "swap_used_pct": 95.0,
        "deferral_escalations": 3,
    }
    lines = digest.digest_lines(counts)
    case_lines = [ln for ln in lines if ln.startswith("EVAL CASE")]
    assert case_lines == [
        *(f"EVAL CASE #{i} ({100 + i}): red 3 nights since 2026-11-02" for i in range(1, 6)),
        "EVAL CASE +19 more",
    ], "golden order, at most 5, then one summary line"
    for prefix in ("EXPIRY", "drain:", "swap:"):
        assert any(ln.startswith(prefix) for ln in lines), prefix
    assert (
        len(
            eval_epoch_lines.eval_case_lines(
                {"eval_case_persistent": counts["eval_case_persistent"][:5]}
            )
        )
        == 5
    )


def test_the_first_pass_notices_are_capped_so_later_facts_survive_the_digest_cap():
    """D2 (fix-round review): a mass-recovery night's first-pass NOTICES are capped like
    the case alerts — at most 5 in golden order, then ONE '+N more' notice — so the
    facts rendered after them (here the rerank sidecar's failure) still reach the push."""
    counts = {
        "eval_regression": False,
        "queued_drained": 5,
        "queued_items_saved": 4,
        "review_triaged": 3,
        "auto_applied": 2,
        "eval_case_first_pass": [{"case": i, "satisfier": 100 + i} for i in range(1, 25)],
        "rerank_probe": "failed",
    }
    lines = digest.digest_lines(counts)
    notices = [ln for ln in lines if ln.startswith("eval case")]
    assert notices == [
        *(f"eval case #{i} ({100 + i}): first pass after 3+ red nights" for i in range(1, 6)),
        "eval case +19 more first passes",
    ], "golden order, at most 5, then one summary notice"
    assert "rerank probe: failed" in lines, "the fact after the notices survives"
    five = {"eval_case_first_pass": counts["eval_case_first_pass"][:5]}
    assert len(eval_epoch_lines.first_pass_lines(five)) == 5, "no summary line at the cap"


def test_the_runtime_reads_the_host_that_served_never_the_first_configured(monkeypatch):
    """H3 (ML; harness probe P8): the identity GETs go to the configured URL whose PUBLIC
    form served the probe — a fallback host's build and digest, never the primary's."""
    monkeypatch.setattr(settings, "kb_embed_url", REMOTE)
    monkeypatch.setattr(settings, "ollama_url", LOCAL)
    monkeypatch.setattr(settings, "kb_embed_model", "MODEL")
    monkeypatch.setattr(embed_identity, "_get", _REAL_IDENTITY_GET)
    seen: list[str] = []

    def fake_get(url, *, timeout=None):  # the two hosts answer DIFFERENTLY
        seen.append(url)
        remote = url.startswith(REMOTE)
        payload = (
            {"version": "9.9.9" if remote else "0.40.11"}
            if url.endswith("/api/version")
            else {"models": [{"name": "MODEL:latest", "digest": ("ab" if remote else "cd") * 32}]}
        )
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    assert embed_identity.runtime(LOCAL) == "ollama 0.40.11 MODEL:latest@cdcdcdcdcdcd"
    assert seen == [LOCAL + "/api/version", LOCAL + "/api/tags"]
    seen.clear()
    primary = embed_identity.runtime("http://10.0.0.254:11434")
    assert primary == "ollama 9.9.9 MODEL:latest@abababababab"
    assert seen == [REMOTE + "/api/version", REMOTE + "/api/tags"]


def test_server_reported_fields_are_sanitised_capped_and_hex_checked(monkeypatch):
    """H3 (MF) / TA2-8 (B30, B32): what the server reports lands in report meta and log
    lines — printable ASCII without spaces, at most 40 chars; a non-hex digest is none."""
    monkeypatch.setattr(settings, "kb_embed_model", "MODEL")
    host = embed.public_host(settings.embed_url)
    _identity_server(monkeypatch, version="9" * 100)
    assert embed_identity.runtime(host) == f"ollama {'9' * 40} MODEL:latest@790764642607"
    _identity_server(monkeypatch, version=" 1.2 beta\x07\n")
    assert embed_identity.runtime(host) == "ollama 1.2beta MODEL:latest@790764642607"
    _identity_server(monkeypatch, tags={"models": [{"name": "MODEL", "digest": "zz" * 32}]})
    assert embed_identity.runtime(host) is None, "fail-open: a non-hex digest is no digest"


def test_the_probe_records_the_embed_config_the_epoch_key_never_sees(monkeypatch, tmp_path):
    """H4 (harness probe P10): a KB_EMBED_QUERY_PREFIX edit moves the fingerprint while the
    epoch key and the runtime stay put, so the probe records the prefix's sha (never its
    text) and the configured device beside the fingerprint — a reload reads as the
    fingerprint moving with ALL of them unchanged."""
    golden = _golden(tmp_path)
    key = eval_epoch.golden_hash(golden)
    monkeypatch.setattr(settings, "kb_embed_query_prefix", "")
    before = eval_cases._fingerprint()
    prefix = "Instruct: retrieve the notes\nQuery: "
    monkeypatch.setattr(settings, "kb_embed_query_prefix", prefix)
    after = eval_cases._fingerprint()
    assert after["eval_embed_fingerprint"] != before["eval_embed_fingerprint"]
    assert eval_epoch.golden_hash(golden) == key, "no new epoch: the key never sees the prefix"
    sha = hashlib.sha256(prefix.encode()).hexdigest()[:12]
    assert (after["eval_embed_query_prefix_sha"], before["eval_embed_query_prefix_sha"]) == (
        sha,
        hashlib.sha256(b"").hexdigest()[:12],
    )
    assert after["eval_embed_device"] == settings.kb_embed_device
    assert prefix not in str(after), "the sha, never the instruction text"

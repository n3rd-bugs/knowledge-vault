"""kbworker.contradiction_blanks — the no-verdict rest store (KB 55589). Pure: no DB, no Ollama.

conftest's autouse ``_isolated_state_dir`` points KB_STATE_DIR at a per-test tmp dir, so
nothing here can touch the live ~/kb/state.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from kbcore.config import settings
from kbworker import contradiction_blanks as cb


@pytest.fixture(autouse=True)
def _seat(monkeypatch):
    monkeypatch.setattr(settings, "kb_contradiction_judge_model", "seat:27b")
    monkeypatch.setattr(settings, "kb_embed_model", "embed:1")


def _days_ago(n: float) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).isoformat()


def test_a_blank_rests_for_retry_days_then_is_due_and_an_answer_clears_it():
    stamps = cb.load()
    assert stamps == {} and not cb.known(stamps, 2, 1)
    cb.record(stamps, 2, 1)  # either order: the key is (lower, higher)
    assert cb.known(stamps, 1, 2) and cb.resting(stamps, 1, 2)
    later = datetime.now(UTC) + timedelta(days=cb.RETRY_DAYS, minutes=1)
    assert not cb.resting(stamps, 1, 2, now=later), "due again after its rest"
    cb.record(stamps, 1, 2)
    assert cb.load()["1\t2"]["nights"] == 2
    cb.clear(stamps, 1, 2)
    assert cb.load() == {}


def test_a_new_seat_or_embedder_makes_every_pair_fresh(monkeypatch):
    stamps = cb.load()
    cb.record(stamps, 1, 2)
    monkeypatch.setattr(settings, "kb_contradiction_judge_model", "other:9b")
    assert not cb.known(stamps, 1, 2) and not cb.resting(stamps, 1, 2)
    monkeypatch.setattr(settings, "kb_contradiction_judge_model", "seat:27b")
    monkeypatch.setattr(settings, "kb_embed_model", "embed:2")
    assert not cb.known(stamps, 1, 2)


def test_stale_and_foreign_stamps_are_pruned_on_the_next_write():
    stamps = {
        "1\t2": {"at": _days_ago(1), "nights": 1, "embed_model": "embed:1", "seat": "seat:27b"},
        "3\t4": {
            "at": _days_ago(cb.PRUNE_DAYS + 1),
            "nights": 9,
            "embed_model": "embed:1",
            "seat": "seat:27b",
        },  # older than PRUNE_DAYS: no retry has refreshed it, the pair is gone
        "5\t6": {"at": _days_ago(1), "nights": 1, "embed_model": "embed:0", "seat": "seat:27b"},
        "7\t8": "not a stamp",
    }
    cb.record(stamps, 9, 10)
    assert set(cb.load()) == {"1\t2", "9\t10"}


@pytest.mark.parametrize(
    "at", ["2026-10-27T09:00:00", "not a date", None, 12345]
)  # naive, garbage, missing, wrong type
def test_an_unreadable_timestamp_never_crashes_and_never_rests(at):
    stamps = {"1\t2": {"at": at, "nights": "x", "embed_model": "embed:1", "seat": "seat:27b"}}
    assert cb.known(stamps, 1, 2)
    assert not cb.resting(stamps, 1, 2)
    cb.record(stamps, 1, 2)  # a garbage "nights" restarts the count
    assert cb.load()["1\t2"]["nights"] == 1


def test_a_corrupt_or_unwritable_marker_fails_open(caplog):
    cb.stamp_path().parent.mkdir(parents=True, exist_ok=True)
    cb.stamp_path().write_text("{not json", encoding="utf-8")
    assert cb.load() == {}, "corrupt: every pair is fresh"
    cb.stamp_path().write_text(json.dumps(["a", "list"]), encoding="utf-8")
    assert cb.load() == {}
    cb.stamp_path().unlink()
    cb.stamp_path().mkdir()  # a directory where the file should be: the write fails
    cb.record({}, 1, 2)
    assert "not writable" in caplog.text


def test_select_puts_fresh_pairs_first_caps_retries_and_counts_the_resting():
    stamps: cb.Stamps = {}
    rows = [(i, i + 1, 0.9) for i in range(0, 40, 2)]  # 20 candidate pairs, sim order
    for a, b, _s in rows[:8]:  # the 8 most similar blanked before ...
        cb.record(stamps, a, b)
    for key in list(stamps)[:6]:  # ... 6 of them a while ago: due again
        stamps[key]["at"] = _days_ago(cb.RETRY_DAYS + 1)
    chosen, resting = cb.select(stamps, rows, cap=None)
    assert resting == 2
    assert chosen[:12] == rows[8:], "every fresh pair first, in the query's order"
    assert chosen[12:] == rows[: cb.RETRIES_PER_RUN], "then a few due retries"
    capped, _r = cb.select(stamps, rows, cap=10)
    assert capped == rows[8:18], "the cap goes to fresh pairs before any retry"


def test_a_future_timestamp_or_an_absurd_count_never_parks_or_crashes():
    stamps = {
        "1\t2": {
            "at": (datetime.now(UTC) + timedelta(days=365)).isoformat(),
            "nights": 1e999,
            "embed_model": "embed:1",
            "seat": "seat:27b",
        },
    }
    assert not cb.resting(stamps, 1, 2), "a clock that ran fast must not park a pair"
    cb.record(stamps, 1, 2)  # nights=inf: OverflowError in int() — restarts the count
    assert cb.load()["1\t2"]["nights"] == 1

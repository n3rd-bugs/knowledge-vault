"""PER-WINDOW EXTRACTION CHECKPOINTS: a multi-window group resumes where it
stopped instead of re-extracting from window 1 every night.

THE WASTE (measured 2026-08-10). A coalesced group is extracted one
context-sized window at a time and its rows flip only when the LAST window lands.
That flip is all-or-nothing on purpose — a session must never half-land in the
queue's status accounting — but it also meant the EXTRACTION CALLS had no memory.
Three giant sessions (aquamonix 269 rows/116 MB at deferrals=6, ims 59/52 MB,
weird-rtos 10/8 MB) spent ~8 GPU-hours a night re-extracting windows whose items
were already stored (they dedupe to nothing on content_hash), then hit the same
failing window and deferred the whole group again. One night's report: 7.9 h of
drain, 0 rows drained, 0 items saved. The same restart waste hits any giant
interrupted by the tail reserve or the deadline.

WHAT CHANGED, AND WHAT DID NOT. Row flips are untouched: still group-atomic,
still one attempt/deferral per local day, same failure taxonomy. Only the extraction
calls remember — a window whose ``extract_and_ingest`` RETURNED has its items
durably stored (Invariant 3), so re-running it is pure waste, and the checkpoint
records which those were, keyed to a fingerprint of the assembled text.

These tests pin: the checkpoint is persisted per window; the next pass extracts
ONLY the outstanding windows; the totals booked by ``mark_group_extracted`` are
the SUM across every pass (the ``saved_count`` truthfulness this repo already has
history with — see tests/test_saved_count.py); a fingerprint that drifts discards
the checkpoint; the kill switch restores the old behaviour exactly; the deferral
counter still climbs; completion and ``requeue_extracted`` leave no residue; and
a single-window group is never checkpointed at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kbcore import ingest_queue
from kbcore.config import settings
from kbcore.db import connect, transaction
from kbcore.errors import ExtractionFailed, ExtractorUnavailable
from kbworker import consolidate, prep, sessions

# Four windows, deterministic and independent of num_ctx: the SPLIT is pinned by
# tests/test_drain_windows.py, and this suite is about what the drain remembers
# ACROSS runs. Slicing the assembled text keeps the fingerprint honest — it is
# taken over that same text, so any change to the group's rows moves it.
WINDOWS = 4


@pytest.fixture(autouse=True)
def _four_windows(monkeypatch):
    def split(text: str) -> list[str]:
        n = max(1, len(text) // WINDOWS)
        return [text[i * n : (i + 1) * n] for i in range(WINDOWS - 1)] + [text[(WINDOWS - 1) * n :]]

    monkeypatch.setattr(prep, "_fit_windows", split)


@pytest.fixture(autouse=True)
def _no_stand_down(monkeypatch):
    """Disarm the wall-clock/power guard: a suite run after the noon deadline
    would otherwise stop these drains at a cohort boundary."""
    monkeypatch.setattr(settings, "kb_drain_deadline", "")
    monkeypatch.setattr(settings, "kb_drain_deadline_days", "")
    monkeypatch.setattr(settings, "kb_power_db", "")
    consolidate.reset_stop_cache()
    consolidate._STOP_LOG.clear()
    yield
    consolidate.reset_stop_cache()
    consolidate._STOP_LOG.clear()


@pytest.fixture(autouse=True)
def _two_nights_in_one_day(monkeypatch):
    """These tests compress 'tomorrow night' into a second ``_drive`` seconds
    later, which the per-DAY attempt budget (review #4) would otherwise skip: a
    content-failed row is not re-offered until the civil day turns. Pin the
    boundary to the future so every drive counts as a fresh day; the budget
    itself is pinned in tests/test_drain_refill.py."""
    monkeypatch.setattr(
        ingest_queue, "drain_day_start", lambda: datetime.now(UTC) + timedelta(days=1)
    )


def _enqueue(project: str, body: str, source_uri: str | None = None) -> int:
    res = ingest_queue.enqueue(project, body, kind="session", source_uri=source_uri)
    assert res.id is not None, "setup failed to queue the row"
    return res.id


def _age(ids: list[int], minutes: int = 120) -> None:
    """Backdate rows past the coalescing quiet window (a group of 2+ rows is only
    returned once it has been quiet, so a fingerprint-drift test needs this)."""
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET created_at = now() - make_interval(mins => %s) "
            "WHERE id = ANY(%s)",
            (minutes, list(ids)),
        )


def _row(qid: int) -> tuple[str, int, int, int, int]:
    with connect() as conn:
        return conn.execute(
            "SELECT status, attempts, deferrals, coalesce(saved_count, 0), "
            "coalesce(rejected_count, 0) FROM ingest_queue WHERE id = %s",
            (qid,),
        ).fetchone()


def _checkpoint(qid: int) -> dict | None:
    with connect() as conn:
        meta = conn.execute("SELECT meta FROM ingest_queue WHERE id = %s", (qid,)).fetchone()[0]
    return ingest_queue.window_checkpoint(meta or {})


class _Extractor:
    """A call-recording ``extract_and_ingest``: records the text of every window
    it is asked to extract, yields a per-window (saved, rejected), and can raise
    for one window index. The recording IS the assertion — the whole feature is
    about which extraction calls do NOT happen."""

    def __init__(self, yields: dict[int, tuple[int, int]], fail_at: int | None = None, exc=None):
        self.seen: list[str] = []
        self.yields = yields
        self.fail_at = fail_at
        self.exc = exc

    def __call__(self, text, project, uri, **kw):
        idx = kw["extra_detail"].get("window", 0)
        self.seen.append(text)
        if self.fail_at is not None and idx == self.fail_at:
            raise self.exc
        return self.yields.get(idx, (1, 0))


def _drive(monkeypatch, extractor: _Extractor) -> tuple[int, int]:
    monkeypatch.setattr(sessions, "extract_and_ingest", extractor)
    return consolidate._drain_ingest_queue()


def _big(project: str, uri: str | None = None) -> tuple[int, list[str]]:
    """One queued row whose assembled text splits into :data:`WINDOWS` windows,
    plus the window texts in order (so a recorded call can be named)."""
    body = "".join(f"User: q{i}\nAssistant: a{i}\n" for i in range(120))
    qid = _enqueue(project, body, source_uri=uri)
    return qid, prep._fit_windows(body.strip())


# --------------------------------------------------------------- resume


def test_a_group_that_fails_at_window_2_banks_the_windows_that_landed(kb, monkeypatch):
    """The headline: an interrupted group writes down what it finished."""
    qid, wins = _big("cp-bank")
    ext = _Extractor({}, fail_at=2, exc=ExtractionFailed("unparseable model output"))
    _drive(monkeypatch, ext)

    assert ext.seen == wins[:3], "the drain did not stop at the failing window"
    cp = _checkpoint(qid)
    assert cp is not None, "an interrupted group left no checkpoint — the whole feature"
    assert cp["done"] == [0, 1], f"the banked windows are wrong: {cp}"
    assert cp["fp"] == ingest_queue.window_fingerprint(wins)
    assert _row(qid)[0] == "failed", "the failure taxonomy changed"


def test_a_window_geometry_change_discards_the_checkpoint_wholesale(kb, monkeypatch):
    """The reviewed-out CRITICAL: hashing the pre-split TEXT kept the fingerprint
    valid across a num_ctx change that MOVED every window boundary — stale done
    indices then skipped content never extracted on any night, and the group
    flipped 'extracted' with knowledge silently missing. The fingerprint now
    covers the windows themselves: same text, different split => full re-extract."""
    qid, wins = _big("cp-geometry")
    first = _Extractor({}, fail_at=2, exc=ExtractionFailed("unparseable model output"))
    _drive(monkeypatch, first)
    assert _checkpoint(qid)["done"] == [0, 1]

    # Same text, DIFFERENT geometry — as a num_ctx change would produce: re-split
    # the assembled text into half as many, twice-as-large windows.
    real_fit = prep._fit_windows

    def _coarser(text: str) -> list[str]:
        fine = real_fit(text)
        return ["".join(fine[i : i + 2]) for i in range(0, len(fine), 2)]

    monkeypatch.setattr(prep, "_fit_windows", _coarser)
    second = _Extractor({})
    drained, _ = _drive(monkeypatch, second)

    # The only assertion that matters: EVERY new-geometry window was extracted;
    # nothing was skipped on the strength of the stale checkpoint.
    assert len(second.seen) >= 2, "the coarser split produced too few windows to prove anything"
    assert (drained, _row(qid)[0]) == (1, "extracted")
    assert consolidate._COHORT_STATS.get("cohort_windows_resumed", 0) == 0, (
        "a stale-geometry checkpoint was honored — the exact reviewed-out knowledge-loss bug"
    )


def test_one_invalid_done_index_discards_the_whole_checkpoint(kb):
    """Filtering bad indices was the dodge: a stale checkpoint's in-range subset
    could slip past the range guard. One invalid entry now voids everything."""
    fp = ingest_queue.window_fingerprint(["a", "b", "c", "d"])
    meta = {"extract_checkpoint": {"fp": fp, "done": [0, 7], "saved": 1, "rejected": 0}}
    assert ingest_queue.resume_windows(meta, fp, 4) == (set(), 0, 0)
    meta["extract_checkpoint"]["done"] = [0, True]  # bool masquerading as int
    assert ingest_queue.resume_windows(meta, fp, 4) == (set(), 0, 0)
    meta["extract_checkpoint"]["done"] = [0, 0, 1]  # duplicate entries
    assert ingest_queue.resume_windows(meta, fp, 4) == (set(), 0, 0)
    meta["extract_checkpoint"]["done"] = [0, 1]  # the honest shape still resumes
    assert ingest_queue.resume_windows(meta, fp, 4) == ({0, 1}, 1, 0)


def test_the_next_pass_extracts_ONLY_the_outstanding_windows(kb, monkeypatch):
    """~8 GPU-hours a night was windows 0..n-1 being re-mined into duplicates
    that dedupe to nothing. The second pass must call the extractor for 2 and 3
    and for nothing else."""
    qid, wins = _big("cp-resume")
    first = _Extractor({}, fail_at=2, exc=ExtractionFailed("unparseable model output"))
    _drive(monkeypatch, first)
    assert first.seen == wins[:3]

    second = _Extractor({})
    drained, _saved = _drive(monkeypatch, second)

    assert second.seen == wins[2:], (
        f"the resumed pass re-extracted stored windows: {len(second.seen)} calls, expected 2"
    )
    assert (drained, _row(qid)[0]) == (1, "extracted"), "the resumed group did not finish"
    assert consolidate._COHORT_STATS["cohort_windows_resumed"] == 2, "resume is invisible"
    assert consolidate._COHORT_STATS["cohort_groups_resumed"] == 1


def test_the_totals_are_the_SUM_across_both_passes(kb, monkeypatch):
    """saved_count TRUTHFULNESS across nights (tests/test_saved_count.py owns the
    within-run half). A group finished over two runs must book what it actually
    produced — restoring the checkpointed totals is the only way, because the
    windows that produced them are never re-run."""
    qid, _wins = _big("cp-totals")
    first = _Extractor({0: (2, 1), 1: (3, 0)}, fail_at=2, exc=ExtractionFailed("bad output"))
    _drive(monkeypatch, first)

    second = _Extractor({2: (5, 2), 3: (7, 1)})
    _drained, saved_this_run = _drive(monkeypatch, second)

    status, _a, _d, saved, rejected = _row(qid)
    assert (status, saved, rejected) == ("extracted", 2 + 3 + 5 + 7, 1 + 0 + 2 + 1), (
        "the group booked only the last run's yield"
    )
    assert saved_this_run == 5 + 7, (
        "restored totals leaked into THIS run's queued_items_saved — the run report "
        "would claim credit for items saved on an earlier night"
    )


def test_the_checkpoint_advances_window_by_window_not_once_at_the_end(kb, monkeypatch):
    """Persisting at the end would checkpoint only the groups that never needed
    one. Each landed window must be visible to the NEXT run immediately."""
    qid, _wins = _big("cp-incremental")
    seen_states: list[list[int]] = []

    class _Watching(_Extractor):
        def __call__(self, text, project, uri, **kw):
            cp = _checkpoint(qid)
            seen_states.append(list(cp["done"]) if cp else [])
            return super().__call__(text, project, uri, **kw)

    _drive(monkeypatch, _Watching({}, fail_at=3, exc=ExtractionFailed("bad output")))

    assert seen_states == [[], [0], [0, 1], [0, 1, 2]], (
        f"the checkpoint did not advance window by window: {seen_states}"
    )
    assert _checkpoint(qid)["done"] == [0, 1, 2]


# ------------------------------------------------------ the fingerprint guard


def test_a_group_whose_TEXT_CHANGED_discards_the_checkpoint_and_re_extracts_all(kb, monkeypatch):
    """THE safety property. A late chunk coalesces into the group, the assembled
    text changes, the windows MOVE — so 'window 2' no longer means what it meant
    and every banked index is meaningless. Full re-extraction is the correct
    answer, and Invariant 3 makes it harmless."""
    uri = "session:drifting"
    body = "".join(f"User: q{i}\nAssistant: a{i}\n" for i in range(120))
    first_id = _enqueue("cp-drift", body, uri)
    _age([first_id])
    first = _Extractor({}, fail_at=2, exc=ExtractionFailed("bad output"))
    _drive(monkeypatch, first)
    assert _checkpoint(first_id)["done"] == [0, 1], "setup: no checkpoint to invalidate"

    late = _enqueue("cp-drift", "User: one more thing\nAssistant: noted\n", uri)
    _age([first_id, late])

    second = _Extractor({})
    _drive(monkeypatch, second)

    assert len(second.seen) == WINDOWS, (
        f"a moved window was skipped on a stale checkpoint: {len(second.seen)} calls"
    )
    assert consolidate._COHORT_STATS["cohort_windows_resumed"] == 0, (
        "a stale checkpoint was trusted"
    )
    assert _row(first_id)[0] == "extracted" and _row(late)[0] == "extracted"


# ------------------------------------------------------------- kill switch


def test_the_kill_switch_restores_the_old_full_re_extract_behaviour(kb, monkeypatch):
    """kb_drain_window_checkpoint=False must be today's behaviour exactly: no
    checkpoint written, and the next pass starts again at window 0."""
    monkeypatch.setattr(settings, "kb_drain_window_checkpoint", False)
    qid, wins = _big("cp-off")

    first = _Extractor({}, fail_at=2, exc=ExtractionFailed("bad output"))
    _drive(monkeypatch, first)
    assert first.seen == wins[:3]
    assert _checkpoint(qid) is None, "the kill switch still wrote a checkpoint"

    second = _Extractor({})
    _drive(monkeypatch, second)
    assert second.seen == wins, "the old behaviour is not a full re-extraction"
    assert consolidate._COHORT_STATS["cohort_windows_resumed"] == 0


# ------------------------------------------- failure paths are UNCHANGED


def test_the_deferral_counter_still_climbs_on_the_same_failing_window(kb, monkeypatch):
    """Escalation via DEFERRAL_CAP is untouched — a poisonous window still
    escalates, it just gets there without re-extracting the good windows first.
    One run per LOCAL day here: the burn is bounded per day (review #5 pipe-2),
    so a same-day boot catch-up no longer advances the counter."""
    qid, wins = _big("cp-defer")

    for run in (1, 2, 3):
        monkeypatch.setattr(ingest_queue, "drain_day_start", lambda run=run: datetime(2026, 1, run))
        ext = _Extractor({}, fail_at=2, exc=ExtractorUnavailable("ollama restarting"))
        _drive(monkeypatch, ext)
        status, attempts, deferrals, _s, _r = _row(qid)
        assert (status, attempts, deferrals) == ("pending", 0, run), (
            f"run {run}: an outage must defer once per day and burn no attempt"
        )
        expected = wins[:3] if run == 1 else wins[2:3]
        assert ext.seen == expected, (
            f"run {run}: extracted {len(ext.seen)} window(s), wanted {len(expected)}"
        )


def test_a_deferred_group_resumes_at_the_failing_window_and_can_still_finish(kb, monkeypatch):
    """The outage heals: the group picks up at the window that failed, not at 0."""
    qid, wins = _big("cp-heal")
    _drive(monkeypatch, _Extractor({}, fail_at=1, exc=ExtractorUnavailable("down")))

    healed = _Extractor({})
    _drive(monkeypatch, healed)

    assert healed.seen == wins[1:], "the healed run re-extracted the window that landed"
    assert _row(qid)[0] == "extracted"


def test_a_group_stopped_by_the_DEADLINE_resumes_at_its_next_window(kb, monkeypatch):
    """The other half of the motivation. A giant is interrupted by the tail
    reserve or the wall-clock cutoff far more often than by a bad window: the
    drain stops BETWEEN cohorts, which since windowing can land mid-GROUP, and
    the rows are simply left 'pending'. That path must resume too — otherwise the
    biggest sessions, the ones that cannot finish inside one window, are exactly
    the ones that never make progress."""
    monkeypatch.setattr(settings, "kb_extract_ensemble", "qwen3:14b")
    monkeypatch.setattr(settings, "kb_ensemble_shadow", False)  # live ⇒ real cohorts
    monkeypatch.setattr(settings, "kb_drain_cohort", 2)  # two windows per cohort
    monkeypatch.setattr(consolidate, "_cohort_prepare", lambda batch: ({}, {}))
    qid, wins = _big("cp-deadline")

    first = _Extractor({})
    monkeypatch.setattr(consolidate, "should_stop", lambda: "deadline" if first.seen else None)
    _drive(monkeypatch, first)

    assert first.seen == wins[:2], "the drain did not stop at the cohort boundary"
    assert _row(qid)[0] == "pending", "an interrupted group must stay pending, never lost"
    assert _checkpoint(qid)["done"] == [0, 1], "the landed windows were not banked"

    monkeypatch.setattr(consolidate, "should_stop", lambda: None)
    second = _Extractor({})
    _drive(monkeypatch, second)

    assert second.seen == wins[2:], "the next night restarted the giant from window 0"
    assert _row(qid)[0] == "extracted"


# ----------------------------------------------------------- no residue


def test_a_completed_group_leaves_no_checkpoint_behind(kb, monkeypatch):
    """Cleared in the same transaction as the flip: a finished group must not
    carry resume state that a future re-open could read."""
    qid, _wins = _big("cp-clean")
    _drive(monkeypatch, _Extractor({}))

    assert _row(qid)[0] == "extracted"
    assert _checkpoint(qid) is None, "a completed group left checkpoint residue"


def test_requeue_extracted_clears_the_checkpoint(kb):
    """A group an owner deliberately re-opens starts FRESH. (Completion already
    strips the key, so this pins the belt-and-braces clear for any row that
    reached 'extracted' by another path.)"""
    qid = _enqueue("cp-requeue", "produced nothing at all")
    ingest_queue.save_window_checkpoint(qid, fp="deadbeef", done=[0, 1], saved=0, rejected=0)
    ingest_queue.mark_extracted(qid, saved=0, rejected=0)
    assert _checkpoint(qid) is not None, "setup: no checkpoint to clear"

    assert ingest_queue.requeue_extracted(project="cp-requeue", reason="test") == [qid]
    assert _checkpoint(qid) is None, "a re-opened group would resume past its own re-mining"
    assert _row(qid)[0] == "pending"


def test_the_checkpoint_write_never_touches_the_rest_of_meta(kb):
    """meta carries the grouping key. A checkpoint that clobbered source_uri would
    scatter a session across as many groups as it has rows."""
    qid = _enqueue("cp-meta", "a session row", source_uri="session:keep-me")
    ingest_queue.save_window_checkpoint(qid, fp="abc", done=[0], saved=1, rejected=0)

    with connect() as conn:
        meta = conn.execute("SELECT meta FROM ingest_queue WHERE id = %s", (qid,)).fetchone()[0]
    assert meta["source_uri"] == "session:keep-me", "the checkpoint overwrote the grouping key"
    assert meta["extract_checkpoint"]["done"] == [0]


# --------------------------------------------------- pointless churn guard


def test_a_SINGLE_window_group_is_never_checkpointed(kb, monkeypatch):
    """One UPDATE per drained note, for a group that can never resume, is pure
    churn — and the queue is mostly small notes."""
    calls: list[int] = []
    monkeypatch.setattr(
        ingest_queue,
        "save_window_checkpoint",
        lambda qid, **kw: calls.append(qid),
    )
    monkeypatch.setattr(prep, "_fit_windows", lambda text: [text])  # one window, really
    qid = _enqueue("cp-single", "User: a small note\nAssistant: noted")

    _drive(monkeypatch, _Extractor({}))

    assert calls == [], "a single-window group wrote a checkpoint"
    assert _row(qid)[0] == "extracted"


def test_a_multi_window_group_DOES_reach_the_store(kb, monkeypatch):
    """The other half of the churn guard: the spy above must be able to fail."""
    calls: list[int] = []
    real = ingest_queue.save_window_checkpoint
    monkeypatch.setattr(
        ingest_queue,
        "save_window_checkpoint",
        lambda qid, **kw: (calls.append(qid), real(qid, **kw))[1],
    )
    qid, _wins = _big("cp-multi")

    _drive(monkeypatch, _Extractor({}))

    assert calls == [qid] * (WINDOWS - 1), (
        f"expected one checkpoint per landed window bar the last, got {len(calls)}"
    )


# ------------------------------------------------ the re-arm keeps the memory


def test_an_identical_resubmission_over_a_terminal_row_keeps_the_checkpoint(kb):
    """Review #4 P2: enqueue's ON CONFLICT re-arm set meta = EXCLUDED.meta,
    wiping extract_checkpoint on IDENTICAL resubmission — the one case where the
    fingerprint is MOST likely to still match. queue_repair.requeue_failed
    preserves it for the same reason; the re-arm now merges it forward."""
    body = "".join(f"User: q{i}\nAssistant: a{i}\n" for i in range(120))
    qid = _enqueue("cp-rearm", body)
    with transaction() as conn:  # terminally failed: the re-arm's WHERE clause
        conn.execute(
            "UPDATE ingest_queue SET status = 'failed', attempts = %s WHERE id = %s",
            (ingest_queue.MAX_ATTEMPTS, qid),
        )
    ingest_queue.save_window_checkpoint(qid, fp="f" * 64, done=[0, 1], saved=2, rejected=1)
    banked = _checkpoint(qid)
    assert banked is not None, "setup failed to bank a checkpoint"

    res = ingest_queue.enqueue("cp-rearm", body, kind="session")
    assert (res.id, res.requeued) == (qid, True), "setup did not re-arm the terminal row"

    assert _checkpoint(qid) == banked, "the re-arm merge clobbered the banked checkpoint"
    with connect() as conn:
        meta = conn.execute("SELECT meta FROM ingest_queue WHERE id = %s", (qid,)).fetchone()[0]
    assert meta.get("submitted_by"), "the rest of meta must still be the FRESH submission's"


def test_a_fresh_enqueue_carries_no_checkpoint_residue(kb):
    """The preserve-slice collapses to '{}' when there is nothing to preserve —
    a brand-new row must not grow an extract_checkpoint key."""
    qid = _enqueue("cp-fresh", "User: new\nAssistant: row")
    assert _checkpoint(qid) is None

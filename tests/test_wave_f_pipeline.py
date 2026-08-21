"""Wave F pipeline package: queue_groups/powerguard splits, per-day deferral
burn, prep uri from group_uri, day-aware evidence cap, host telemetry, rerank probe."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from kbcore import ingest_queue, scope
from kbcore.config import settings
from kbworker import consolidate, prep


@pytest.fixture()
def as_owner():
    with scope.use_principal(scope.OWNER):
        yield


# ------------------------------------------------------------- split seams


def test_queue_groups_split_is_invisible_through_ingest_queue():
    """Third application of the documented seam: every moved name resolves to
    the SAME object through ingest_queue, so no caller can see the split."""
    from kbcore import queue_groups

    for name in (
        "COALESCE_QUIET_MINUTES",
        "_DEGENERATE_URI_SQL",
        "_DRAINABLE",
        "group_key",
        "normalise_source_uri",
        "pending",
        "pending_groups",
    ):
        assert getattr(ingest_queue, name) is getattr(queue_groups, name), name


def test_powerguard_split_is_invisible_through_consolidate():
    """The test suite patches consolidate._on_battery; the seam must keep that
    (and should_stop's global lookup) resolving through consolidate."""
    from kbworker import powerguard

    assert consolidate._on_battery is powerguard._on_battery
    assert consolidate._reading_age_seconds is powerguard._reading_age_seconds


# ------------------------------------- pipe-2: one deferral burn per LOCAL day


def _mk(project: str, body: str) -> int:
    r = ingest_queue.enqueue(project, body, "session")
    assert r.id
    return r.id


def _row(qid: int) -> tuple:
    from kbcore.db import connect

    with connect() as c:
        return c.execute(
            "SELECT status, deferrals, error, meta->>'last_deferred_day' "
            "FROM ingest_queue WHERE id=%s",
            (qid,),
        ).fetchone()


def test_two_same_day_deferrals_burn_one(kb, as_owner):
    """DEFERRAL_CAP is reasoned in NIGHTS but burned per RUN, and multi-run local
    days are real (boot catch-up timer; 08-15 ran twice) — a flapping-power
    outage must not escalate a transient group in 3-4 days (review #5 pipe-2)."""
    qid = _mk("proj", "deferred twice by a flapping-power day")
    assert ingest_queue.mark_deferred(qid, "ExtractorUnavailable: 01:00 run") is False
    assert ingest_queue.mark_deferred(qid, "ExtractorUnavailable: boot catch-up") is False

    status, deferrals, error, day = _row(qid)
    assert (status, deferrals) == ("pending", 1), "two same-day defers burned more than one"
    assert "boot catch-up" in error, "the latest error must still be recorded on the skip"
    assert day == ingest_queue.drain_day_start().date().isoformat()


def test_deferrals_still_burn_across_days(kb, as_owner, monkeypatch):
    """Same-day is the only skip: a fresh local day burns again, so the cap keeps
    its multi-night meaning."""
    tick = {"n": 0}

    def _next_day():
        tick["n"] += 1
        return datetime(2026, 1, 1) + timedelta(days=tick["n"])

    monkeypatch.setattr(ingest_queue, "drain_day_start", _next_day)
    qid = _mk("proj", "deferred across two days")
    ingest_queue.mark_deferred(qid, "outage day one")
    ingest_queue.mark_deferred(qid, "outage day two")
    assert _row(qid)[1] == 2


# --------------------------------------- pipe-5: prep job uri from group_uri


def _group(meta_uri: str | None, group_uri: str | None) -> dict:
    g = {
        "uri": "proj::x",
        "project": "proj",
        "source_uri": None,
        "rows": [
            {
                "id": 7,
                "project": "proj",
                "kind": "session",
                "content": "plain prose, no jsonl here",
                "meta": {"source_uri": meta_uri} if meta_uri is not None else {},
            }
        ],
    }
    if group_uri is not None:
        g["group_uri"] = group_uri
    return g


def test_a_stored_degenerate_uri_emits_the_guarded_job_uri():
    """A pre-guard 'session:' row (requeue_extracted can re-open one) is truthy,
    so the old `meta.get('source_uri') or ...` fallback missed it; the job must
    carry pending_groups' guarded value instead (review #5 pipe-5)."""
    jobs = prep._assemble_jobs([_group("session:", "queue:session:7")])
    assert jobs and all(j["uri"] == "queue:session:7" for j in jobs)


def test_a_padded_uri_emits_the_btrimmed_group_uri():
    """group_uri is btrim'd in SQL; the raw meta value is not — the job uri must
    follow the SQL twin so the two paths cannot diverge."""
    jobs = prep._assemble_jobs([_group("  session:abc  ", "session:abc")])
    assert jobs and all(j["uri"] == "session:abc" for j in jobs)


def test_a_hand_built_group_without_group_uri_still_falls_back():
    jobs = prep._assemble_jobs([_group("session:abc", None)])
    assert jobs and all(j["uri"] == "queue:session:7" for j in jobs)


# ------------------------------------- pipe-4: day-aware evidence pair cap


@pytest.fixture()
def _weekday_deadline(monkeypatch):
    """Day-pinned calendar: the noon deadline applies Mon-Fri (the live shape),
    hermetically — never read from the box's .env."""
    monkeypatch.setattr(settings, "kb_drain_deadline", "12:00")
    monkeypatch.setattr(settings, "kb_drain_deadline_days", "mon,tue,wed,thu,fri")


def test_evidence_cap_is_20_on_a_deadline_day(_weekday_deadline):
    mon = datetime(2026, 8, 17)
    assert mon.weekday() == 0  # day-pin guard
    assert consolidate._evidence_pair_cap(mon) == consolidate.EVIDENCE_PAIR_CAP_WEEKDAY == 20


def test_evidence_cap_is_48_on_an_unbounded_day(_weekday_deadline):
    sat = datetime(2026, 8, 22)
    assert sat.weekday() == 5  # day-pin guard
    assert consolidate._evidence_pair_cap(sat) == consolidate.EVIDENCE_PAIR_CAP_UNBOUNDED == 48


def test_an_explicit_cap_override_beats_the_calendar(_weekday_deadline, monkeypatch):
    """EVIDENCE_PAIR_CAP keeps its exact pre-split patch-surface meaning."""
    monkeypatch.setattr(consolidate, "EVIDENCE_PAIR_CAP", 4)
    assert consolidate._evidence_pair_cap(datetime(2026, 8, 22)) == 4
    rows = [(n, n, n, {}) for n in range(10)]
    assert len(consolidate._evidence_batch(rows)) == 4


def test_no_deadline_at_all_means_the_unbounded_cap(monkeypatch):
    monkeypatch.setattr(settings, "kb_drain_deadline", "")
    monkeypatch.setattr(settings, "kb_drain_deadline_days", "")
    assert consolidate._evidence_pair_cap(datetime(2026, 8, 17)) == 48


# --------------------------------------- ops-3: host memory telemetry


def test_host_memory_math(tmp_path):
    fake = tmp_path / "meminfo"
    fake.write_text(
        "MemTotal:       65505072 kB\n"
        "MemAvailable:   45097156 kB\n"
        "SwapTotal:      16777212 kB\n"
        "SwapFree:        2097152 kB\n",
        encoding="ascii",
    )
    swap_pct, mem_gb = consolidate._host_memory(fake)
    assert swap_pct == 87.5
    assert mem_gb == 43.0


def test_host_memory_zero_swap_is_zero_pct_not_a_crash(tmp_path):
    fake = tmp_path / "meminfo"
    fake.write_text("MemAvailable: 1048576 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    assert consolidate._host_memory(fake) == (0.0, 1.0)


def test_host_memory_reads_the_real_procfs():
    swap_pct, mem_gb = consolidate._host_memory()
    assert 0.0 <= swap_pct <= 100.0
    assert mem_gb >= 0.0


# --------------------------------------- ops-4: rerank probe tail step


def test_rerank_probe_records_ok_and_latency(monkeypatch):
    from kbcore import rerank

    monkeypatch.setattr(settings, "kb_rerank_url", "http://127.0.0.1:9")
    monkeypatch.setattr(rerank, "probe", lambda: {"ok": True, "latency_ms": 42}, raising=False)
    counts: dict = {}
    consolidate._rerank_probe_step(counts)
    assert counts == {"rerank_probe": "ok", "rerank_probe_ms": 42}


def test_rerank_probe_records_failed(monkeypatch):
    from kbcore import rerank

    monkeypatch.setattr(settings, "kb_rerank_url", "http://127.0.0.1:9")
    monkeypatch.setattr(rerank, "probe", lambda: {"ok": False, "latency_ms": None}, raising=False)
    counts: dict = {}
    consolidate._rerank_probe_step(counts)
    assert counts["rerank_probe"] == "failed"


def test_rerank_probe_is_fail_open_when_the_helper_raises(monkeypatch):
    """probe() promises fail-open; if it breaks that promise the tail step still
    must not abort the run — 'failed' is the honest attribution."""
    from kbcore import rerank

    def boom():
        raise RuntimeError("probe machinery broken")

    monkeypatch.setattr(settings, "kb_rerank_url", "http://127.0.0.1:9")
    monkeypatch.setattr(rerank, "probe", boom, raising=False)
    counts: dict = {}
    consolidate._rerank_probe_step(counts)
    assert counts == {"rerank_probe": "failed", "rerank_probe_ms": None}


def test_rerank_probe_is_silent_without_a_sidecar_url(monkeypatch):
    """Positive-only: an unconfigured sidecar is not a failure, and the step must
    not dial anything (conftest pins kb_rerank_url='' for the whole suite)."""
    from kbcore import rerank

    monkeypatch.setattr(settings, "kb_rerank_url", "")
    monkeypatch.setattr(
        rerank, "probe", lambda: pytest.fail("probed an unconfigured sidecar"), raising=False
    )
    counts: dict = {}
    consolidate._rerank_probe_step(counts)
    assert counts == {}


def test_rerank_probe_is_silent_until_pkg_search_lands_probe(monkeypatch):
    """The tree must import and run with kbcore.rerank.probe absent (hasattr
    guard) — no keys, so the digest stays positive-only silent."""
    from kbcore import rerank

    monkeypatch.setattr(settings, "kb_rerank_url", "http://127.0.0.1:9")
    monkeypatch.delattr(rerank, "probe", raising=False)
    counts: dict = {}
    consolidate._rerank_probe_step(counts)
    assert counts == {}

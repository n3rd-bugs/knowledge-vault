"""Ops health: the reaper's heartbeat, the fleet staleness signal, the notifier.

Three things that were all the same class of bug — a health signal that is not
actually wired to the health it reports on:

* the stale-run reaper reads ``ingest_queue.processed_at``, but re-windowing
  (2026-08-01) moved completion from the ROW to the GROUP. A group's rows do not
  flip until its LAST window, so a healthy drain inside a large group (an
  83-window group has already drained here) went silent for longer than
  ``PROGRESS_WINDOW_MINUTES`` and a concurrent boot catch-up's reaper would have
  failed it;
* the fleet's own beacon cannot answer "which machine is on old hooks" — it
  shipped broken once, and pre-beacon boxes cannot self-report at all. The queue
  rows are the evidence: rm1 is capturing rows with no ``source_uri`` today;
* the failure notifier discarded curl's exit code, so a push that never arrived
  looked exactly like one that did — in the one place where nobody is watching.

The unit-file tests parse FILES and never call systemctl: what matters is that
the repo (the thing an install replays onto a machine) encodes the intent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kbcore import fleet_health, ingest_queue, jobs
from kbcore.db import connect, transaction
from kbworker import consolidate, sessions, tail_steps

REPO = Path(__file__).resolve().parents[1]
UNIT_DIR = REPO / "systemd"
NOTIFIER_SCRIPT = UNIT_DIR / "kb-notify-fail.sh"
NOTIFIER_UNIT = UNIT_DIR / "kb-notify-fail@.service"


# ============================================================ 1. reaper heartbeat


def _age_running(hours: int) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE consolidation_requests SET started_at = now() - make_interval(hours => %s) "
            "WHERE status = 'running'",
            (hours,),
        )


def _silence_queue(hours: int = 3) -> None:
    """Every queue row last touched long ago — the state a large group starts in."""
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET processed_at = now() - make_interval(hours => %s)", (hours,)
        )


def _heartbeat_gap_minutes() -> float | None:
    """Exactly what the reaper measures: minutes since ANY queue row last moved."""
    with connect() as conn:
        row = conn.execute(
            "SELECT extract(epoch from (now() - max(processed_at))) / 60.0 FROM ingest_queue"
        ).fetchone()
    return None if row[0] is None else float(row[0])


def _request_status(rid: int) -> str:
    with connect() as conn:
        return conn.execute(
            "SELECT status FROM consolidation_requests WHERE id = %s", (rid,)
        ).fetchone()[0]


@pytest.fixture()
def big_row(kb):
    """One queued submission several extraction windows wide."""
    budget = consolidate.window_budget_bytes()
    line = "User: tell me about the drain\nAssistant: it windows the transcript now\n"
    text = line * ((budget * 3) // len(line) + 1)
    res = ingest_queue.enqueue("fleetproj", text, kind="session", source_uri="session:big")
    assert res.id is not None
    return res.id


@pytest.fixture()
def running_run(kb):
    """A consolidation request claimed long ago: only liveness separates it from dead."""
    # consolidation_requests is session-persistent (not in the conftest TRUNCATE
    # list), so a leftover active row would be the one request_consolidation
    # dedupes against and every assertion below would be about the wrong id.
    with transaction() as conn:
        conn.execute("DELETE FROM consolidation_requests")
    rid, _ = jobs.request_consolidation("nightly")
    assert jobs.claim_pending_consolidation() == rid
    _age_running(jobs.STALE_RUN_HOURS + 1)
    return rid


def test_a_multi_window_drain_beats_the_heartbeat_between_windows(kb, big_row, monkeypatch):
    """The regression: rows flip ONCE, after the last window, so without a
    per-window beat the whole group is silence to the reaper."""
    _silence_queue(hours=3)
    gaps: list[float | None] = []

    def fake(text, project, uri, **kw):
        gaps.append(_heartbeat_gap_minutes())
        return 1, 0

    monkeypatch.setattr(sessions, "extract_and_ingest", fake)
    consolidate._drain_ingest_queue()

    assert len(gaps) >= 3, f"the oversized row was not windowed ({len(gaps)} call(s))"
    assert gaps[0] is not None and gaps[0] > jobs.PROGRESS_WINDOW_MINUTES, (
        "the fixture did not establish pre-drain silence, so the test proves nothing"
    )
    for i, gap in enumerate(gaps[1:], start=1):
        assert gap is not None and gap < jobs.PROGRESS_WINDOW_MINUTES, (
            f"window {i} started {gap} min after the last heartbeat — over the "
            f"{jobs.PROGRESS_WINDOW_MINUTES} min the reaper allows"
        )


def test_a_concurrent_reaper_does_not_kill_a_healthy_multi_window_drain(
    kb, big_row, running_run, monkeypatch
):
    """The live failure: the boot catch-up timer fires mid-nightly, its
    request_consolidation runs the reaper, and the run in flight is 'failed'."""
    _silence_queue(hours=3)
    calls: list[str] = []
    verdicts: list[tuple[int, bool]] = []

    def fake(text, project, uri, **kw):
        calls.append(text)
        if len(calls) >= 2:  # a boot catch-up asks, mid-group
            verdicts.append(jobs.request_consolidation("boot-catchup"))
        return 1, 0

    monkeypatch.setattr(sessions, "extract_and_ingest", fake)
    consolidate._drain_ingest_queue()

    assert verdicts, "the reaper never ran mid-drain — the test did not exercise the bug"
    assert _request_status(running_run) == "running", "a working drain was reaped as dead"
    assert all(rid == running_run and not created for rid, created in verdicts), (
        "the reaper failed the live run and let a second one be queued"
    )


def test_without_the_heartbeat_the_same_drain_IS_reaped(kb, big_row, running_run, monkeypatch):
    """The other half of the pair: with the beat removed, the failure returns —
    so the test above is passing because of the fix and not by accident."""
    _silence_queue(hours=3)
    monkeypatch.setattr(jobs, "touch_drain_heartbeat", lambda ids: 0)
    calls: list[str] = []

    def fake(text, project, uri, **kw):
        calls.append(text)
        if len(calls) >= 2:
            jobs.request_consolidation("boot-catchup")
        return 1, 0

    monkeypatch.setattr(sessions, "extract_and_ingest", fake)
    consolidate._drain_ingest_queue()

    assert _request_status(running_run) == "failed", (
        "expected the pre-fix behaviour with the heartbeat disabled"
    )


def test_the_heartbeat_never_changes_a_rows_status_or_attempts(kb, big_row):
    """Invariant 5: this is a timestamp touch and nothing else."""
    with connect() as conn:
        before = conn.execute(
            "SELECT status, attempts, deferrals, saved_count FROM ingest_queue WHERE id = %s",
            (big_row,),
        ).fetchone()

    assert jobs.touch_drain_heartbeat([big_row]) == 1

    with connect() as conn:
        row = conn.execute(
            "SELECT status, attempts, deferrals, saved_count, processed_at "
            "FROM ingest_queue WHERE id = %s",
            (big_row,),
        ).fetchone()
    assert row[:4] == before, "the heartbeat moved more than the timestamp"
    assert row[4] is not None, "the heartbeat did not land"


def test_the_heartbeat_is_a_no_op_for_an_empty_group(kb):
    """A group with no ids is not an error, just nothing to beat for."""
    assert jobs.touch_drain_heartbeat([]) == 0


def test_the_drain_beats_the_heartbeat_only_between_windows(kb, big_row, monkeypatch):
    """The last window flips the rows (mark_group_extracted), so the beat is for
    the windows BEFORE it — one call per window minus one."""
    beats: list[list[int]] = []
    calls: list[str] = []

    def beat(ids):
        beats.append(list(ids))
        return len(ids)

    def fake(text, project, uri, **kw):
        calls.append(text)
        return 1, 0

    monkeypatch.setattr(jobs, "touch_drain_heartbeat", beat)
    monkeypatch.setattr(sessions, "extract_and_ingest", fake)
    consolidate._drain_ingest_queue()

    assert len(beats) == len(calls) - 1
    assert all(b == [big_row] for b in beats), "the beat must cover the whole group"


# ======================================================= 2. fleet staleness signal


def _age_queue_row(qid: int, hours: int) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET created_at = now() - make_interval(hours => %s) WHERE id = %s",
            (hours, qid),
        )


def test_a_submission_with_no_session_uri_is_counted_as_degenerate(kb):
    """rm1's live symptom: the hook sends no session_uri, so the row normalises to
    NULL and can never coalesce — every row becomes its own extraction."""
    ingest_queue.enqueue("oldhooks", "a session captured by a pre-fix hook", kind="session")
    ingest_queue.enqueue("newhooks", "a session with an id", source_uri="session:abc")

    report = fleet_health.degenerate_by_project()
    assert report["oldhooks"] == {"degenerate": 1, "total": 1}
    assert report["newhooks"] == {"degenerate": 0, "total": 1}
    assert fleet_health.stale_hook_projects(report) == ["oldhooks"]


def test_a_degenerate_uri_counts_even_when_the_key_is_present(kb):
    """'session:' with nothing after it is the shape the broken hook actually
    sent; it must read the same as an absent key (the SQL twin of
    normalise_source_uri), or the signal misses the case it exists for."""
    ingest_queue.enqueue("stubhooks", "a session whose uri is a bare scheme", source_uri="session:")

    assert fleet_health.degenerate_by_project()["stubhooks"]["degenerate"] == 1


def test_the_denominator_rides_along(kb):
    """3 of 3 is a box wholly on old hooks; 3 of 300 is one stale client. Without
    the total the owner cannot tell those apart."""
    for i in range(3):
        ingest_queue.enqueue("mixed", f"good row {i}", source_uri=f"session:{i}")
    ingest_queue.enqueue("mixed", "bad row", kind="session")

    assert fleet_health.degenerate_by_project()["mixed"] == {"degenerate": 1, "total": 4}


def test_a_box_fixed_yesterday_stops_being_reported(kb):
    """The window is what makes this actionable: it must describe the fleet NOW,
    not carry a fixed machine's history forever."""
    res = ingest_queue.enqueue("fixedbox", "a row from the bad old days", kind="session")
    _age_queue_row(res.id, hours=fleet_health.FLEET_WINDOW_HOURS + 1)

    assert "fixedbox" not in fleet_health.degenerate_by_project()
    assert fleet_health.degenerate_by_project(hours=48)["fixedbox"]["degenerate"] == 1


def test_a_clean_fleet_reports_nothing_to_act_on(kb):
    ingest_queue.enqueue("cleanbox", "a properly identified session", source_uri="session:xyz")
    report = fleet_health.degenerate_by_project()

    assert report["cleanbox"]["degenerate"] == 0
    assert fleet_health.stale_hook_projects(report) == []


def test_the_signal_reads_the_same_predicate_the_grouping_pass_does(kb):
    """One definition of 'degenerate'. A signal that disagrees with the behaviour
    it reports on is worse than no signal, so the SQL is imported, not restated."""
    assert fleet_health._DEGENERATE_URI_SQL is ingest_queue._DEGENERATE_URI_SQL


def test_the_signal_never_writes(kb):
    """Read-only by construction: it is an aggregate over evidence that already
    exists (no new table, no migration)."""
    ingest_queue.enqueue("readonly", "a row", kind="session")
    with connect() as conn:
        before = conn.execute("SELECT count(*), max(processed_at) FROM ingest_queue").fetchone()
    fleet_health.degenerate_by_project()
    with connect() as conn:
        assert conn.execute("SELECT count(*), max(processed_at) FROM ingest_queue").fetchone() == (
            before
        )


# --- the digest line ---------------------------------------------------------


def test_the_digest_names_the_projects_with_stale_hooks():
    """Named, not counted: the action is 'go update that machine', and a bare
    number does not say which box to walk over to."""
    lines = tail_steps.digest_lines(
        {
            "queued_drained": 4,
            "fleet_degenerate_by_project": {
                "alpha": {"degenerate": 3, "total": 3},
                "beta": {"degenerate": 0, "total": 9},
                "gamma": {"degenerate": 1, "total": 40},
            },
        }
    )
    assert "STALE HOOKS suspected: alpha, gamma" in lines


def test_a_healthy_fleet_adds_no_digest_line():
    """Only facts the run produced: a zero here reads as noise on every push."""
    clean = {"beta": {"degenerate": 0, "total": 9}}
    lines = tail_steps.digest_lines({"queued_drained": 4, "fleet_degenerate_by_project": clean})
    assert not any("STALE HOOKS" in line for line in lines)


@pytest.mark.parametrize(
    "report",
    [
        None,
        {},
        "not-a-report",
        {"alpha": None},
        {"alpha": 3},
        {"alpha": {}},
        {"alpha": {"degenerate": "two"}},  # a shape no version ever wrote
        {"alpha": {"degenerate": 0, "total": 5}},
    ],
)
def test_an_old_or_broken_report_costs_one_line_not_the_digest(report):
    """digest_lines runs over a report that a FAILED step may not have written and
    that an older run may have written differently. It must degrade to no line."""
    lines = tail_steps.digest_lines({"queued_drained": 1, "fleet_degenerate_by_project": report})
    assert lines == ["drained: 1"]


def test_the_nightly_report_carries_the_fleet_key():
    """The report is persisted in consolidation_requests.result and read
    night-over-night, so the key has to be in the run's own vocabulary."""
    source = (REPO / "src" / "kbworker" / "consolidate.py").read_text()
    assert '"fleet_degenerate_by_project"' in source
    assert "fleet_health.degenerate_by_project" in source


# ===================================================== 3. notify-fail observability


def _unit_value(key: str) -> str:
    for raw in NOTIFIER_UNIT.read_text().splitlines():
        line = raw.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"{NOTIFIER_UNIT.name} has no {key}=")


def _script_const(name: str) -> str:
    body = NOTIFIER_SCRIPT.read_text()
    m = re.search(rf"^{name}=\"?([^\"\n#]+)\"?", body, re.MULTILINE)
    assert m, f"{NOTIFIER_SCRIPT.name} has no {name}="
    return m.group(1).strip()


def test_the_notifier_logs_curls_exit_code():
    """The bug: `>/dev/null 2>&1 || true` made a push that never arrived
    indistinguishable from one that did — and this notifier only ever runs where
    nobody is watching. notify-complete.sh already fixed exactly this."""
    body = NOTIFIER_SCRIPT.read_text()
    assert "RC=$?" in body, "curl's exit code is discarded again"
    assert re.search(r"log_line .*rc=\$RC", body), "the rc is captured but never logged"
    curl_lines = [
        ln for ln in body.splitlines() if "curl " in ln and not ln.strip().startswith("#")
    ]
    assert curl_lines
    for line in curl_lines:
        assert "|| true" not in line, f"the exit code is swallowed again: {line}"


def test_the_notifier_captures_curls_stderr_rather_than_discarding_it():
    """'could not resolve host' and '401' have opposite fixes; a bare rc cannot
    tell them apart."""
    body = NOTIFIER_SCRIPT.read_text()
    assert "2>&1 >/dev/null" in body, "stderr must be captured, not merged into /dev/null"


def test_the_notifier_log_is_bounded():
    """A per-failure log that grows forever is a second ops problem."""
    body = NOTIFIER_SCRIPT.read_text()
    assert re.search(r"tail -\d+ \"\$LOG\"", body), "the log never truncates"


def test_the_notifier_retries_because_a_boot_push_races_cloudflared():
    """The failures most worth alerting on happen at boot, when ntfy and the
    tunnel are still coming up — so the first attempt is the likeliest to fail."""
    tries = int(_script_const("NOTIFY_TRIES"))
    backoff = [int(x) for x in _script_const("NOTIFY_BACKOFF").split()]

    assert tries >= 3, "one shot at a boot-time push is what made this unreliable"
    assert len(backoff) == tries - 1, "there must be no sleep after the final attempt"
    assert backoff == sorted(backoff) and backoff[-1] > backoff[0], "the delay must back off"


def test_the_retry_budget_fits_inside_the_units_timeout():
    """A TimeoutStartSec below the retry budget kills the notifier mid-push and
    the failure is silent again — the bug wearing a different hat."""
    tries = int(_script_const("NOTIFY_TRIES"))
    max_time = int(_script_const("NOTIFY_MAX_TIME"))
    backoff = sum(int(x) for x in _script_const("NOTIFY_BACKOFF").split())
    worst_case = tries * max_time + backoff

    timeout = int(_unit_value("TimeoutStartSec"))
    assert worst_case < timeout, (
        f"the script can take {worst_case}s but the unit kills it at {timeout}s"
    )
    assert timeout <= 300, "a hung notifier must still be bounded"


def test_the_retry_keeps_the_credential_off_argv():
    """The retry loop must not have quietly reintroduced the 2026-07-31 leak."""
    body = NOTIFIER_SCRIPT.read_text()
    assert "-K -" in body, "auth must still come from a curl config on stdin"
    for line in body.splitlines():
        if "curl " not in line or line.strip().startswith("#"):
            continue
        assert "NTFY_TOKEN" not in line and "NTFY_PASSWORD" not in line, line
        assert not re.search(r"-u\s*[\"']?\$", line), f"basic auth in argv: {line}"


def test_the_notifier_still_exits_zero_after_every_retry_fails():
    """A failing failure-notifier is a loop, and a non-zero exit would mask the
    original failure in `systemctl --user list-units --failed`."""
    assert NOTIFIER_SCRIPT.read_text().rstrip().endswith("exit 0")


def test_a_standdown_is_logged_rather_than_silent():
    """'no channel configured' and 'the push failed' both look like a missing
    notification from outside, and they have opposite fixes."""
    body = NOTIFIER_SCRIPT.read_text()
    assert re.search(r"log_line .*no config", body)
    assert re.search(r"log_line .*NTFY_TOPIC", body)
    # ...without breaking the silence guarantee itself.
    assert re.search(r"\[ -r \"\$CFG\" \] \|\| exit 0", body)


def test_the_unit_does_not_still_claim_the_nightly_runs_at_0300():
    """The timers moved to 00:30/01:00 on 2026-08-01. A comment that names a time
    the system no longer uses is how the next reader gets misled."""
    text = NOTIFIER_UNIT.read_text()
    schedules = set(re.findall(r"\b(\d{2}:\d{2})\b", text))
    live = {
        m.group(1)
        for name in ("kb-backup.timer", "kb-consolidate.timer")
        for m in [re.search(r"OnCalendar=.*?(\d{2}:\d{2})", (UNIT_DIR / name).read_text())]
        if m
    }
    assert live, "no OnCalendar found in the timers — this check would be vacuous"
    assert schedules <= live, f"{NOTIFIER_UNIT.name} names dead schedules: {schedules - live}"


def test_the_notifier_documents_both_credential_shapes():
    """A remote box holds a write-only token; the owner box holds the ntfy admin
    basic-auth pair. The script supports both, so the comment must say so."""
    body = NOTIFIER_SCRIPT.read_text()
    header = body.split("set -u", 1)[0]
    assert "NTFY_TOKEN" in header and "WRITE-ONLY" in header.upper()
    assert "NTFY_USER/NTFY_PASSWORD" in header

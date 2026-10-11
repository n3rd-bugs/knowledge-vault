"""RESEND ALERT in the nightly digest (wave C.2, incident KB 49064).

A stale transcript copy re-sent 282 MB of already-covered bytes from byte 0 and
the digest said only ``overlap rows N``. ``fleet_flush.resend_summary`` now carries
``overlap_alerts`` (per principal x session, over KB_RESEND_ALERT_MB or
KB_RESEND_ALERT_ROWS) and the digest renders ONE alert-class line per session.
Telemetry only (proposal_v2 amendment 3): the rows are stored either way.

Real DB in kb_test (the kb fixture); nothing here touches public.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from psycopg.types.json import Jsonb

from kbcore import db, fleet_flush, fleet_overlap, ingest_queue, scope
from kbcore.config import settings
from kbcore.models import FlushStamp
from kbworker import digest, digest_resend_alert

pytestmark = pytest.mark.usefixtures("kb")

PROJECT = "i-qemu"
SID = "bd162e9f-1111-4222-8333-444455556666"
URI = f"session:{SID}"
TOKEN_SHAPED = "kbg_" + "0" * 40  # a fake credential shape — must never reach a push line


@pytest.fixture(autouse=True)
def _thresholds(monkeypatch):
    monkeypatch.setattr(settings, "kb_resend_alert_mb", 10.0)
    monkeypatch.setattr(settings, "kb_resend_alert_rows", 20)


def _as(name: str):
    return scope.use_principal(
        scope.Principal(
            name=name,
            read_projects=frozenset({PROJECT}),
            write_projects=frozenset({PROJECT}),
            caps=scope.ALL_CAPS,
        )
    )


def _overlap_row(
    n: int,
    overlap_bytes: int,
    *,
    uri: str = URI,
    box: str = "i-qemu@rm1",
    hours_ago: float = 0,
    flush_bytes: int | None = None,
    extra: dict | None = None,
) -> int:
    """A stamped row carrying ``meta.overlap`` the way ``queue_guard.apply_stamp`` writes it
    (a FULL overlap unless ``flush_bytes`` exceeds ``overlap_bytes``)."""
    with _as(box):
        res = ingest_queue.enqueue(PROJECT, f"re-sent chunk {uri} {n}", source_uri=uri)
    meta = {
        "flush": {"offset": n, "bytes": flush_bytes or overlap_bytes, "size": 10**9},
        "overlap": {"covered_end": 10**9, "bytes": overlap_bytes},
        **(extra or {}),
    }
    with db.transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET meta = meta || %s, "
            "created_at = now() - make_interval(secs => %s) WHERE id = %s",
            (Jsonb(meta), hours_ago * 3600.0, res.id),
        )
    return res.id


def _created_at(rid: int) -> str:
    with db.connect() as conn:
        at = conn.execute("SELECT created_at FROM ingest_queue WHERE id = %s", (rid,)).fetchone()[0]
    return at.isoformat()


def test_below_threshold_renders_only_the_quiet_fact():
    for n in range(3):
        _overlap_row(n, 300_000)
    s = fleet_flush.resend_summary(hours=24)
    assert s["overlap_rows"] == 3 and "overlap_alerts" not in s
    lines = digest.digest_lines({"resend_summary": s})
    assert lines == ["resend guard: overlap rows 3"]


def test_above_the_mb_threshold_alerts_first_ascii_with_the_repair_command():
    first = _overlap_row(0, 6_000_000, hours_ago=2)
    _overlap_row(1, 6_500_000)
    s = fleet_flush.resend_summary(hours=24)
    assert s["overlap_alerts"] == [
        {
            "principal": "i-qemu@rm1",
            "sid8": "bd162e9f",
            "rows": 2,
            "mb": 12.5,
            "first_at": _created_at(first),
            "since": _created_at(first),
        }
    ]
    lines = digest.digest_lines({"queued_drained": 4, "resend_summary": s})
    assert lines[0] == (
        "RESEND ALERT: i-qemu@rm1 session bd162e9f re-sent 12.5 MB already covered (2 rows) "
        "-- check the box's flush log for 'shrank'/'stale copy'; owner repair (dry run first; "
        "--since is the session's first unrepaired full-overlap row; if the dry run's W0 is "
        "later than --since, re-run the dry run with --since <W0> before --apply): "
        f"kb repair resend-dups --session bd162e9f --replayed --since {_created_at(first)}"
    )
    assert lines[1:] == ["drained: 4", "resend guard: overlap rows 2"], "the fact stays"
    assert all(ln.isascii() for ln in lines)


def test_the_rows_threshold_trips_alone_through_the_real_stamp_path(monkeypatch):
    monkeypatch.setattr(settings, "kb_resend_alert_rows", 3)
    with _as("i-qemu@rm1"):  # the live file's progress: [0, 400) sent and stamped
        ingest_queue.enqueue(
            PROJECT, "a" * 400, source_uri=URI, flush=FlushStamp(offset=0, bytes=400, size=900)
        )
        for n in range(3):  # the stale copy re-sends from byte 0 with new boundaries
            body = chr(ord("b") + n) * 100
            stamp = FlushStamp(offset=n * 100, bytes=100, size=900)
            ingest_queue.enqueue(PROJECT, body, source_uri=URI, flush=stamp)
    s = fleet_flush.resend_summary(hours=24)
    (alert,) = s["overlap_alerts"]
    assert (alert["rows"], alert["mb"], alert["sid8"]) == (3, 0.0, "bd162e9f")
    assert digest.digest_lines({"resend_summary": s})[0].startswith(
        "RESEND ALERT: i-qemu@rm1 session bd162e9f re-sent 0.0 MB already covered (3 rows)"
    )


def test_two_sessions_one_line_each_largest_first():
    other = "session:7c26902a-3e49-43bf-8327-1add36ac9e93"
    _overlap_row(0, 11_000_000)
    _overlap_row(0, 40_000_000, uri=other, box="aquamonix@usama-home")
    s = fleet_flush.resend_summary(hours=24)
    lines = [ln for ln in digest.digest_lines({"resend_summary": s}) if "RESEND ALERT" in ln]
    assert [ln.split()[2:5] for ln in lines] == [
        ["aquamonix@usama-home", "session", "7c26902a"],
        ["i-qemu@rm1", "session", "bd162e9f"],
    ]


def test_the_window_bound_is_respected():
    _overlap_row(0, 30_000_000, hours_ago=30)
    assert "overlap_alerts" not in fleet_flush.resend_summary(hours=24)
    assert fleet_flush.resend_summary(hours=48)["overlap_alerts"][0]["mb"] == 30.0


def test_no_secret_or_token_reaches_the_line():
    _overlap_row(0, 20_000_000, box=TOKEN_SHAPED)
    s = fleet_flush.resend_summary(hours=24)
    assert TOKEN_SHAPED not in str(s), "the principal is scrubbed in kbcore"
    (line,) = digest_resend_alert.resend_alert_lines(s)
    assert "kbg_" not in line and "kbe_" not in line and line.isascii()


def test_cap_odd_shapes_and_field_sanitising():
    entries = [
        {"principal": f"p{i}@h", "sid8": f"{i:08d}", "rows": 30, "mb": 50.0 - i, "first_at": "x"}
        for i in range(5)
    ]
    lines = digest_resend_alert.resend_alert_lines({"overlap_alerts": entries})
    assert len(lines) == digest_resend_alert.RESEND_ALERT_LINES + 1 == 4
    assert lines[-1].startswith("RESEND ALERT: +2 more session(s) over the threshold")
    odd = [None, "junk", {"sid8": None, "mb": 1, "rows": 1}, {"sid8": "abc", "mb": "x", "rows": 1}]
    assert digest_resend_alert.resend_alert_lines({"overlap_alerts": odd}) == []
    for bad in (None, "junk", {"overlap_alerts": "junk"}, {"overlap_alerts": None}):
        assert digest_resend_alert.resend_alert_lines(bad) == []
    weird = {"principal": "rm1; rm -rf ~ é", "sid8": "bd16$(x)", "rows": 21, "mb": 10.0}
    (line,) = digest_resend_alert.resend_alert_lines({"overlap_alerts": [weird]})
    assert line.isascii() and "$(" not in line and "; rm" not in line


# --- review fixes (2026-10-11) -------------------------------------------------


def test_each_criterion_disables_alone_at_or_below_zero(monkeypatch):
    for n in range(3):  # many rows, few MB
        _overlap_row(n, 100_000)
    other = "session:7c26902a-3e49-43bf-8327-1add36ac9e93"
    _overlap_row(0, 30_000_000, uri=other)  # few rows, many MB
    monkeypatch.setattr(settings, "kb_resend_alert_rows", 3)
    monkeypatch.setattr(settings, "kb_resend_alert_mb", 0.0)
    (alert,) = fleet_flush.resend_summary(hours=24)["overlap_alerts"]
    assert alert["sid8"] == "bd162e9f", "MB <= 0: only the rows criterion trips"
    monkeypatch.setattr(settings, "kb_resend_alert_rows", -1)
    monkeypatch.setattr(settings, "kb_resend_alert_mb", 10.0)
    (alert,) = fleet_flush.resend_summary(hours=24)["overlap_alerts"]
    assert alert["sid8"] == "7c26902a", "rows <= 0: only the MB criterion trips"


def test_both_criteria_off_means_no_alert_and_no_query(monkeypatch):
    _overlap_row(0, 50_000_000)
    monkeypatch.setattr(settings, "kb_resend_alert_rows", 0)
    monkeypatch.setattr(settings, "kb_resend_alert_mb", -5.0)
    s = fleet_flush.resend_summary(hours=24)
    assert "overlap_alerts" not in s and s["overlap_rows"] == 1, "the quiet fact stays"
    assert not any("RESEND ALERT" in ln for ln in digest.digest_lines({"resend_summary": s}))

    class _NoQuery:
        def execute(self, *a, **kw):
            raise AssertionError("the overlap-alert query must be skipped")

    assert fleet_overlap.overlap_alerts(_NoQuery(), datetime.now(UTC)) == []


def test_sanitiser_substitutes_underscore_never_a_glob_character():
    weird = {
        "principal": "rm1?*[x]; rm -rf ~ \u00e9",
        "sid8": "b[1]*?6",
        "rows": 21,
        "mb": 10.0,
        "since": "2026-10-08T14:03:00+00:00[*?]",
    }
    (line,) = digest_resend_alert.resend_alert_lines({"overlap_alerts": [weird]})
    assert line.isascii()
    assert not any(ch in line for ch in "?*[]"), "no glob character anywhere on the line"
    assert line.startswith("RESEND ALERT: rm1___x___rm_-rf____ session b_1___6 re-sent")
    assert "--session b_1___6 --replayed" in line
    assert line.endswith("--since 2026-10-08T14:03:00+00:00____")
    old = {"principal": "p@h", "sid8": "abcd1234", "rows": 30, "mb": 1.0}  # no instant at all
    (line,) = digest_resend_alert.resend_alert_lines({"overlap_alerts": [old]})
    assert line.endswith("--since FIRST_RESENT_ROW_CREATED_AT")
    command = line.rsplit("): ", 1)[1]  # what the owner copy-pastes ('<W0>' is prose)
    assert command.startswith("kb repair resend-dups") and "<" not in command


def test_since_is_the_first_overlap_row_ever_so_every_night_names_the_same_instant():
    incident = _overlap_row(0, 1_000_000, hours_ago=30)  # last night, outside tonight's window
    _overlap_row(0, 1_000_000, hours_ago=40, box="aquamonix@usama-home")  # another principal
    tonight = _overlap_row(1, 12_000_000, hours_ago=1)
    (alert,) = fleet_flush.resend_summary(hours=24)["overlap_alerts"]
    assert alert["first_at"] == _created_at(tonight), "tonight's first row, as before"
    assert alert["since"] == _created_at(incident), "the incident's first overlap row, all time"
    (line,) = digest_resend_alert.resend_alert_lines({"overlap_alerts": [alert]})
    assert line.endswith(f"--replayed --since {_created_at(incident)}")
    (old_report,) = digest_resend_alert.resend_alert_lines(
        {"overlap_alerts": [{k: v for k, v in alert.items() if k != "since"}]}
    )
    assert old_report.endswith(f"--since {_created_at(tonight)}"), "an older report: first_at"


# --- review 2, MUST 3: since = the first UNREPAIRED FULL-overlap row ----------------


def test_since_skips_an_earlier_partial_overlap_row():
    """Day 2: a benign partial overlap (a timeout retry with a grown chunk). Day 7: the
    stale copy re-sends from byte 0. The repair's --since must be the day-7 row — the
    day-2 instant would shrink C and drop the new incident's duplicates into NEW."""
    _overlap_row(0, 400, flush_bytes=1_000, hours_ago=24 * 5)  # day 2: 400 of 1000 covered
    resend = _overlap_row(1, 12_000_000, hours_ago=2)  # day 7: every byte already covered
    _overlap_row(2, 3_000_000, hours_ago=1)
    (alert,) = fleet_flush.resend_summary(hours=24)["overlap_alerts"]
    assert alert["since"] == _created_at(resend) == alert["first_at"]


def test_since_excludes_a_full_overlap_row_a_repair_already_marked():
    """An earlier incident's full-overlap rows that a replay apply marked (meta.repair)
    are not this incident's start: since is not the first apply's undo key."""
    at = "2026-10-04T00:00:00+00:00"  # the replay apply's marker shape (resend_repair_replay)
    marker = {"repair": {"mode": "replay", "since": at, "at": at, "by": "resend-dups"}}
    _overlap_row(0, 5_000_000, hours_ago=24 * 6, extra=marker)  # repaired, all bytes covered
    resend = _overlap_row(1, 12_000_000, hours_ago=3)
    (alert,) = fleet_flush.resend_summary(hours=24)["overlap_alerts"]
    assert alert["since"] == _created_at(resend)


def test_since_falls_back_to_first_at_when_no_row_qualifies():
    """Tonight's rows are all partial (or carry an odd non-numeric bytes field, which
    the CASE guard reads as 'not full' instead of failing the cast): since = first_at."""
    first = _overlap_row(0, 12_000_000, flush_bytes=15_000_000, hours_ago=2)
    odd = _overlap_row(1, 6_000_000, hours_ago=1)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET meta = jsonb_set(meta, '{overlap,bytes}', "
            "to_jsonb('6e6x'::text)) WHERE id = %s",
            (odd,),
        )
    (alert,) = fleet_flush.resend_summary(hours=24)["overlap_alerts"]
    assert alert["since"] == alert["first_at"] == _created_at(first)

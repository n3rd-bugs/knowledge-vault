"""Wave S item S4/S6 (KB 33784, root cause KB 33783): fleet flush telemetry.

Real DB in kb_test (the kb fixture); flush stamps and ages are written straight
onto queue rows the way the REST path (item S3) will, since that path lands in
a parallel worktree. The ledger (``ingest_resend_events``, migration 020) is
seeded the way the guard (item S2) UPSERTs it. Nothing here touches public.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from kbcli import queue_cmds
from kbcore import db, fleet_coverage, fleet_flush, fleet_rows, ingest_queue, scope, setup_manifest
from kbcore.config import settings
from kbcore.errors import InvalidOperation
from kbworker import digest, digest_fleet_lines, weekly_fleet, weekly_render

pytestmark = pytest.mark.usefixtures("kb")

PROJECT = "flushproj"
OTHER = "otherproj"
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_ledger(kb):
    """The bounded ledger is not in conftest's truncate list (it holds no content
    and outlives knowledge tables); keep each test's sums its own. Runs AFTER
    ``kb`` (its live-data guards) and names the schema like conftest's TRUNCATE
    does, so a vanished kb_test errors instead of falling through to public."""
    assert settings.kb_db_schema == "kb_test"
    with db.transaction() as conn:
        conn.execute("DELETE FROM kb_test.ingest_resend_events")
    yield


def _as_box(name: str):
    return scope.use_principal(
        scope.Principal(
            name=name,
            read_projects=frozenset({PROJECT, OTHER}),
            write_projects=frozenset({PROJECT, OTHER}),
            caps=scope.ALL_CAPS,
        )
    )


def _row(
    body: str,
    *,
    box: str = "rm1",
    project: str = PROJECT,
    uri: str = "session:abcdef0123456789",
    hook_version: str | None = None,
    hours_ago: float = 0.0,
    stamp: dict | None = None,
    extra_meta: dict | None = None,
) -> int:
    with _as_box(box):
        res = ingest_queue.enqueue(project, body, source_uri=uri, hook_version=hook_version)
    assert res.id, "fixture rows must be fresh (unique bodies)"
    meta_patch = dict(extra_meta or {})
    if stamp is not None:
        meta_patch["flush"] = stamp
    with db.transaction() as conn:
        if meta_patch:
            conn.execute(
                "UPDATE ingest_queue SET meta = meta || %s WHERE id = %s",
                (Jsonb(meta_patch), res.id),
            )
        if hours_ago:
            conn.execute(
                "UPDATE ingest_queue SET created_at = now() - make_interval(secs => %s) "
                "WHERE id = %s",
                (hours_ago * 3600.0, res.id),
            )
    return res.id


def _stamp(offset: int, nbytes: int, size: int, event: str = "Stop", box: str = "lab") -> dict:
    return {"offset": offset, "bytes": nbytes, "size": size, "event": event, "box": box}


def _ledger(principal: str, uri: str, rows: int, nbytes: int, hours_ago: float = 0.0) -> None:
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ingest_resend_events
                   (principal, uri, first_at, last_at, refused_rows, refused_bytes, last_kind)
            VALUES (%s, %s, now() - make_interval(secs => %s), now() - make_interval(secs => %s),
                    %s, %s, 'session')
            """,
            (principal, uri, hours_ago * 3600.0, hours_ago * 3600.0, rows, nbytes),
        )


# --- setup_manifest ----------------------------------------------------------


def test_setup_manifest_agrees_with_the_served_route(kb):
    from kbrest.server import _SETUP_ASSETS, _SETUP_SCRIPTS, create_app

    assert setup_manifest.SETUP_SCRIPTS == _SETUP_SCRIPTS
    assert setup_manifest.SETUP_ASSETS == _SETUP_ASSETS
    served = (
        TestClient(create_app())
        .get("/api/setup/manifest.json", headers={"Authorization": f"Bearer {settings.kb_token}"})
        .json()
    )
    assert setup_manifest.manifest() == served, "the two digests must be byte-identical"
    assert setup_manifest.version() == served["version"] and len(served["version"]) == 12


# --- fleet_flush_by_box ------------------------------------------------------


def test_by_box_fields(kb):
    _row("rm1 chunk one", stamp=_stamp(0, 100, 300, "PreCompact"), hook_version="oldver000000")
    _row("rm1 chunk two", stamp=_stamp(100, 200, 300, "Stop"), hook_version="newver000000")
    _row("rm1 unstamped legacy", uri="session:zzzz")
    _row("rm1 swept", uri="session:swept", stamp=_stamp(0, 50, 50, "Backfill", box="lab2"))
    _row("ws older", box="win-laptop", hours_ago=30, stamp=_stamp(0, 10, 10))
    _row("per-project token", box="docker-mount@usama-server", uri="session:dm")

    report = fleet_flush.fleet_flush_by_box()

    rm1 = report["rm1"]
    assert rm1["host"] == "rm1", "a principal with no '@' is its own host"
    assert report["docker-mount@usama-server"]["host"] == "usama-server", "per-principal rows keep"
    assert rm1["rows"] == 4 and rm1["sessions"] == 3 and rm1["sessions_30d"] == 3
    assert rm1["unstamped"] == 1
    assert rm1["labels"] == ["lab", "lab2"], "one principal, two labels = one credential, two boxes"
    assert rm1["last_event"] == "Backfill" and rm1["hook_version"] == "newver000000"
    assert rm1["sweep_sessions"] == 1 and rm1["sweep_recovered_mb"] == 0.0
    assert rm1["age_hours"] < 0.1 and "tails_open" not in rm1
    assert rm1["mb"] == 0.0 and isinstance(rm1["last_seen"], str)
    win = report["win-laptop"]
    assert win["rows"] == 0, "outside the tiled window: nothing counted"
    assert 29.9 < win["age_hours"] < 30.1, "but last_seen is all-time, so the age still reads"
    assert win["labels"] == ["lab"] and win["last_event"] == "Stop", "labels/events span 30 d"
    for key in ("unstamped", "hook_version", "sweep_sessions", "tails_open"):
        assert key not in win, "absent means absent (no blank, no zero)"


def test_open_tails_at_the_boundary(kb):
    hours = settings.kb_fleet_tail_hours
    # A session whose last stamped firing reached 200 of 900 bytes, 25 h ago.
    _row("t1 a", uri="session:11111111aaaa", stamp=_stamp(0, 100, 900), hours_ago=hours + 2)
    _row("t1 b", uri="session:11111111aaaa", stamp=_stamp(100, 100, 900), hours_ago=hours + 1)
    # Same shape but fresh: the flush may still be running.
    _row("t2 a", uri="session:22222222bbbb", stamp=_stamp(0, 100, 900), hours_ago=hours - 1)
    # Complete: reach == size, old, not a tail.
    _row("t3 a", uri="session:33333333cccc", stamp=_stamp(0, 900, 900), hours_ago=hours + 5)

    tails = fleet_flush.open_tails()
    assert [t["sid8"] for t in tails] == ["11111111"]
    assert tails[0]["mb"] == 0.0 and tails[0]["project"] == PROJECT and tails[0]["box"] == "rm1"
    assert len(fleet_flush.open_tails(hours=0)) == 2, "an explicit horizon reaches the fresh one"
    box = fleet_flush.fleet_flush_by_box()["rm1"]
    assert box["tails_open"] == 1
    assert box["largest_tail"] == {"mb": 0.0, "project": PROJECT, "sid8": "11111111"}


# --- fleet_silent ------------------------------------------------------------


def test_host_of():
    assert fleet_rows.host_of("docker-mount@usama-server") == "usama-server"
    assert fleet_rows.host_of("i-qemu@rm1") == "rm1"
    assert fleet_rows.host_of("a@b@rm1") == "rm1", "the part after the LAST '@'"
    assert fleet_rows.host_of("rm1") == "rm1", "no '@': its own host"
    assert fleet_rows.host_of("(unknown)") == "(unknown)"
    assert fleet_rows.host_of("odd@") == "odd@", "a trailing '@' names nothing; keep the principal"


def test_silence_at_72h_with_the_30d_floor(kb):
    _row("quiet box", box="quiet", hours_ago=settings.kb_fleet_silent_hours + 1)
    _row("live box", box="live", hours_ago=settings.kb_fleet_silent_hours - 1)
    _row("gone box", box="gone", hours_ago=24 * (fleet_flush.FLEET_FLOOR_DAYS + 1))

    silent = fleet_flush.fleet_silent()
    assert [s["host"] for s in silent] == ["quiet"], "positive-only; the 30 d floor drops 'gone'"
    assert silent[0]["sessions_30d"] == 1 and silent[0]["principals"] == ["quiet"]
    assert 73 <= silent[0]["age_hours"] <= 73.1
    assert fleet_flush.fleet_silent(hours=200) == []


def test_silence_is_keyed_on_the_host(kb):
    """F1: per-project tokens are ``project@host``. Three finished projects
    gone quiet beside one live one is a LIVE host (never named); a host none of
    whose principals submitted is silent, with its principals listed."""
    quiet = settings.kb_fleet_silent_hours + 10
    _row("live project", box="kb-system@usama-server", uri="session:live", hours_ago=1)
    for proj in ("docker-mount", "expenses", "old-proj"):
        _row(f"{proj} done", box=f"{proj}@usama-server", uri=f"session:{proj}", hours_ago=quiet)
    _row("rm1 qemu", box="i-qemu@rm1", uri="session:q1", hours_ago=quiet)
    _row("rm1 rewriter", box="i-binary-rewriter@rm1", uri="session:q2", hours_ago=quiet + 5)

    silent = fleet_flush.fleet_silent()
    assert [s["host"] for s in silent] == ["rm1"]
    assert silent[0]["principals"] == ["i-binary-rewriter@rm1", "i-qemu@rm1"]
    assert silent[0]["sessions_30d"] == 2
    assert quiet <= silent[0]["age_hours"] <= quiet + 0.1, "the host's LATEST principal's age"


def test_a_labelled_principals_prestamp_rows_never_rescue_its_host(kb):
    """The pre-stamp (unlabelled) rows of a labelled principal are history or
    an unstamping transport: a fresh one does not make the host live. The
    quiet machine is still named ``principal/label`` among the principals."""
    _row("shared B old", box="shared", stamp=_stamp(0, 5, 5, box="deskB"), hours_ago=100)
    _row("shared prestamp fresh", box="shared", uri="session:fresh")

    silent = fleet_flush.fleet_silent()
    assert [s["host"] for s in silent] == ["shared"]
    assert silent[0]["principals"] == ["shared/deskB"]
    assert 99.9 <= silent[0]["age_hours"] <= 100.1

    _row("shared A fresh", box="shared", uri="session:a", stamp=_stamp(0, 5, 5, box="deskA"))
    assert fleet_flush.fleet_silent() == [], "a labelled machine still flushing keeps the host live"


# --- stale_hook_boxes --------------------------------------------------------


def test_stale_hooks_compare_against_the_served_manifest(kb, monkeypatch):
    current = setup_manifest.version()
    _row("current box", box="cur", hook_version=current, stamp=_stamp(0, 1, 1))
    _row("stale box", box="old", hook_version="4bfd73b21062", stamp=_stamp(0, 1, 1))
    _row("mcp only box", box="mcp")

    _row("idle box, unversioned but outside the window", box="idle", hours_ago=30)

    stale = {"server": current, "boxes": {"old/lab": "4bfd73b21062", "mcp": "unversioned"}}
    assert fleet_flush.stale_hook_boxes() == stale, (
        "a labelled group is named principal/label; a group whose window rows never stamp a "
        "version is 'unversioned' with require-stamps OFF (F2); no window rows = not judged"
    )
    assert fleet_flush.stale_hook_boxes("4bfd73b21062") == {
        "server": "4bfd73b21062",
        "boxes": {"cur/lab": current, "mcp": "unversioned"},
    }
    monkeypatch.setattr(settings, "kb_fleet_require_stamps", True)
    assert fleet_flush.stale_hook_boxes()["boxes"] == {
        "old/lab": "4bfd73b21062",
        "mcp": "unversioned",
        "idle": "unstamped",
    }, "the require-stamps arm still names a never-stamping population member"


def test_only_hook_traffic_can_be_unversioned(kb):
    """A memory/document submission over MCP is not hook traffic: on 2026-09-14 one
    owner-token kb_ingest_session row (kind memory, uri claude-code:...) named the
    OWNER principal 'unversioned'. Only kind=session rows with a session: uri —
    what flush-session-to-kb.sh sends — can carry the verdict."""
    with _as_box("owner"):
        res = ingest_queue.enqueue(
            PROJECT, "an mcp memory note", kind="memory", source_uri="claude-code:d7d07bd3"
        )
    assert res.id
    assert fleet_flush.stale_hook_boxes() == {}, "no hook traffic -> nobody is unversioned"
    _row("a hook flush with no version", box="owner", uri="session:0011223344556677")
    assert fleet_flush.stale_hook_boxes()["boxes"] == {"owner": "unversioned"}, (
        "the same principal IS unversioned once it flushes session rows without a hook_version"
    )


def test_require_stamps_flags_unstamped_flushes_of_a_versioned_group(kb, monkeypatch):
    """The ``unstamped`` arm is about ``meta.flush``: a current-version group
    that still submits rows with no flush stamp is flagged only under the knob;
    the version it stamps is never mistaken for 'unversioned'."""
    current = setup_manifest.version()
    _row("stamped", box="mix", hook_version=current, stamp=_stamp(0, 1, 1))
    _row("no flush stamp", box="mix", uri="session:bare", hook_version=current)
    assert fleet_flush.stale_hook_boxes() == {}
    monkeypatch.setattr(settings, "kb_fleet_require_stamps", True)
    assert fleet_flush.stale_hook_boxes()["boxes"] == {"mix": "unstamped"}, (
        "the unstamped row is the labelled principal's pre-stamp group, still arriving in the "
        "window, so the bare principal is named (V4-M-6); the stamped 'mix/lab' group is clean"
    )


def test_staleness_is_keyed_on_principal_and_label(kb):
    """One credential on two machines (V4-M-6): deskA ran update.sh and submitted
    LAST; deskB is still on the old hooks. Keyed on the principal alone, A's
    latest row hid B — the stale group must be named ``shared/deskB``."""
    current = setup_manifest.version()
    old = "4bfd73b21062"
    _row("B old hooks", box="shared", hook_version=old, stamp=_stamp(0, 1, 1, box="deskB"))
    _row("A current", box="shared", hook_version=current, stamp=_stamp(0, 1, 1, box="deskA"))

    assert fleet_flush.stale_hook_boxes() == {
        "server": current,
        "boxes": {"shared/deskB": "4bfd73b21062"},
    }


def test_a_labelled_principals_unlabelled_rows_speak_only_while_still_arriving(kb):
    """The pre-stamp history of a labelled principal is history; unlabelled rows
    STILL arriving in the window are a machine on old hooks that shares the
    credential (no label to name it, so the bare principal is flagged)."""
    current = setup_manifest.version()
    _row("A current", box="shared", hook_version=current, stamp=_stamp(0, 1, 1, box="deskA"))
    _row("history", box="shared", uri="session:old1", hook_version="4bfd73b21062", hours_ago=240)
    assert fleet_flush.stale_hook_boxes() == {}, "ten-day-old pre-stamp rows are history"

    _row("still on old hooks", box="shared", uri="session:old2", hook_version="4bfd73b21062")
    assert fleet_flush.stale_hook_boxes()["boxes"] == {"shared": "4bfd73b21062"}


# --- resend_summary ----------------------------------------------------------


def test_resend_sums_come_from_the_ledger(kb):
    _ledger("rm1", "session:aaaa", rows=40, nbytes=4_000_000)
    _ledger("rm1", "session:bbbb", rows=2, nbytes=200_000)
    _ledger("usama-server", "session:cccc", rows=5, nbytes=500_000)
    _ledger("usama-server", "session:aaaa", rows=1, nbytes=100_000)  # same uri, 2nd principal
    _ledger("win", "session:dddd", rows=99, nbytes=9_000_000, hours_ago=25)  # last night
    _row("overlapping", extra_meta={"overlap": {"rows": [1]}})
    dup = _row("a duplicate", uri="session:dupdup")
    with db.transaction() as conn:
        conn.execute("UPDATE ingest_queue SET status = 'duplicate' WHERE id = %s", (dup,))

    s = fleet_flush.resend_summary()
    assert s["refused_rows"] == 48 and s["refused_mb"] == 4.8
    assert s["sessions"] == 3, "DISTINCT uris: aaaa under two principals is one session"
    assert s["boxes"] == ["rm1", "usama-server"]
    assert s["per_box"] == [["rm1", 42, 4.2], ["usama-server", 6, 0.6]]
    assert s["overlap_rows"] == 1
    assert "stamp_inconsistent" not in s, "the guard leaves no row marker; no dead zero key"
    assert s["duplicate_rows"] == 1 and s["duplicate_mb"] == 0.0
    assert fleet_flush.resend_summary(hours=48)["refused_rows"] == 147


def test_a_quiet_night_yields_empty_containers_and_no_keys(kb):
    assert fleet_flush.resend_summary() == {}
    assert fleet_flush.fleet_silent() == []
    assert fleet_flush.stale_hook_boxes() == {}
    assert fleet_flush.fleet_flush_by_box() == {}
    counts = {"queued_drained": 1, "fleet_flush": {}, "fleet_silent": [], "resend_summary": {}}
    before = set(counts)
    assert digest.digest_lines(counts) == ["drained: 1"]
    assert set(counts) == before, "rendering never adds counts keys"


# --- queue_coverage ----------------------------------------------------------


def test_coverage_offers_a_seed_only_from_stamped_rows_of_one_clean_project(kb):
    uri = "session:cafe0000deadbeef"
    _row("c1", uri=uri, stamp=_stamp(0, 100, 900))
    _row("c2", uri=uri, stamp=_stamp(100, 250, 900))
    _row("c3 unstamped", uri=uri)

    (s,) = fleet_coverage.queue_coverage(session_prefix="cafe0000").sessions
    assert s.seed_candidate == 350 and s.refusal is None
    assert s.stamped_rows == 2 and s.rows == 3 and s.boxes == ["rm1"]
    assert s.post_scrub_bytes == len("c1c2c3 unstamped") and s.post_scrub_mb == 0.0
    assert [p.model_dump() for p in s.projects] == [
        {
            "project": PROJECT,
            "rows": 3,
            "mb": 0.0,
            "pending": 3,
            "extracted": 0,
            "failed": 0,
            "duplicate": 0,
        }
    ]
    assert fleet_coverage.SessionCoverage.model_fields["post_scrub_bytes"].description == (
        "post-scrub, NOT a seed"
    )


def test_coverage_refusals(kb):
    two = "session:2proj000000"
    _row("p1", uri=two, project=PROJECT, stamp=_stamp(0, 10, 50))
    _row("p2", uri=two, project=OTHER, stamp=_stamp(10, 10, 50))
    red = "session:redact00000"
    _row("secret ghp_" + "0" * 36 + " leaked", uri=red, stamp=_stamp(0, 60, 60))
    bare = "session:nostamp0000"
    _row("no stamps at all", uri=bare)

    by = {s.uri: s for s in fleet_coverage.queue_coverage(box="rm1").sessions}
    assert by[two].seed_candidate is None
    assert by[two].refusal == f"rows under 2 projects ({PROJECT}, {OTHER}); repair first"
    assert [p.project for p in by[two].projects] == [PROJECT, OTHER]
    assert by[red].seed_candidate is None and by[red].redacted_rows == 1
    assert by[red].refusal.startswith("1 row(s) carry redactions")
    assert by[bare].refusal.startswith("no stamped rows")
    assert fleet_coverage.queue_coverage(session_prefix="2proj").sessions[0].uri == two
    assert fleet_coverage.queue_coverage(session_prefix="%").sessions == [], "LIKE is escaped"


def test_coverage_is_owner_only_and_needs_exactly_one_selector(kb):
    with pytest.raises(InvalidOperation):
        fleet_coverage.queue_coverage()
    with pytest.raises(InvalidOperation):
        fleet_coverage.queue_coverage(session_prefix="a", box="b")
    with _as_box("rm1"), pytest.raises(InvalidOperation):
        fleet_coverage.queue_coverage(session_prefix="a")
    _row("would match session:%", uri="session:anything")
    with pytest.raises(InvalidOperation):  # blank is NO selector, never "every session"
        fleet_coverage.queue_coverage(session_prefix="   ")
    with pytest.raises(InvalidOperation):
        fleet_coverage.queue_coverage(box=" ")


def test_kb_queue_coverage_renders_the_dry_table(kb):
    from typer.testing import CliRunner

    from kbcli.main import app

    uri = "session:c11000000000"
    _row("k1", uri=uri, stamp=_stamp(0, 100, 900))
    r = CliRunner().invoke(app, ["queue", "coverage", "--session", "c11"])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0] == "queue coverage for session c11: 1 session(s)"
    assert lines[1].startswith(f"session {uri}: 1 rows, 0.0 MB (post-scrub, NOT a seed); ")
    assert lines[1].endswith("; boxes: rm1; stamped 1/1")
    assert lines[-1] == "  seed candidate: 100 (max flush offset+bytes over stamped rows)"
    assert all(ln.isascii() for ln in lines)
    s = fleet_coverage.queue_coverage(box="rm1").sessions[0]
    s.seed_candidate, s.refusal = None, "no stamped rows"
    assert queue_cmds.render_session(s)[-1] == "  seed: REFUSED - no stamped rows"


# --- digest lines -------------------------------------------------------------

FLEET = {
    "usama-server": {"age_hours": 2.1, "rows": 9, "mb": 4.0, "sessions": 3, "sessions_30d": 20},
    "rm1": {
        "age_hours": 9.4,
        "rows": 2,
        "mb": 1.0,
        "sessions": 1,
        "sessions_30d": 12,
        "tails_open": 2,
        "largest_tail": {"mb": 171.3, "project": "aquamonix", "sid8": "7c26902a"},
        "sweep_sessions": 1,
        "sweep_recovered_mb": 2.4,
        "hook_version": "4bfd73b21062",
        "labels": ["rm1-lab"],
    },
    "win-laptop": {"age_hours": 31.0, "rows": 0, "mb": 0.0, "sessions": 0, "sessions_30d": 4},
    "box4": {"age_hours": 50.0, "rows": 0, "mb": 0.0, "sessions": 0, "sessions_30d": 1},
    "box5": {"age_hours": 60.0, "rows": 0, "mb": 0.0, "sessions": 0, "sessions_30d": 1},
}
NIGHT = {
    "queued_drained": 4,
    "fleet_flush": FLEET,
    "fleet_silent": [{"name": "rm1", "box": "rm1", "age_hours": 98.4, "sessions_30d": 12}],
    "fleet_stale_hooks": {"server": "1a2b3c4d5e6f", "boxes": {"rm1": "4bfd73b21062"}},
    "resend_summary": {
        "refused_rows": 212,
        "refused_mb": 13.4,
        "sessions": 3,
        "boxes": ["rm1"],
        "overlap_rows": 1,
    },
    "ingest_queue_pending": 4123,
}


def test_digest_alert_and_fact_lines_exact_ascii():
    lines = digest.digest_lines(NIGHT)
    assert lines == [
        "STALE HOOKS: rm1 4bfd73b21062 -- server 1a2b3c4d5e6f",
        "FLEET SILENT: rm1 (last flush 4.1d ago, 12 sessions/30d)",
        "RESEND GUARD: refused 212 rows/13.4 MB to date from rm1"
        " - a box is resending; check its offsets",
        "QUEUE DEPTH: 4123 pending (82% of 5000 cap) - the drain is falling behind",
        "drained: 4",
        "fleet: usama-server 2.1h, rm1 9.4h, win-laptop 31.0h, box4 50.0h +1",
        "resend guard: overlap rows 1",
        "flush tails >24h: 2 sessions (largest 171.3 MB, aquamonix 7c26902a)",
        "sweep: 1 sessions / 2.4 MB recovered (rm1 1)",
    ]
    assert all(ln.isascii() for ln in lines)


def test_fleet_silent_line_names_hosts_oldest_first_and_caps():
    silent = [
        {"host": f"h{i}", "age_hours": 120.0 + 24 * i, "principals": [f"p@h{i}"], "sessions_30d": i}
        for i in range(6)
    ]
    line = digest_fleet_lines.fleet_silent_line(silent)
    assert line == (
        "FLEET SILENT: h5 (last flush 10.0d ago, 5 sessions/30d), "
        "h4 (last flush 9.0d ago, 4 sessions/30d), h3 (last flush 8.0d ago, 3 sessions/30d), "
        "h2 (last flush 7.0d ago, 2 sessions/30d) +2"
    )
    assert line.isascii() and digest_fleet_lines.FLEET_LINE_BOXES == 4
    pre_f1 = [{"name": "rm1", "box": "rm1", "age_hours": 98.4, "sessions_30d": 12}]
    assert digest_fleet_lines.fleet_silent_line(pre_f1) == (
        "FLEET SILENT: rm1 (last flush 4.1d ago, 12 sessions/30d)"
    ), "an old report's name/box still renders"
    assert digest_fleet_lines.fleet_silent_line([{"age_hours": 1}]) is None


def test_stale_hooks_line_collapses_principals_of_one_host():
    stale = {
        "server": "575fa122d16a",
        "boxes": {
            "i-rocket-chip@rm1": "unversioned",
            "muhammadusama@rm1": "unversioned",
            "i-qemu@rm1": "unversioned",
            "docker-mount@usama-server": "unversioned",
            "lab/desk": "4bfd73b21062",
            "kb@rm1": "4bfd73b21062",
        },
    }
    assert digest_fleet_lines.stale_hooks_line({"fleet_stale_hooks": stale}) == (
        "STALE HOOKS: docker-mount@usama-server unversioned, rm1 (3 principals) unversioned, "
        "kb@rm1 4bfd73b21062, lab/desk 4bfd73b21062 -- server 575fa122d16a"
    ), "more than two principals of one host with one verdict collapse; two stay named"
    two = {"a@rm1": "unversioned", "b@rm1": "unversioned"}
    assert digest_fleet_lines.collapse_stale(two) == ["a@rm1 unversioned", "b@rm1 unversioned"]
    # ONE shared credential on three desks (V4-M-6) is one principal, not three:
    # its labelled groups stay named label by label, never "shared (3 principals)".
    desks = {f"shared/desk{d}": "unversioned" for d in "ABC"}
    assert digest_fleet_lines.collapse_stale(desks) == [
        "shared/deskA unversioned",
        "shared/deskB unversioned",
        "shared/deskC unversioned",
    ]
    # Three DISTINCT principals of one host collapse even when one carries two labels.
    mixed = {"a@rm1": "unversioned", "b@rm1/x": "unversioned", "b@rm1/y": "unversioned"}
    mixed["c@rm1"] = "unversioned"
    assert digest_fleet_lines.collapse_stale(mixed) == ["rm1 (3 principals) unversioned"]


def test_stale_hooks_is_one_merged_line():
    both = dict(NIGHT, fleet_degenerate_by_project={"alpha": {"degenerate": 3, "total": 3}})
    lines = digest.digest_lines(both)
    stale = [ln for ln in lines if ln.startswith("STALE HOOKS")]
    assert stale == [
        "STALE HOOKS: rm1 4bfd73b21062 -- server 1a2b3c4d5e6f; suspected (no session id): alpha"
    ]
    legacy_only = digest.digest_lines(
        {"fleet_degenerate_by_project": {"alpha": {"degenerate": 3, "total": 3}}}
    )
    assert legacy_only == ["STALE HOOKS suspected: alpha"], "the legacy text survives verbatim"


def test_resend_below_the_alert_threshold_is_a_fact(monkeypatch):
    monkeypatch.setattr(settings, "kb_resend_alert_per_night", 50)
    summary = {"refused_rows": 41, "refused_mb": 2.0, "sessions": 3, "boxes": ["usama-server"]}
    lines = digest.digest_lines({"resend_summary": summary})
    assert lines == ["resend guard: 41 refused to date (3 sessions; boxes: usama-server)"]
    assert digest_fleet_lines.resend_guard_alert(dict(summary, refused_rows=51)) is not None
    # The cap is a module-top import: a relocation fails at import, never as a lost alert.
    assert hasattr(ingest_queue, "MAX_PENDING") and digest_fleet_lines.MAX_PENDING == 5000
    assert digest_fleet_lines.queue_depth_line(3999) is None
    assert digest_fleet_lines.queue_depth_line(4000).startswith("QUEUE DEPTH: 4000 pending (80%")


@pytest.mark.parametrize(
    "counts",
    [
        {"fleet_flush": None},
        {"fleet_flush": "junk", "fleet_silent": "junk", "fleet_stale_hooks": "junk"},
        {"fleet_stale_hooks": {"boxes": None}, "resend_summary": {"refused_rows": "many"}},
        {"fleet_silent": [{"age_hours": "x"}], "ingest_queue_pending": "deep"},
        {"fleet_flush": {"b": "junk", 3: None}},
    ],
)
def test_odd_shapes_cost_no_line_and_no_exception(counts):
    assert digest.digest_lines(dict(counts, queued_drained=1)) == ["drained: 1"]


def test_odd_per_box_values_cost_only_their_own_line():
    odd = {"fleet_flush": {"b": {"tails_open": "2", "largest_tail": "big", "sweep_sessions": True}}}
    assert digest_fleet_lines.tails_line(odd) is None
    assert digest_fleet_lines.sweep_line(odd) is None
    assert digest.digest_lines(odd) == ["fleet: b 0.0h"]


def test_digest_stays_under_the_cap_with_every_alert_on():
    legacy_alerts = {  # every pre-wave-S alert key (tests/test_wave_g_reporting FULL_NIGHT)
        "eval_regression": True,
        "eval_hit_at_5": 0.5,
        "eval_trust_status": "watch",
        "untriaged_expiring_7d": 3,
        "fleet_degenerate_by_project": {"alpha": {"degenerate": 1, "total": 1}},
        "deferral_escalations": 2,
        "eval_rerank_tally": {"active": 50, "fallback": 2, "off": 0},
        "stopped": {"evidence_refreshed": {"reason": "battery", "deferred": 20}},
        "swap_used_pct": 90.0,
        "search_misses_24h": 4,
        "search_misses_thin_24h": 2,
        "queued_items_saved": 2,
        "review_triaged": 3,
        "auto_applied": 4,
        "contradiction_pairs_judged": 5,
        "groups_near_deferral_cap": 1,
        "drained_deferred": 6,
        "attempts_cap_retirements": 1,
        "review_expired": 500,
        "never_triaged_open": 100,
        "promote_candidates": 9,
        "rerank_probe": "failed",
    }
    loud = dict(legacy_alerts, **NIGHT)
    alerts = digest._alert_lines(loud)
    assert len(alerts) == 13 and len(alerts) < digest.DIGEST_MAX_LINES
    lines = digest.digest_lines(loud)
    assert len(lines) <= digest.DIGEST_MAX_LINES == 20
    assert lines[: len(alerts)] == alerts, "the cap trims facts, never an alert"
    assert all(ln.isascii() for ln in lines)


# --- weekly -------------------------------------------------------------------

NOW = datetime(2026, 9, 13, 1, 30, tzinfo=UTC)
WEEK = [
    (NOW - timedelta(days=2), {"queued_drained": 1, "resend_summary": {"refused_rows": 1000}}),
    (
        NOW - timedelta(days=1),
        dict(
            NIGHT, resend_summary=dict(NIGHT["resend_summary"], duplicate_rows=3, duplicate_mb=1.5)
        ),
    ),
]
MISS_STATS = {"total": 0, "distinct": 0}


def test_weekly_block_and_push_line():
    block = weekly_fleet.fleet_block(WEEK, NOW)
    assert block[1] == "fleet flush (window totals; last = age of last flush):"
    assert block[3].startswith("  box4 ")
    rm1 = next(ln for ln in block if ln.startswith("  rm1 "))
    # 'last' ages from the WEEKLY's now: 9.4h before a report a day old = 33.4h.
    assert rm1.split() == [
        "rm1",
        "2",
        "1.0",
        "1",
        "2.4",
        "33.4h",
        "2",
        "4bfd73b21062",
        "0",
        "rm1-lab",
    ]
    assert block[-3] == weekly_fleet.SESS_NOTE
    # PEAK night, never a sum (each night re-reports lifetime tallies): 1000, not 1212.
    assert block[-2] == "  resend refused to date (peak night of 2): 1000 rows / 0.0 MB"
    assert block[-1] == (
        "  duplicate rows on record: 3 (1.5 MB) -> kb repair resend-dups --all --rows-only"
    )
    assert weekly_fleet.push_line(WEEK, NOW) == [
        "fleet: 5 boxes; silent: rm1 4.1d; stale hooks: 1; resend refused 1000 to date"
    ]
    assert weekly_fleet.fleet_block([]) == [] and weekly_fleet.push_line([]) == []
    assert weekly_fleet.push_line([(NOW, {"queued_drained": 1})]) == []
    assert all(ln.isascii() for ln in block)


def _night(at: datetime, **box: object) -> tuple[datetime, dict]:
    return at, {"fleet_flush": {"rm1": dict(box)}}


def test_weekly_table_sums_every_fleet_report_and_ages_from_the_max_last_seen():
    """F3: the table used to read the LAST report carrying fleet_flush; a
    resumed run (battery pause -> AC-restore) reports a short tiled window and
    zeroed every box for the week."""
    first = _night(
        NOW - timedelta(days=3),
        last_seen=(NOW - timedelta(days=3, hours=2)).isoformat(),
        age_hours=2.0,
        rows=10,
        mb=4.0,
        sessions=3,
        sweep_recovered_mb=1.0,
        unstamped=2,
        hook_version="aaaa00000000",
        tails_open=3,
        labels=["a"],
    )
    second = _night(
        NOW - timedelta(days=1),
        last_seen=(NOW - timedelta(hours=30)).isoformat(),
        age_hours=6.0,
        rows=5,
        mb=1.5,
        sessions=2,
        hook_version="bbbb00000000",
        labels=["b"],
    )
    resumed = _night(NOW - timedelta(hours=20), age_hours=40.0, rows=0, mb=0.0, sessions=0)
    block = weekly_fleet.fleet_block([first, second, resumed], NOW)
    rm1 = next(ln for ln in block if ln.startswith("  rm1 ")).split()
    assert rm1 == ["rm1", "15", "5.5", "5", "1.0", "30.0h", "0", "bbbb00000000", "2", "a,b"], (
        "sums over every report; last = max last_seen aged from now; hook_ver = latest "
        "non-empty; tails from the last report; a zero-row resumed report zeroes nothing"
    )
    assert weekly_fleet.push_line([first, second, resumed], NOW) == ["fleet: 1 boxes"]
    # No now: ages from the last fleet-carrying report (pure, deterministic).
    alone = next(ln for ln in weekly_fleet.fleet_block([first]) if ln.startswith("  rm1 "))
    assert alone.split()[5] == "2.0h"


def test_weekly_push_line_names_silent_hosts_oldest_first_and_caps():
    """fleet_silent() returns hosts in NAME order; the push must cap in AGE
    order like the digest line, so '+N' never hides the quietest machine."""
    silent = [{"host": f"h{i}", "age_hours": 120.0 + 24 * i, "principals": []} for i in range(6)]
    night = (NOW, {"fleet_flush": FLEET, "fleet_silent": silent})
    assert weekly_fleet.push_line([night], NOW) == [
        "fleet: 5 boxes; silent: h5 10.0d, h4 9.0d, h3 8.0d, h2 7.0d +2"
    ]
    push = weekly_render._push_lines(NOW, [night], MISS_STATS, [], 0, None, 7)
    assert len(push) < weekly_render.PUSH_MAX_LINES and all(ln.isascii() for ln in push)


def test_weekly_render_carries_the_block_and_the_push_stays_short():
    body = weekly_render._report_body(NOW, WEEK, MISS_STATS, [], 0, 0, None, 0, [])
    assert "fleet flush (window totals; last = age of last flush):" in body
    assert "kb repair resend-dups --all --rows-only" in body
    push = weekly_render._push_lines(NOW, WEEK, MISS_STATS, [], 0, None, 42)
    assert push[-2] == (
        "fleet: 5 boxes; silent: rm1 4.1d; stale hooks: 1; resend refused 1000 to date"
    )
    assert push[-1] == "full table: kb-system item 42"
    assert len(push) < weekly_render.PUSH_MAX_LINES and all(ln.isascii() for ln in push)
    quiet = weekly_render._push_lines(
        NOW, [(NOW, {"queued_drained": 1})], MISS_STATS, [], 0, None, 1
    )
    assert not any(ln.startswith("fleet:") for ln in quiet)


def test_the_push_cap_trims_the_fleet_line_before_the_full_table_pointer(monkeypatch):
    full = weekly_render._push_lines(NOW, WEEK, MISS_STATS, [], 0, None, 42)
    monkeypatch.setattr(weekly_render, "PUSH_MAX_LINES", len(full) - 1)
    capped = weekly_render._push_lines(NOW, WEEK, MISS_STATS, [], 0, None, 42)
    assert len(capped) == len(full) - 1
    assert capped[-1] == "full table: kb-system item 42", "the pointer always survives"
    assert not any(ln.startswith("fleet:") for ln in capped), "the fleet line is what gets cut"


# --- wiring -------------------------------------------------------------------


def test_a_quiet_nightly_run_carries_the_four_keys_as_empty_containers(kb, monkeypatch):
    """Behavioural, not a source grep: a real ``consolidate.main()`` on a quiet
    fleet (the test_ac_restore seam — nothing queued, so no Ollama) stores the
    four ADDED keys with their empty shapes beside the never-renamed one."""
    from kbworker import consolidate

    monkeypatch.setattr(consolidate.eval_sentinel, "run_sentinel", lambda counts: None)
    counts = consolidate.main()
    assert counts["skipped"] is False and "fleet_by_box" in counts
    assert counts["fleet_flush"] == {} and counts["fleet_silent"] == []
    assert counts["fleet_stale_hooks"] == {} and counts["resend_summary"] == {}
    rendered = digest.digest_lines(counts)
    assert not any(ln.startswith(("fleet", "FLEET", "RESEND", "resend")) for ln in rendered)


def test_the_nightly_never_renames_fleet_by_box():
    source = (REPO / "src" / "kbworker" / "consolidate.py").read_text()
    assert '"fleet_by_box", fleet_health.fleet_by_box' in source, "never renamed"
    for key, fn in (
        ("fleet_flush", "fleet_flush_by_box"),
        ("fleet_silent", "fleet_silent"),
        ("fleet_stale_hooks", "stale_hook_boxes"),
        ("resend_summary", "resend_summary"),
    ):
        assert f'_run_step(counts, "{key}", fleet_flush.{fn})' in source

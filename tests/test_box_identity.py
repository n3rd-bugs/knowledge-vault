"""Wave C (review #3 P2, KB 19663): box identity in capture.

The server always AUTHENTICATED a per-machine principal at ingest and then
discarded it. Now:
* ``ingest_queue.enqueue`` stamps the principal NAME into the queue row's meta
  (additive key ``submitted_by``) — read from ``kbcore.scope`` (rule 12:
  authority never rides a caller argument, so REST/MCP inherit it for free);
* the drain threads it into the extracted items' meta provenance, the same
  place session repo/commit ride (kbworker.prep);
* ``fleet_health.fleet_by_box`` aggregates the staleness split BY BOX, riding
  the nightly report as the ADDED key ``fleet_by_box`` beside the project view.

Real DB in kb_test; no Ollama involvement beyond conftest's embed stub.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from kbcore import db, fleet_health, ingest_queue, scope
from kbworker import prep

pytestmark = pytest.mark.usefixtures("kb")

PROJECT = "boxproj"


def _as_box(name: str):
    """A per-machine principal with an rw grant on PROJECT (what /api/enrol mints)."""
    return scope.use_principal(
        scope.Principal(
            name=name,
            read_projects=frozenset({PROJECT}),
            write_projects=frozenset({PROJECT}),
            caps=scope.ALL_CAPS,
        )
    )


def _row_meta(qid: int) -> dict:
    with db.connect() as conn:
        return conn.execute("SELECT meta FROM ingest_queue WHERE id = %s", (qid,)).fetchone()[0]


def _strip_stamp(qid: int) -> None:
    """Simulate a row enqueued before the stamp existed (legacy client)."""
    with db.transaction() as conn:
        conn.execute("UPDATE ingest_queue SET meta = meta - 'submitted_by' WHERE id = %s", (qid,))


# --- the enqueue stamp -------------------------------------------------------


def test_enqueue_stamps_the_authenticated_submitter(kb):
    owner_res = ingest_queue.enqueue(PROJECT, "captured by the owner box", source_uri="session:o")
    assert _row_meta(owner_res.id)["submitted_by"] == "owner"

    with _as_box("box-rm1"):
        box_res = ingest_queue.enqueue(PROJECT, "captured by rm1", source_uri="session:r")
    assert _row_meta(box_res.id)["submitted_by"] == "box-rm1", (
        "the per-machine principal the bearer authenticated is what gets stamped"
    )


def test_identity_comes_from_scope_never_from_the_caller(kb):
    """Rule 12: authority comes from kbcore.scope — enqueue must not even OFFER a
    submitted_by argument a caller could forge."""
    assert "submitted_by" not in inspect.signature(ingest_queue.enqueue).parameters


# --- threading into item provenance ------------------------------------------


def test_prep_threads_the_box_into_item_provenance(kb):
    with _as_box("box-rm1"):
        ingest_queue.enqueue(
            PROJECT,
            "a captured session about the widget fix",
            source_uri="session:prov",
            repo="/home/u/widgets",
            commit="deadbee",
        )

    jobs = prep._assemble_jobs(ingest_queue.pending_groups())

    assert len(jobs) == 1
    extra = jobs[0]["extra_meta"]
    assert extra["submitted_by"] == "box-rm1", "rides the SAME meta the drain already stamps"
    assert extra["session_repo"] == "widgets" and extra["session_commit"] == "deadbee", (
        "beside the existing repo/commit provenance, not instead of it"
    )


def test_a_legacy_row_threads_no_empty_key(kb):
    res = ingest_queue.enqueue(PROJECT, "an old row", source_uri="session:legacy")
    _strip_stamp(res.id)

    jobs = prep._assemble_jobs(ingest_queue.pending_groups())

    assert len(jobs) == 1
    assert "submitted_by" not in jobs[0]["extra_meta"], (
        "absent means ABSENT — a field with nothing to say is omitted, not sent blank"
    )


# --- the per-box fleet aggregate ----------------------------------------------


def test_fleet_by_box_aggregates_the_staleness_split(kb):
    # The owner's box is on pre-fix hooks (no session id -> degenerate row).
    ingest_queue.enqueue(PROJECT, "a session with no id", kind="session")
    # rm1 submits properly identified work.
    with _as_box("box-rm1"):
        ingest_queue.enqueue(PROJECT, "an identified session", source_uri="session:ok")
    # A legacy client predates the stamp entirely.
    legacy = ingest_queue.enqueue(PROJECT, "a pre-stamp row", source_uri="session:old")
    _strip_stamp(legacy.id)

    report = fleet_health.fleet_by_box()

    assert report["owner"] == {"degenerate": 1, "total": 1}
    assert report["box-rm1"] == {"degenerate": 0, "total": 1}
    assert report["(unknown)"] == {"degenerate": 0, "total": 1}, (
        "pre-stamp clients group under (unknown) — an unknown box is a box on old code"
    )
    assert fleet_health.stale_hook_projects(report) == ["owner"], (
        "the digest's tolerant reader reads the by-box view unchanged (same shape)"
    )


def test_fleet_by_box_respects_the_window(kb):
    res = ingest_queue.enqueue(PROJECT, "a row from the bad old days", kind="session")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET created_at = now() - make_interval(hours => %s) WHERE id = %s",
            (fleet_health.FLEET_WINDOW_HOURS + 1, res.id),
        )

    assert "owner" not in fleet_health.fleet_by_box()
    assert fleet_health.fleet_by_box(hours=48)["owner"]["degenerate"] == 1


def test_the_nightly_report_adds_fleet_by_box_and_renames_nothing(kb):
    """Telemetry keys are ADDED, never renamed: the new key rides beside the
    project view, which stays byte-identical in name and producer."""
    source = (
        Path(__file__).resolve().parent.parent / "src" / "kbworker" / "consolidate.py"
    ).read_text()
    assert '"fleet_degenerate_by_project"' in source, "the existing key is untouched"
    assert '"fleet_by_box"' in source and "fleet_health.fleet_by_box" in source

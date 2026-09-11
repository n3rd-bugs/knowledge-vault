"""kb repair resend-dups — review-fix behaviours (wave S, S5, fixer pass).

Companion of :mod:`tests.test_resend_repair` (same helpers, same kb_test-only rule).
Each test pins one finding: the backfill can never mint a duplicate_of cycle; the
``--as-resend`` paths work AFTER ``--backfill-keys`` (the runbook order) and after
an undo; tier 2 sees the rows an ``--as-resend`` run is about to flip; the audit
stamp names the group's duplicate rows across a two-pass run; ``--limit`` bounds
the work; ``--since`` is tz-aware and cleanly guarded; ``--all --as-resend`` is
refused; listed-but-ignored rows are reported; undo ``--since`` is the marker time
on both sides; the reaper, the drain and ``requeue_failed`` never see a flip.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from kbcli.main import app
from kbcore import db, ingest_queue, jobs, resend_repair, resend_repair_items
from kbcore.errors import InvalidOperation
from kbcore.ingest import ingest
from kbcore.queue_guard import chunk_key
from kbcore.queue_repair import requeue_failed
from kbcore.resend_repair import RepairSelector, session_rows_sql
from tests.test_resend_repair import (
    CHUNK1,
    SID,
    URI,
    _hash,
    _hist,
    _item,
    _item_state,
    _review_resolved,
    _row,
    _seed_extracted_canonical,
)

pytestmark = pytest.mark.usefixtures("kb")
runner = CliRunner()
PARKED = "parked 2026-09-10 owner: resend residue"


def _cli(*args: str, yes: bool = False):
    return runner.invoke(app, ["repair", "resend-dups", *args], input="y\n" if yes else None)


# --- [major] backfill after an --as-resend flip ---------------------------------


def test_backfill_after_an_as_resend_flip_never_cycles():
    low = _hist("gateway", CHUNK1)  # lowest id, pending
    mined = _hist("aquamonix", CHUNK1, status="extracted", saved=2)
    sel = RepairSelector(project="gateway", as_resend=True, rows_only=True)
    resend_repair.apply(resend_repair.plan(sel), sel)
    assert _row(low)["status"] == "duplicate" and _row(low)["duplicate_of"] == mined

    plan = resend_repair.backfill_keys(dry_run=False)
    assert plan.to_key == {mined: chunk_key(URI, _hash(CHUNK1))} and plan.to_mark == {}
    assert _row(mined)["duplicate_of"] is None and _row(mined)["chunk_key"], "canonical unmarked"
    assert _row(low)["duplicate_of"] == mined and _row(low)["chunk_key"] is None
    assert resend_repair.backfill_keys(dry_run=False).marked == 0

    # The broken state the old code minted (every row marked): skipped, never "fixed" into a cycle.
    with db.transaction() as c:
        c.execute("UPDATE ingest_queue SET duplicate_of = %s WHERE id = %s", (low, mined))
    plan = resend_repair.backfill_keys(dry_run=False)
    assert plan.skipped == 1 and plan.to_key == {} and plan.to_mark == {}


# --- [major] --as-resend after --backfill-keys, and after an undo ---------------


def test_backfill_then_rows_file_as_resend_flips_the_parked_row(tmp_path):
    a1 = _hist("aquamonix", CHUNK1, status="extracted", saved=3)
    parked = _hist("rm3-tester", CHUNK1, status="failed", error=PARKED, attempts=3)
    resend_repair.backfill_keys(dry_run=False)
    assert _row(parked)["duplicate_of"] == a1, "step 0 stamps the parked row too"
    f = tmp_path / "parked.txt"
    f.write_text(f"{parked} rm3-tester failed\n")

    result = _cli("--rows-file", str(f), "--as-resend", "--rows-only", "--apply", yes=True)
    assert result.exit_code == 0, result.output
    assert "FLIPPED 1 row(s)" in result.output
    r = _row(parked)
    assert r["status"] == "duplicate" and r["duplicate_of"] == a1
    assert r["meta"]["repair"]["prev_status"] == "failed" and r["processed_at"] is not None
    assert r["error"] == f"repair: resend duplicate of {a1}"


def test_as_resend_apply_undo_redo_pairs_the_same_rows_again():
    a1 = _hist("aquamonix", CHUNK1, status="extracted", saved=3)
    b1 = _hist("rm3-tester", CHUNK1)
    parked = _hist("rm3-tester", CHUNK1 + " tail", status="failed", error=PARKED, attempts=3)
    _hist("aquamonix", CHUNK1 + " tail", status="extracted", saved=0)
    sel = RepairSelector(project="rm3-tester", as_resend=True, rows_only=True)
    resend_repair.apply(resend_repair.plan(sel), sel)
    assert _row(b1)["status"] == _row(parked)["status"] == "duplicate"

    done = resend_repair.undo(sel, dry_run=False)
    assert done.rows_restored == 2 and _row(parked)["status"] == "failed"
    assert _row(b1)["duplicate_of"] == a1, "undo keeps the fact"

    (p,) = resend_repair.plan(sel)
    assert p.flip == {b1: a1, parked: _row(parked)["duplicate_of"]} and p.parked == 1
    resend_repair.apply([p], sel)
    assert _row(b1)["status"] == _row(parked)["status"] == "duplicate"
    assert _row(parked)["meta"]["repair"]["prev_error"] == PARKED


# --- tier 2 under --as-resend ------------------------------------------------------


def test_attribute_quotes_under_as_resend_sees_the_planned_flips():
    a1 = _hist("aquamonix", CHUNK1, status="extracted", saved=5)
    dup_row = _hist("docker-mount", CHUNK1)  # unmarked: no backfill ran
    own_row = _hist("docker-mount", "assistant: only docker-mount ever saw this line.")
    from_dup, _ = _item("docker-mount", "dup item", "bind-mount /srv/data read-only.")
    from_own, _ = _item("docker-mount", "own item", "only docker-mount ever saw this line.")

    sel = RepairSelector(project="docker-mount", as_resend=True, attribute_quotes=True)
    (p,) = resend_repair.plan(sel)
    assert (p.rows, p.dup, p.dup_ids, p.flip) == (2, 1, [dup_row], {dup_row: a1})
    assert p.archive_ids == [from_dup] and p.keep == {"ambiguous": 1}
    resend_repair.apply([p], sel)
    assert _item_state(from_dup)[0] == "archived" and _item_state(from_own)[0] == "active"
    assert _item_state(from_dup)[1]["archived"]["queue_rows"] == [dup_row]
    assert _row(own_row)["status"] == "pending"


# --- two-pass audit stamp + the canon column under --rows-only --------------------


def test_two_pass_run_stamps_the_groups_duplicate_rows_and_reads_the_gate_under_rows_only():
    _, (b1, b2) = _seed_extracted_canonical()
    iid, rq = _item("docker-mount", "mount decision", "read-only.")
    resend_repair.backfill_keys(dry_run=False)

    dry = _cli("--all", "--rows-only")
    assert dry.exit_code == 0 and "extracted,5" in dry.output, dry.output
    first = _cli("--all", "--rows-only", "--apply", yes=True)
    assert first.exit_code == 0 and "FLIPPED 2 row(s); ARCHIVED 0 item(s)" in first.output
    assert _item_state(iid)[0] == "active"

    second = _cli("--session", SID[:8], "--apply", yes=True)
    assert second.exit_code == 0 and "FLIPPED 0 row(s); ARCHIVED 1 item(s)" in second.output
    status, meta, _ = _item_state(iid)
    assert status == "archived" and meta["archived"]["queue_rows"] == sorted([b1, b2])
    assert _review_resolved(rq)


# --- --limit, --since, --all --as-resend, ignored listed rows ---------------------


def test_limit_bounds_the_planning_work(monkeypatch):
    _seed_extracted_canonical()
    _hist("aquamonix", "remint firing", uri="session:remint-1", status="extracted", saved=1)
    _hist("gateway", "remint firing", uri="session:remint-1")
    resend_repair.backfill_keys(dry_run=False)
    planned: list[str] = []
    monkeypatch.setattr(resend_repair_items, "plan_items", lambda p, **kw: planned.append(p.uri))
    plans = resend_repair.plan(RepairSelector(all=True, limit=1))
    assert len(plans) == 1 and planned == [plans[0].uri]


def test_selector_since_is_always_tz_aware():
    naive = RepairSelector(all=True, since=datetime(2020, 1, 1))
    assert naive.since == datetime(2020, 1, 1, tzinfo=UTC)
    aware = datetime(2020, 1, 1, 12, tzinfo=UTC)
    assert RepairSelector(all=True, since=aware).since == aware


def test_cli_since_garbage_is_a_clean_error():
    result = _cli("--since", "garbage")
    assert result.exit_code == 1 and "ISO-8601" in result.output, result.output
    assert not isinstance(result.exception, InvalidOperation)


def test_all_with_as_resend_is_refused():
    with pytest.raises(InvalidOperation, match="BOUNDED"):
        resend_repair.require_selector(RepairSelector(all=True, as_resend=True))
    result = _cli("--all", "--as-resend", "--rows-only")
    assert result.exit_code == 1 and "BOUNDED" in result.output
    resend_repair.require_selector(RepairSelector(session="7c", as_resend=True))


def test_rows_file_reports_listed_rows_it_cannot_touch(tmp_path):
    a1 = _hist("aquamonix", CHUNK1, status="extracted", saved=3)
    parked = _hist("rm3-tester", CHUNK1, status="failed", error=PARKED, attempts=3)
    plain_failed = _hist("rm3-tester", "x", status="failed", error="embed ReadTimeout")
    done = _hist("rm3-tester", "y", status="extracted", saved=0)
    f = tmp_path / "rows.txt"
    f.write_text(f"{parked}\n{plain_failed}\n{done}\n999999999\n")

    result = _cli("--rows-file", str(f), "--as-resend", "--rows-only")
    assert result.exit_code == 0, result.output
    assert "flip 1 row(s); keep: not pending/parked (2)" in result.output
    assert "keep: unknown queue id (1)" in result.output
    assert _row(parked)["status"] == "failed" and _row(parked)["duplicate_of"] is None, "dry run"

    resend_repair.backfill_keys(dry_run=False)
    marked = _cli("--rows-file", str(f), "--rows-only")  # the default (marked) path
    assert marked.exit_code == 0 and "keep: not a marked pending duplicate (3)" in marked.output
    assert _row(parked)["duplicate_of"] == a1


def test_undo_since_is_the_marker_time_on_rows_and_items():
    _, (b1, b2) = _seed_extracted_canonical()
    iid, _ = _item("docker-mount", "mount decision", "read-only.")
    with db.transaction() as c:
        c.execute("UPDATE ingest_queue SET created_at = '2020-01-01T00:00:00Z'")
    resend_repair.backfill_keys(dry_run=False)
    sel = RepairSelector(session=SID[:8])
    resend_repair.apply(resend_repair.plan(sel), sel)

    recent = datetime.now(UTC) - timedelta(minutes=5)
    u = resend_repair.undo(RepairSelector(session=SID[:8], since=recent), dry_run=True)
    assert sorted(u.row_ids) == sorted([b1, b2]) and u.item_ids == [iid]
    future = datetime.now(UTC) + timedelta(hours=1)
    u = resend_repair.undo(RepairSelector(session=SID[:8], since=future), dry_run=True)
    assert u.row_ids == [] and u.item_ids == []


# --- hybrid §1.10 gaps: summary, reaper, parked drain, one transaction -------------


def test_summary_items_are_never_listed():
    _seed_extracted_canonical()
    iid, _ = _item("docker-mount", "mount decision", "read-only.")
    summ = ingest("docker-mount", "summary", "weekly", "body", kind="session", uri=URI)
    resend_repair.backfill_keys(dry_run=False)
    sel = RepairSelector(session=SID[:8])
    (p,) = resend_repair.plan(sel)
    assert p.items == 1 and p.archive_ids == [iid]
    resend_repair.apply([p], sel)
    assert _item_state(summ)[0] == "active" and _item_state(iid)[0] == "archived"


def _recent_progress() -> int:
    with db.connect() as c:
        return c.execute(
            "SELECT count(*) FROM ingest_queue "
            "WHERE processed_at > now() - make_interval(mins => %s)",
            (jobs.PROGRESS_WINDOW_MINUTES,),
        ).fetchone()[0]


def test_reaper_sees_no_progress_after_a_flip():
    _, (b1, b2) = _seed_extracted_canonical()
    with db.transaction() as c:  # the canonical was mined long ago; a run is stuck
        c.execute("UPDATE ingest_queue SET processed_at = now() - interval '3 hours'")
        c.execute(
            "INSERT INTO consolidation_requests (status, started_at) "
            "VALUES ('running', now() - make_interval(hours => %s))",
            (jobs.STALE_RUN_HOURS + 1,),
        )
    resend_repair.backfill_keys(dry_run=False)
    assert _recent_progress() == 0
    sel = RepairSelector(session=SID[:8], rows_only=True)
    resend_repair.apply(resend_repair.plan(sel), sel)
    assert _row(b1)["status"] == _row(b2)["status"] == "duplicate"
    assert _recent_progress() == 0, "a flip is not drain progress"
    with db.transaction() as c:
        assert jobs._reap_stale_runs(c) == 1


def test_parked_row_flip_is_invisible_to_the_drain_and_requeue_failed():
    _hist("aquamonix", CHUNK1, status="extracted", saved=3)
    parked = _hist("rm3-tester", CHUNK1, status="failed", error=PARKED, attempts=3)
    sel = RepairSelector(project="rm3-tester", as_resend=True, rows_only=True)
    resend_repair.apply(resend_repair.plan(sel), sel)
    assert _row(parked)["status"] == "duplicate"
    assert requeue_failed(reason="test") == [], "a 'duplicate' row is not a failed row"
    assert _row(parked)["status"] == "duplicate"
    grouped = {r["id"] for g in ingest_queue.pending_groups(quiet_minutes=0) for r in g["rows"]}
    assert parked not in grouped and ingest_queue.queue_depth() == 0
    assert {r["id"] for r in ingest_queue.pending(limit=100)} == set()


def test_archive_and_review_resolution_are_one_transaction(monkeypatch):
    _seed_extracted_canonical()
    iid, rq = _item("docker-mount", "mount decision", "read-only.")
    resend_repair.backfill_keys(dry_run=False)
    sel = RepairSelector(session=SID[:8])
    (p,) = resend_repair.plan(sel)
    assert p.archive_ids == [iid]

    class _Boom(RuntimeError):
        pass

    class _Proxy:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a, **kw):
            if "UPDATE items" in sql:
                raise _Boom("items update failed after the review rows were resolved")
            return self._conn.execute(sql, *a, **kw)

    real = db.transaction

    @contextmanager
    def failing_transaction():
        with real() as conn:
            yield _Proxy(conn)

    monkeypatch.setattr(resend_repair_items, "transaction", failing_transaction)
    with pytest.raises(_Boom):
        resend_repair_items.archive_group(p)
    assert not _review_resolved(rq), "the review flip rolled back with the item flip"
    assert _item_state(iid)[0] == "active"


# --- hygiene: the aliased session-rows fragment compiles and excludes degenerate uris --


def test_session_rows_fragment_compiles_with_and_without_an_alias():
    _hist("gateway", CHUNK1)
    _hist("gateway", "degenerate identity", uri="session:")
    with db.connect() as c:
        aliased = c.execute(
            f"SELECT count(*) FROM ingest_queue q WHERE {session_rows_sql('q')}"  # noqa: S608
        ).fetchone()[0]
        plain = c.execute(
            f"SELECT count(*) FROM ingest_queue WHERE {session_rows_sql()}"  # noqa: S608
        ).fetchone()[0]
    assert aliased == plain == 1

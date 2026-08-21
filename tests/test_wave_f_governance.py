"""Wave F governance package (review #5): wall-blocked disposition (val-3),
the `kb review bulk --suggested-rejects` owner path (strat-8), and the
untriaged-pool gauge beside the eligible-flow activity counters (val-5).

Real DB (kb_test via the ``kb`` fixture); the LLM is never called — triage
suggestions are written production-shaped via review_triage.store_triage and
backdated (the test_auto_apply pattern). Everything asserted is an Invariant-5
status flip: nothing is deleted, no trust changes, and the project wall itself
(0cfa6e0) is untouched — only ROWS the wall already ruled on are disposed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from kbcore import db, review_auto, review_rejects, review_triage
from kbcore.config import settings
from kbcore.errors import InvalidOperation
from kbcore.ingest import ingest
from kbcore.items import log_access
from kbcore.review import queue_review

runner = CliRunner()


# --- helpers (test_auto_apply shapes) ----------------------------------------


def _unverified(tag: str, project: str = "p") -> tuple[int, int]:
    """One unverified item + its open unverified_extraction row."""
    item_id = ingest(project, "note", f"note {tag}", f"body {tag}", trust="unverified", uri=tag)
    return item_id, queue_review(item_id, "unverified_extraction", {"rejected_count": 0})


def _triage(qid: int, action: str, dup_of: int | None = None, days_old: float = 10) -> None:
    """Store a production-shaped triage suggestion, backdated past the cooling rail."""
    assert review_triage.store_triage(qid, {"action": action, "dup_of": dup_of, "rationale": "t"})
    at = (datetime.now(UTC) - timedelta(days=days_old)).isoformat()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE review_queue SET detail = jsonb_set(detail, '{triage,at}', to_jsonb(%s::text)) "
            "WHERE id = %s",
            (at, qid),
        )


def _row(qid: int) -> tuple[bool, dict]:
    with db.connect() as conn:
        r = conn.execute(
            "SELECT resolved, detail FROM review_queue WHERE id = %s", (qid,)
        ).fetchone()
    return bool(r[0]), (r[1] if isinstance(r[1], dict) else {})


def _status(item_id: int) -> str:
    with db.connect() as conn:
        return conn.execute("SELECT status FROM items WHERE id = %s", (item_id,)).fetchone()[0]


@pytest.fixture(autouse=True)
def _auto_apply_defaults(monkeypatch):
    """Pin the knobs so a live .env cannot decide what these tests measure."""
    monkeypatch.setattr(settings, "kb_auto_apply_enabled", True)
    monkeypatch.setattr(settings, "kb_auto_apply_cooling_days", 3)


# --- wall_blocked disposition (val-3) ----------------------------------------


def test_close_wall_blocked_resolves_only_the_walled_aged_stock(kb):
    # Wall-blocked: cross-project retirement, aged past cooling, never accessed.
    canon_other = ingest("other", "note", "canon-x", "surviving body x", uri="wb-canon")
    blocked, q_blocked = _unverified("wb-dup", project="p")
    _triage(q_blocked, "reject_duplicate", dup_of=canon_other)
    # Same-project duplicate: appliable, therefore NOT wall-blocked.
    same_canon = ingest("p", "note", "canon-same", "surviving body same", uri="wb-same-canon")
    same, q_same = _unverified("wb-same", project="p")
    _triage(q_same, "reject_duplicate", dup_of=same_canon)
    # Cross-project but still cooling: the owner has not seen 3 digests yet.
    fresh_canon = ingest("other", "note", "canon-fresh", "surviving fresh", uri="wb-fresh-canon")
    fresh, q_fresh = _unverified("wb-fresh", project="p")
    _triage(q_fresh, "reject_duplicate", dup_of=fresh_canon, days_old=1)
    # Cross-project but recently recalled: rail 2 hands it to the human.
    used_canon = ingest("other", "note", "canon-used", "surviving used", uri="wb-used-canon")
    used, q_used = _unverified("wb-used", project="p")
    _triage(q_used, "reject_duplicate", dup_of=used_canon)
    log_access(used, "kb_get")

    assert review_rejects.close_wall_blocked_rows() == 1
    resolved, detail = _row(q_blocked)
    assert resolved is True
    assert detail["auto_resolution"]["kind"] == "wall_blocked"
    assert detail["triage"]["action"] == "reject_duplicate", "the verdict is preserved"
    assert _status(blocked) == "active", "the ITEM is untouched — the wall's ruling stands"
    for q, item in ((q_same, same), (q_fresh, fresh), (q_used, used)):
        assert _row(q)[0] is False and _status(item) == "active"
    assert review_rejects.close_wall_blocked_rows() == 0, "idempotent: the stock is disposed"


def test_wall_blocked_close_is_reversible_and_the_reopen_sticks(kb):
    canon = ingest("other", "note", "u-canon", "surviving u", uri="u-canon")
    dup, qid = _unverified("u-dup", project="p")
    _triage(qid, "reject_duplicate", dup_of=canon)
    assert review_rejects.close_wall_blocked_rows() == 1

    since = datetime.now(UTC) - timedelta(minutes=5)
    assert review_rejects.undo_wall_blocked(since, dry_run=True) == [qid]
    assert _row(qid)[0] is True, "dry run writes nothing"
    assert review_rejects.undo_wall_blocked(since) == [qid]
    resolved, detail = _row(qid)
    assert resolved is False
    assert detail["auto_resolution"]["kind"] == "wall_blocked"
    assert detail["auto_resolution"]["undone_at"], "the reopen is stamped"
    assert _status(dup) == "active"
    # The stamp is a PERMANENT veto (candidates() excludes undone rows), so the
    # next nightly close cannot silently re-close what the owner reopened.
    assert review_rejects.close_wall_blocked_rows() == 0
    assert _row(qid)[0] is False
    assert review_rejects.undo_wall_blocked(since) == [], "an open row is not re-undone"


def test_close_wall_blocked_respects_the_kill_switch(kb, monkeypatch):
    canon = ingest("other", "note", "k-canon", "surviving k", uri="k-canon")
    _dup, qid = _unverified("k-dup", project="p")
    _triage(qid, "reject_duplicate", dup_of=canon)
    monkeypatch.setattr(settings, "kb_auto_apply_enabled", False)
    assert review_rejects.close_wall_blocked_rows() == 0
    assert _row(qid)[0] is False


def test_undo_wall_blocked_requires_an_aware_datetime(kb):
    with pytest.raises(InvalidOperation):
        review_rejects.undo_wall_blocked(datetime(2026, 1, 1))  # noqa: DTZ001 — the point


# --- the owner bulk-reject path (strat-8) ------------------------------------


def test_list_suggested_rejects_selection(kb):
    noise, q_noise = _unverified("s-noise", project="p")
    _triage(q_noise, "reject_noise")
    canon = ingest("p", "note", "s-canon", "surviving s", uri="s-canon")
    _dup, q_dup = _unverified("s-dup", project="p")
    _triage(q_dup, "reject_duplicate", dup_of=canon)
    _keep, q_keep = _unverified("s-keep", project="p")
    _triage(q_keep, "keep")
    _plain, _q_plain = _unverified("s-plain", project="p")  # no verdict at all
    _other, q_other = _unverified("s-other", project="q")
    _triage(q_other, "reject_noise")
    retired, q_retired = _unverified("s-retired", project="p")
    _triage(q_retired, "reject_noise")
    with db.transaction() as conn:  # retired out-of-band: the moot sweep's job
        conn.execute("UPDATE items SET status = 'archived' WHERE id = %s", (retired,))

    rows = review_rejects.list_suggested_rejects()
    assert {r["queue_id"] for r in rows} == {q_noise, q_dup, q_other}
    assert rows[0]["queue_id"] == q_noise, "oldest first"
    assert rows[0]["item_id"] == noise and rows[0]["project"] == "p"
    assert {r["action"] for r in rows} == {"reject_noise", "reject_duplicate"}
    only_p = review_rejects.list_suggested_rejects(project="p")
    assert {r["queue_id"] for r in only_p} == {q_noise, q_dup}
    assert [r["queue_id"] for r in review_rejects.list_suggested_rejects(limit=1)] == [q_noise]


def test_cli_suggested_rejects_dry_run_is_the_default(kb):
    from kbcli.main import app

    item, qid = _unverified("cli-sr", project="p")
    _triage(qid, "reject_noise")
    res = runner.invoke(app, ["review", "bulk", "--suggested-rejects"])
    assert res.exit_code == 0
    assert "dry run: 1 item(s) would be archived" in res.output
    assert _row(qid)[0] is False and _status(item) == "active"
    # The explicit spelling the weekly report advertises behaves identically,
    # and --dry-run wins over --apply when both are passed.
    for argv in (
        ["review", "bulk", "--suggested-rejects", "--dry-run"],
        ["review", "bulk", "--suggested-rejects", "--apply", "--dry-run"],
    ):
        res = runner.invoke(app, argv)
        assert res.exit_code == 0 and _status(item) == "active"


def test_cli_suggested_rejects_apply_confirms_then_archives(kb):
    from kbcli.main import app

    item, qid = _unverified("cli-sra", project="p")
    _triage(qid, "reject_noise")
    res = runner.invoke(app, ["review", "bulk", "--suggested-rejects", "--apply"], input="n\n")
    assert res.exit_code != 0 and _status(item) == "active", "declining writes nothing"
    res = runner.invoke(app, ["review", "bulk", "--suggested-rejects", "--apply"], input="y\n")
    assert res.exit_code == 0
    assert "1 archived, 0 error(s)" in res.output
    assert _status(item) == "archived" and _row(qid)[0] is True


def test_cli_bulk_flag_fences(kb):
    from kbcli.main import app

    res = runner.invoke(app, ["review", "bulk", "--suggested-rejects", "--action", "reject"])
    assert res.exit_code == 1, "--suggested-rejects picks its own rows"
    res = runner.invoke(app, ["review", "bulk", "--action", "dismiss", "--apply"])
    assert res.exit_code == 1, "--apply belongs to --suggested-rejects mode"
    res = runner.invoke(app, ["review", "bulk"])
    assert res.exit_code == 1, "no mode chosen"
    res = runner.invoke(app, ["review", "bulk", "--suggested-rejects"])
    assert res.exit_code == 0 and "no pending reject suggestions" in res.output


# --- untriaged pool gauge (val-5) --------------------------------------------


def test_untriaged_pool_size_gauge(kb):
    _a, _qa = _unverified("g-a", project="p")  # never examined
    _b, qb = _unverified("g-b", project="p")
    _triage(qb, "keep")  # currently triaged -> out of the pool
    _c, qc = _unverified("g-c", project="p")
    _triage(qc, "reject_duplicate", dup_of=None)
    with db.transaction() as conn:  # simulate the dead-target re-queue: verdict aside
        conn.execute(
            "UPDATE review_queue SET detail = (detail - 'triage') "
            "|| jsonb_build_object('triage_prior', detail->'triage') WHERE id = %s",
            (qc,),
        )
    counts = review_auto.curation_counts(0)
    assert counts["untriaged_pool_size"] == 2, "the never-examined row + the re-queued row"
    assert counts["never_triaged_open"] == 1, "triage_prior rows were examined once"
    # val-5 keeps the counter NAMES stable; only the docs + the new gauge landed.
    for key in ("eligible_entered_1d", "eligible_left_1d", "eligible_flow_net_1d"):
        assert key in counts

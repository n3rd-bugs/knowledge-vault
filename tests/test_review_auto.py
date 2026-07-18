"""WP-1 review-queue automation (owner decision 2026-07-18): moot sweep,
quiet-expiry, dismiss, and bulk drain. Everything asserted here is a status
flip (Invariant 5) — rows close, items keep their body/status/trust unless a
human primitive (promote/reject) was explicitly batched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from kbcore import consolidation, db, review_auto
from kbcore import review as review_mod
from kbcore.config import settings
from kbcore.errors import InvalidOperation, NotFound
from kbcore.ingest import ingest
from kbcore.items import get_item, log_access
from kbcore.review import queue_review
from kbcore.supersede import supersede

runner = CliRunner()


def _row(queue_id: int) -> tuple[bool, dict]:
    with db.connect() as conn:
        r = conn.execute(
            "SELECT resolved, detail FROM review_queue WHERE id = %s", (queue_id,)
        ).fetchone()
    return bool(r[0]), (r[1] if isinstance(r[1], dict) else {})


def _unverified(project: str, tag: str) -> tuple[int, int]:
    """One unverified item + its unverified_extraction review row (the WP-1 shape)."""
    item_id = ingest(project, "note", f"note {tag}", f"body {tag}", trust="unverified", uri=tag)
    return item_id, queue_review(item_id, "unverified_extraction", {"rejected_count": 0})


# --- close_moot_rows ---------------------------------------------------------


def test_moot_sweep_closes_rows_for_retired_items(kb):
    sup, q_sup = _unverified("p", "sup")
    dup, q_dup = _unverified("p", "dup")
    stale, q_stale = _unverified("p", "stale")
    _live, q_live = _unverified("p", "live")
    winner = ingest("p", "note", "winner", "the canonical body", uri="winner")

    # Retire one item per path the sweep must cover (never touching the paths
    # themselves — the sweep only reads the resulting status).
    supersede(sup, new_item_id=winner, reason="test: retired via supersede")
    assert consolidation.mark_duplicate(dup, winner) is True
    with db.transaction() as conn:  # backdate so ONLY this item goes stale (kb_test-owned)
        conn.execute(
            "UPDATE items SET created_at = now() - interval '400 days', "
            "updated_at = now() - interval '400 days' WHERE id = %s",
            (stale,),
        )
    assert consolidation.archive_stale(datetime.now(UTC) - timedelta(days=180)) == 1

    # A contradiction row for a retired item is NOT the moot sweep's business.
    q_contra = queue_review(sup, "contradiction", {"a_id": sup, "b_id": winner})

    assert review_auto.close_moot_rows() == 3
    for qid, status in ((q_sup, "superseded"), (q_dup, "duplicate"), (q_stale, "archived")):
        resolved, detail = _row(qid)
        assert resolved is True
        assert detail["auto_resolution"]["kind"] == "item_retired"
        assert detail["auto_resolution"]["item_status"] == status
        assert detail["rejected_count"] == 0  # jsonb merge preserved prior detail
    assert _row(q_live)[0] is False  # active item: row stays open
    assert _row(q_contra)[0] is False
    assert review_auto.close_moot_rows() == 0  # idempotent


# --- expire_quiet_rows -------------------------------------------------------


def test_quiet_expiry_closes_only_old_unrecalled_rows(kb):
    quiet, q_quiet = _unverified("p", "quiet")
    used, q_used = _unverified("p", "used")
    _young, q_young = _unverified("p", "young")
    with db.transaction() as conn:  # backdate the review rows (kb_test-owned)
        conn.execute(
            "UPDATE review_queue SET created_at = now() - interval '60 days' WHERE id = ANY(%s)",
            ([q_quiet, q_used],),
        )
    log_access(used, "kb_get")  # this item WAS recalled — its row must survive

    assert review_auto.expire_quiet_rows(0) == 0  # kill switch: DB untouched
    assert _row(q_quiet)[0] is False

    assert review_auto.expire_quiet_rows(30) == 1
    resolved, detail = _row(q_quiet)
    assert resolved is True
    assert detail["auto_resolution"]["kind"] == "quiet_expiry"
    assert detail["auto_resolution"]["age_days"] == 30
    assert _row(q_used)[0] is False  # recalled at least once → kept for review
    assert _row(q_young)[0] is False  # too young

    item = get_item(quiet)  # the ITEM stays active + unverified (still searchable)
    assert item.status == "active" and item.trust == "unverified"


# --- review.dismiss ----------------------------------------------------------


def test_dismiss_closes_row_and_itemless_row_item_untouched(kb):
    item, q = _unverified("p", "d1")
    review_mod.dismiss(q)
    resolved, detail = _row(q)
    assert resolved is True and detail["auto_resolution"]["kind"] == "dismissed"
    got = get_item(item)
    assert got.status == "active" and got.trust == "unverified"  # item untouched

    # The item-less sentinel row (previously undismissable by any tool).
    q_sentinel = queue_review(None, "search_regression", {"note": "hit@5 dropped"})
    review_mod.dismiss(q_sentinel)
    assert _row(q_sentinel)[0] is True

    with pytest.raises(InvalidOperation):
        review_mod.dismiss(q)  # double-dismiss
    with pytest.raises(NotFound):
        review_mod.dismiss(999999)


# --- bulk_resolve ------------------------------------------------------------


def test_bulk_resolve_actions_and_per_row_isolation(kb):
    a, qa = _unverified("p", "a")
    b, qb = _unverified("p", "b")
    c, qc = _unverified("p", "c")

    assert review_auto.bulk_resolve("promote", queue_ids=[qa]) == [(qa, f"promoted item {a}")]
    assert get_item(a).trust == "verified"

    assert review_auto.bulk_resolve("reject", queue_ids=[qb]) == [
        (qb, f"rejected item {b} (archived)")
    ]
    assert get_item(b).status == "archived"

    # Per-row isolation: the already-resolved id errors, the fresh one still lands.
    out = dict(review_auto.bulk_resolve("dismiss", queue_ids=[qa, qc]))
    assert out[qa].startswith("error:")
    assert out[qc] == "dismissed" and _row(qc)[0] is True
    got = get_item(c)
    assert got.status == "active" and got.trust == "unverified"

    # A missing id is a per-row error, never an exception aborting the batch.
    assert review_auto.bulk_resolve("dismiss", queue_ids=[999999]) == [
        (999999, "error: review queue row 999999 does not exist")
    ]


def test_bulk_resolve_project_selector_and_refusals(kb):
    _p_item, q_p = _unverified("p", "p-row")
    _o_item, q_other = _unverified("other", "other-row")

    out = review_auto.bulk_resolve("dismiss", project="other")
    assert out == [(q_other, "dismissed")]
    assert _row(q_other)[0] is True and _row(q_p)[0] is False  # only 'other' swept

    with pytest.raises(InvalidOperation):
        review_auto.bulk_resolve("dismiss")  # no selector: never blanket the queue
    with pytest.raises(InvalidOperation):
        review_auto.bulk_resolve("obliterate", queue_ids=[q_p])  # unknown action


# --- CLI ---------------------------------------------------------------------


def test_cli_dismiss_and_bulk(kb):
    from kbcli.main import app

    item, q = _unverified("p", "cli1")
    _item2, q2 = _unverified("p", "cli2")

    res = runner.invoke(app, ["review", "dismiss", str(q)])
    assert res.exit_code == 0 and str(q) in res.stdout
    assert _row(q)[0] is True
    assert get_item(item).trust == "unverified"

    res = runner.invoke(app, ["review", "bulk", "--action", "dismiss", "--ids", f"{q},{q2}"])
    assert res.exit_code == 0
    assert f"[{q}] error" in res.stdout  # q already resolved → per-row error line
    assert f"[{q2}] dismissed" in res.stdout
    assert "1 resolved, 1 error(s), 2 row(s)" in res.stdout
    assert _row(q2)[0] is True

    res = runner.invoke(app, ["review", "bulk", "--action", "dismiss"])
    assert res.exit_code == 1  # no selector refused


# --- worker wiring smoke -----------------------------------------------------


def test_worker_steps_run_clean_on_empty_queue(kb):
    from kbworker import consolidate as worker

    # main() wires exactly these functions (do not run main() — it takes the
    # advisory lock and would write a report); the functions must no-op cleanly.
    assert worker.review_auto is review_auto
    assert review_auto.close_moot_rows() == 0
    assert review_auto.expire_quiet_rows(settings.kb_review_expire_days) == 0

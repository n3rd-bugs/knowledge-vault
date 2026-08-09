"""P0-A — quiet expiry on rails (owner amendment 2026-08-08, amending KB 957).

``expire_quiet_rows`` was ONE uncapped UPDATE with no dry run, no undo and no
stop gate, days away from its first real firing against a ~6,600-row pool. It
now has the rails its sibling ``review_apply`` has, plus the gate the owner
approved: an UNTRIAGED row can never quiet-expire — it stays open as visible
queue depth instead of closing unexamined.

Real DB (kb_test via the ``kb`` fixture); Ollama is never called (triage
suggestions are written straight through ``review_triage.store_triage``, the
production shape). Everything asserted is a status flip on the ROW (Invariant
5): no item is ever touched, nothing is deleted, no trust changes.

The two pre-existing exemptions — reject_* suggestions and the access_log
clause — are REGRESSION-PINNED here as well as in test_curation_loop.py: the
amendment adds a gate, it does not relax either of them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from kbcore import db, review_auto, review_expire, review_triage
from kbcore.config import settings
from kbcore.errors import InvalidOperation
from kbcore.ingest import ingest
from kbcore.items import log_access
from kbcore.review import queue_review
from kbworker import tail_steps

runner = CliRunner()


# --- helpers -----------------------------------------------------------------


def _unverified(tag: str, project: str = "qx") -> tuple[int, int]:
    """One unverified item + its open unverified_extraction row (the WP-1 shape)."""
    item_id = ingest(project, "note", f"note {tag}", f"body {tag}", trust="unverified", uri=tag)
    return item_id, queue_review(item_id, "unverified_extraction", {"rejected_count": 0})


def _triage(qid: int, action: str = "keep") -> None:
    assert review_triage.store_triage(qid, {"action": action, "dup_of": None, "rationale": "r"})


def _age(qid: int, days: int) -> None:
    """Backdate a review row (kb_test-owned data) instead of waiting 30 days."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE review_queue SET created_at = now() - make_interval(days => %s) WHERE id = %s",
            (days, qid),
        )


def _row(qid: int) -> tuple[bool, dict]:
    with db.connect() as conn:
        r = conn.execute(
            "SELECT resolved, detail FROM review_queue WHERE id = %s", (qid,)
        ).fetchone()
    return bool(r[0]), (r[1] if isinstance(r[1], dict) else {})


def _open(qid: int) -> bool:
    return not _row(qid)[0]


def _aged_triaged(tag: str, days: int, action: str = "keep") -> tuple[int, int]:
    """The canonical expirable row: old, triaged, never recalled."""
    item_id, qid = _unverified(tag)
    _triage(qid, action)
    _age(qid, days)
    return item_id, qid


@pytest.fixture(autouse=True)
def _expiry_defaults(monkeypatch):
    """Pin the knobs so a live .env cannot decide what these tests measure."""
    monkeypatch.setattr(settings, "kb_review_expire_days", 30)
    monkeypatch.setattr(settings, "kb_review_expire_cap", 500)


# --- rail 1: the cap turns the cliff into a ramp ------------------------------


def test_cap_closes_the_oldest_rows_first_and_leaves_the_rest(kb):
    qids = [_aged_triaged(f"cap{n}", days)[1] for n, days in enumerate((90, 80, 70, 60, 50))]
    oldest, second, *younger = qids

    out = review_expire.expire_quiet_rows(30, cap=2)

    assert (out["expired"], out["eligible"], out["eligible_remaining"]) == (2, 5, 3)
    assert not _open(oldest) and not _open(second), "the two OLDEST rows must go first"
    assert all(_open(q) for q in younger), "the cap must leave the younger rows open"

    # The next run picks up exactly where this one stopped — a ramp, not a cliff.
    assert review_expire.expire_quiet_rows(30, cap=2)["expired"] == 2
    assert _open(younger[-1]) and not _open(younger[0])
    last = review_expire.expire_quiet_rows(30, cap=2)
    assert (last["expired"], last["eligible"], last["eligible_remaining"]) == (1, 1, 0)
    assert [r["id"] for r in last["sample"]] == [younger[-1]]
    assert review_expire.expire_quiet_rows(30, cap=2)["expired"] == 0, "the pool is drained"


def test_cap_defaults_to_the_knob_and_zero_means_uncapped(kb, monkeypatch):
    qids = [_aged_triaged(f"knob{n}", 60)[1] for n in range(4)]

    monkeypatch.setattr(settings, "kb_review_expire_cap", 1)
    assert review_expire.expire_quiet_rows(30)["expired"] == 1, "cap=None reads the setting"
    assert sum(_open(q) for q in qids) == 3

    # 0 is UNCAPPED (the kill switch is kb_review_expire_days=0, never the cap).
    monkeypatch.setattr(settings, "kb_review_expire_cap", 0)
    out = review_expire.expire_quiet_rows(30)
    assert (out["expired"], out["eligible_remaining"]) == (3, 0)
    assert not any(_open(q) for q in qids)


def test_kill_switch_and_age_floor_still_hold(kb):
    _item, qid = _aged_triaged("floor", 400)
    assert review_expire.expire_quiet_rows(0)["expired"] == 0, "0 days is still the kill switch"
    assert _open(qid)
    _fresh_item, fresh = _unverified("fresh")
    _triage(fresh)
    assert review_expire.expire_quiet_rows(30)["expired"] == 1, "only the aged row expires"
    assert _open(fresh), "a fresh row is still too young"


# --- rail 2: the dry run ------------------------------------------------------


def test_dry_run_counts_right_and_writes_nothing(kb):
    item_id, qid = _aged_triaged("dry", 45)
    _second = _aged_triaged("dry2", 44)[1]

    out = review_expire.expire_quiet_rows(30, dry_run=True, cap=1)

    assert (out["expired"], out["eligible"], out["eligible_remaining"]) == (1, 2, 1)
    assert out["sample"] == [{"id": qid, "item_id": item_id, "project": "qx", "age_days": 45}]
    assert _open(qid) and _open(_second), "a dry run must not close anything"
    assert _row(qid)[1].get("auto_resolution") is None, "a dry run must not stamp anything"


def test_dry_run_sample_is_bounded_and_oldest_first(kb):
    qids = [_aged_triaged(f"s{n}", 100 - n)[1] for n in range(review_expire.SAMPLE_SIZE + 3)]
    out = review_expire.expire_quiet_rows(30, dry_run=True, cap=0)
    assert out["expired"] == len(qids)
    assert [r["id"] for r in out["sample"]] == qids[: review_expire.SAMPLE_SIZE]


# --- rail 3: THE UNTRIAGED GATE (the KB-957 amendment) ------------------------


def test_an_untriaged_row_never_expires_while_its_triaged_twin_does(kb):
    _t_item, triaged = _unverified("twin-triaged")
    _u_item, untriaged = _unverified("twin-untriaged")
    for qid in (triaged, untriaged):
        _age(qid, 400)  # far past ANY threshold, and never recalled
    _triage(triaged, "keep")

    out = review_expire.expire_quiet_rows(30)

    assert out["expired"] == 1 and out["eligible"] == 1
    assert not _open(triaged), "an examined row still expires"
    assert _open(untriaged), "an UNTRIAGED row must stay open as visible queue depth"
    # Not a one-shot: it stays open on every later run, at any age.
    assert review_expire.expire_quiet_rows(1)["expired"] == 0
    assert _open(untriaged)


def test_the_gate_releases_a_row_the_moment_it_is_triaged(kb):
    _item, qid = _unverified("late-triage")
    _age(qid, 400)
    assert review_expire.expire_quiet_rows(30)["expired"] == 0
    _triage(qid, "keep")
    assert review_expire.expire_quiet_rows(30)["expired"] == 1
    assert not _open(qid)


# --- the two pre-existing exemptions, regression-pinned -----------------------


def test_reject_suggested_rows_are_still_exempt(kb):
    """Unchanged by the amendment: a known-bad item's evidence never quiet-closes."""
    assert set(review_expire.REJECT_TRIAGE_ACTIONS) == {"reject_noise", "reject_duplicate"}
    rejects = [_aged_triaged(f"rej-{a}", 400, a)[1] for a in review_expire.REJECT_TRIAGE_ACTIONS]
    _keep_item, keep = _aged_triaged("rej-keep", 400, "keep")

    assert review_expire.expire_quiet_rows(30)["expired"] == 1, "only the keep-triaged row"
    assert all(_open(q) for q in rejects)
    assert not _open(keep)


def test_a_recalled_item_still_protects_its_row(kb):
    """Unchanged by the amendment: one access_log row keeps the review row open."""
    used_item, used = _aged_triaged("used", 400)
    _quiet_item, quiet = _aged_triaged("quiet", 400)
    log_access(used_item, "kb_get")

    assert review_expire.expire_quiet_rows(30)["expired"] == 1
    assert _open(used), "an item that was recalled must be reviewed by a human"
    assert not _open(quiet)


def test_the_item_is_never_touched(kb):
    item_id, qid = _aged_triaged("untouched", 400)
    assert review_expire.expire_quiet_rows(30)["expired"] == 1
    with db.connect() as conn:
        status, trust = conn.execute(
            "SELECT i.status, COALESCE(s.trust, 'verified') FROM items i "
            "LEFT JOIN sources s ON s.id = i.source_id WHERE i.id = %s",
            (item_id,),
        ).fetchone()
    assert (status, trust) == ("active", "unverified"), "the item stays live and searchable"
    resolved, detail = _row(qid)
    assert resolved is True
    assert detail["auto_resolution"] == {
        "kind": "quiet_expiry",
        "age_days": 30,
        "at": detail["auto_resolution"]["at"],
    }
    assert detail["rejected_count"] == 0, "the jsonb merge preserved prior detail"


# --- rail 4: undo + the permanent veto ---------------------------------------


def test_undo_reopens_exactly_the_quiet_expiry_rows_and_vetoes_re_expiry(kb):
    _i1, expired = _aged_triaged("undo-me", 400)
    _i2, other = _aged_triaged("undo-other", 399)
    since = datetime.now(UTC) - timedelta(minutes=1)
    assert review_expire.expire_quiet_rows(30, cap=1)["expired"] == 1
    # A row closed by a DIFFERENT automation must not be reopened by this undo.
    moot_item, moot = _unverified("undo-moot")
    with db.transaction() as conn:
        conn.execute("UPDATE items SET status = 'archived' WHERE id = %s", (moot_item,))
    assert review_auto.close_moot_rows() == 1

    assert review_expire.undo_quiet_expiry(since, dry_run=True) == [expired]
    assert not _open(expired), "a dry run must not reopen anything"

    assert review_expire.undo_quiet_expiry(since) == [expired]
    assert _open(expired) and _open(other)
    assert not _open(moot), "only quiet_expiry stamps are reversed"
    assert _row(expired)[1]["auto_resolution"]["undone_at"], "the undo stamp IS the veto"

    # THE VETO: tonight's sweep must not silently re-close what the owner reopened.
    out = review_expire.expire_quiet_rows(30, cap=0)
    assert out["expired"] == 1 and out["eligible"] == 1, "only the never-undone row is eligible"
    assert _open(expired), "a vetoed row was re-expired — the undo/expiry pair loops"
    assert review_expire.undo_quiet_expiry(since) == [other], "already-undone rows are not redone"


def test_undo_respects_since_and_requires_an_aware_datetime(kb):
    _i1, old = _aged_triaged("since-old", 400)
    assert review_expire.expire_quiet_rows(30)["expired"] == 1
    cutoff = datetime.now(UTC) + timedelta(seconds=1)
    assert review_expire.undo_quiet_expiry(cutoff) == [], "stamped before --since: untouched"
    assert not _open(old)

    with pytest.raises(InvalidOperation):
        review_expire.undo_quiet_expiry(datetime.now())  # noqa: DTZ005 — the point of the test


# --- the report: never_triaged_open ------------------------------------------


def test_never_triaged_open_counts_only_open_untriaged_extraction_rows(kb):
    _i1, untriaged_a = _unverified("nt-a")
    _i2, _untriaged_b = _unverified("nt-b")
    _i3, triaged = _unverified("nt-triaged")
    _triage(triaged)
    _i4, closed = _unverified("nt-closed")
    _age(closed, 400)
    _triage(closed)
    queue_review(None, "search_regression", {"note": "not an extraction row"})

    assert review_auto.curation_counts(30)["never_triaged_open"] == 2
    assert review_expire.expire_quiet_rows(30)["expired"] == 1, "the closed one was triaged"
    counts = review_auto.curation_counts(30)
    assert counts["never_triaged_open"] == 2, "expiry cannot lower the backlog gauge any more"
    assert counts["triage_awaiting_apply"] == 1

    review_auto.bulk_resolve("dismiss", queue_ids=[untriaged_a])
    assert review_auto.curation_counts(30)["never_triaged_open"] == 1, "resolving one drains it"


def test_never_triaged_open_excludes_rows_a_re_queue_already_examined(kb):
    """GAUGE HONESTY (review wfulfrf33). auto-apply's dead-target re-queue moves the
    stored verdict to ``triage_prior`` and drops ``triage``, so a row the 14B HAD
    examined started counting as never-triaged again — inflating the one backlog
    number the nightly digest carries, and inflating it a little more every night
    the re-queue fires. A row waiting for a SECOND look is not an unexamined row."""
    from kbcore import review_apply_rows

    _i1, requeued = _unverified("gauge-requeued")
    _i2, _untouched = _unverified("gauge-untouched")
    assert review_auto.curation_counts(30)["never_triaged_open"] == 2

    _triage(requeued, "reject_duplicate")
    assert review_auto.curation_counts(30)["never_triaged_open"] == 1

    assert review_apply_rows.invalidate_dead_dup(requeued), "the production re-queue shape"
    counts = review_auto.curation_counts(30)
    assert counts["never_triaged_open"] == 1, "a re-queued row WAS examined"
    assert counts["triage_awaiting_apply"] == 0, "and it carries no verdict to apply either"
    assert _open(requeued), "the row itself is still open — only the gauge changed"


def test_first_expiry_date_tracks_the_rows_that_can_actually_close(kb):
    _i1, untriaged = _unverified("fe-untriaged")
    _age(untriaged, 20)  # oldest row, but the gate holds it open forever
    assert review_auto.curation_counts(30)["first_expiry_date"] is None

    _i2, triaged = _unverified("fe-triaged")
    _age(triaged, 5)
    _triage(triaged)
    with db.connect() as conn:
        expected = conn.execute(
            "SELECT (created_at + make_interval(days => 30))::date::text "
            "FROM review_queue WHERE id = %s",
            (triaged,),
        ).fetchone()[0]
    assert review_auto.curation_counts(30)["first_expiry_date"] == expected


# --- the worker: stop gate + digest ------------------------------------------


def test_the_expiry_step_stands_down_with_the_rest_of_the_run(kb):
    from kbworker import consolidate

    _item, qid = _aged_triaged("stopgate", 400)
    consolidate._STOP_LOG.clear()

    assert tail_steps.quiet_expire_step({}, lambda: "battery") == 0
    assert _open(qid), "expiry ran in a run that was already standing down"
    assert consolidate._STOP_LOG["review_expired"]["reason"] == "battery"

    counts: dict = {}
    assert tail_steps.quiet_expire_step(counts, lambda: None) == 1
    assert not _open(qid)
    assert counts["quiet_expiry_counts"] == {"expired": 1, "eligible": 1, "eligible_remaining": 0}


def test_the_digest_shows_expiry_and_backlog_only_when_positive():
    lines = tail_steps.digest_lines(
        {"queued_drained": 1, "review_expired": 7, "never_triaged_open": 4200}
    )
    assert "expired: 7" in lines
    assert "never-triaged open: 4200" in lines
    assert all(line.isascii() for line in lines)

    for counts in (
        {"queued_drained": 1, "review_expired": 0, "never_triaged_open": 0},
        {"queued_drained": 1},
    ):
        quiet = tail_steps.digest_lines(counts)
        assert not any(line.startswith(("expired:", "never-triaged open:")) for line in quiet)


# --- the CLI ------------------------------------------------------------------


def test_cli_expire_defaults_to_a_dry_run_then_applies_on_confirmation(kb):
    from kbcli.main import app

    _item, qid = _aged_triaged("cli", 400)

    res = runner.invoke(app, ["review", "expire"])
    assert res.exit_code == 0, res.output
    assert "would expire now: 1" in res.stdout and f"[{qid}]" in res.stdout
    assert _open(qid), "the default path must not write"

    res = runner.invoke(app, ["review", "expire", "--apply"], input="n\n")
    assert res.exit_code == 1 and _open(qid), "declining the prompt writes nothing"

    res = runner.invoke(app, ["review", "expire", "--apply"], input="y\n")
    assert res.exit_code == 0 and "expired 1 row(s)" in res.stdout
    assert not _open(qid)


def test_cli_undo_expiry_round_trip(kb):
    from kbcli.main import app

    _item, qid = _aged_triaged("cli-undo", 400)
    since = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    assert review_expire.expire_quiet_rows(30)["expired"] == 1

    res = runner.invoke(app, ["review", "undo-expiry", "--since", "not-a-date"])
    assert res.exit_code == 1
    res = runner.invoke(app, ["review", "undo-expiry", "--since", "2026-08-19T00:00:00"])
    assert res.exit_code == 1, "a naive timestamp is refused"

    res = runner.invoke(app, ["review", "undo-expiry", "--since", since, "--dry-run"])
    assert res.exit_code == 0 and f"would reopen 1 quiet-expired row(s): {qid}" in res.stdout
    assert not _open(qid)

    res = runner.invoke(app, ["review", "undo-expiry", "--since", since], input="y\n")
    assert res.exit_code == 0 and f"reopened 1 quiet-expired row(s): {qid}" in res.stdout
    assert _open(qid)

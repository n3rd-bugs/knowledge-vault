"""Automated reject-apply with safety rails + the nightly digest push.

Owner decision 2026-08-02 (kbcore.review_apply): the human apply loop was dead
15 days while 853 reject-suggested items sat live in retrieval, so the machine
now applies reject_noise / reject_duplicate suggestions — behind five rails,
each pinned individually here. Real DB (kb_test via the ``kb`` fixture); the
LLM is never called (triage suggestions are written directly, production-shaped
via review_triage.store_triage), and the only network in the file is a POST at
a dead port to prove push_digest cannot raise.

Everything asserted is a status flip (Invariant 5): nothing is deleted, no
trust is ever raised, and 'keep' is never touched.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from typer.testing import CliRunner

from kbcore import db, notify, review_apply, review_apply_rails, review_triage
from kbcore import review as review_mod
from kbcore.config import settings
from kbcore.errors import InvalidOperation
from kbcore.ingest import ingest
from kbcore.items import log_access
from kbcore.review import queue_review

runner = CliRunner()


# --- helpers -----------------------------------------------------------------


def _unverified(tag: str, project: str = "p") -> tuple[int, int]:
    """One unverified item + its open unverified_extraction row (the WP-1 shape)."""
    item_id = ingest(project, "note", f"note {tag}", f"body {tag}", trust="unverified", uri=tag)
    return item_id, queue_review(item_id, "unverified_extraction", {"rejected_count": 0})


def _triage(qid: int, action: str, dup_of: int | None = None, days_old: float = 10) -> None:
    """Store a PRODUCTION-SHAPED triage suggestion, then backdate its stamp.

    Backdating the stored ``at`` (kb_test-owned data) is how the cooling-off rail
    is exercised without waiting three days.
    """
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
    monkeypatch.setattr(settings, "kb_auto_apply_cap", 300)


# --- rail 1: cooling-off -----------------------------------------------------


def test_cooling_rail_holds_fresh_suggestions_and_fails_closed(kb):
    aged, q_aged = _unverified("aged")
    fresh, q_fresh = _unverified("fresh")
    _stampless, q_stampless = _unverified("stampless")
    _triage(q_aged, "reject_noise", days_old=3)  # exactly at the boundary -> applies
    _triage(q_fresh, "reject_noise", days_old=2)  # inside the window -> held
    _triage(q_stampless, "reject_noise")
    with db.transaction() as conn:  # a suggestion with NO 'at' must FAIL CLOSED
        conn.execute(
            "UPDATE review_queue SET detail = detail #- '{triage,at}' WHERE id = %s",
            (q_stampless,),
        )

    counts = review_apply.auto_apply_rejects()
    assert counts["applied_noise"] == 1
    assert counts["skipped_cooling"] == 2
    assert _status(aged) == "archived" and _row(q_aged)[0] is True
    assert _status(fresh) == "active" and _row(q_fresh)[0] is False
    assert _row(q_stampless)[0] is False


def test_cooling_days_is_configurable(kb, monkeypatch):
    item, qid = _unverified("a")
    _triage(qid, "reject_noise", days_old=5)
    monkeypatch.setattr(settings, "kb_auto_apply_cooling_days", 30)
    assert review_apply.auto_apply_rejects()["skipped_cooling"] == 1
    assert _status(item) == "active"
    monkeypatch.setattr(settings, "kb_auto_apply_cooling_days", 1)
    assert review_apply.auto_apply_rejects()["applied_noise"] == 1
    assert _status(item) == "archived"


# --- rail 2: access exclusion ------------------------------------------------


def test_recently_accessed_items_are_left_for_the_human(kb):
    used, q_used = _unverified("used")
    old_use, q_old = _unverified("old-use")
    _triage(q_used, "reject_noise")
    _triage(q_old, "reject_noise")
    log_access(used, "kb_get")
    log_access(old_use, "kb_search")
    with db.transaction() as conn:  # push the second item's recall outside the window
        conn.execute(
            "UPDATE access_log SET ts = now() - interval '90 days' WHERE item_id = %s",
            (old_use,),
        )

    counts = review_apply.auto_apply_rejects()
    assert counts["skipped_accessed"] == 1
    assert counts["applied_noise"] == 1  # a recall 90 days ago does not protect it
    assert _status(used) == "active" and _row(q_used)[0] is False
    assert _status(old_use) == "archived"


# --- rail 3: dup-target liveness --------------------------------------------


def test_duplicate_applies_against_a_live_canonical(kb):
    canonical = ingest("p", "note", "canonical", "the surviving body", uri="canon")
    dup, qid = _unverified("dup")
    _triage(qid, "reject_duplicate", dup_of=canonical)

    counts = review_apply.auto_apply_rejects()
    assert counts == {
        "applied_noise": 0,
        "applied_duplicate": 1,
        "skipped_accessed": 0,
        "skipped_cooling": 0,
        "skipped_target_gone": 0,
        "eligible_remaining": 0,
    }
    assert _status(dup) == "duplicate"  # NOT archived — the manual path's flip
    assert _status(canonical) == "active"
    resolved, detail = _row(qid)
    assert resolved is True
    assert detail["auto_resolution"]["dup_of"] == canonical
    with db.connect() as conn:  # mark_duplicate's audit edge rode along
        assert (
            conn.execute(
                "SELECT count(*) FROM relations WHERE src_id = %s AND rel = 'derived_from' "
                "AND dst_id = %s",
                (dup, canonical),
            ).fetchone()[0]
            == 1
        )


def test_dead_or_missing_dup_targets_are_skipped(kb):
    gone = ingest("p", "note", "gone", "retired canonical", uri="gone")
    with db.transaction() as conn:
        conn.execute("UPDATE items SET status = 'archived' WHERE id = %s", (gone,))
    a, qa = _unverified("a")
    b, qb = _unverified("b")
    c, qc = _unverified("c")
    _triage(qa, "reject_duplicate", dup_of=gone)  # target not active
    _triage(qb, "reject_duplicate", dup_of=10_000_000)  # target does not exist
    _triage(qc, "reject_duplicate", dup_of=None)  # no target at all

    counts = review_apply.auto_apply_rejects()
    assert counts["skipped_target_gone"] == 3
    assert counts["applied_duplicate"] == 0
    for item, qid in ((a, qa), (b, qb), (c, qc)):
        assert _status(item) == "active" and _row(qid)[0] is False


def test_summary_targets_and_self_reference_are_refused(kb):
    summ = ingest("p", "summary", "rollup", "a consolidation summary", uri="summ")
    a, qa = _unverified("a")
    b, qb = _unverified("b")
    _triage(qa, "reject_duplicate", dup_of=summ)  # unstable canonical
    _triage(qb, "reject_duplicate", dup_of=b)  # duplicate of itself

    counts = review_apply.auto_apply_rejects()
    assert counts["skipped_target_gone"] == 2 and counts["applied_duplicate"] == 0
    assert _status(a) == "active" and _status(b) == "active"


def test_reciprocal_pair_never_retires_both_ends(kb):
    """KB gotcha 958: A dup-of B AND B dup-of A must not retire BOTH."""
    a, qa = _unverified("a")  # ingested first -> oldest -> the item_ranks canonical
    b, qb = _unverified("b")
    _triage(qa, "reject_duplicate", dup_of=b)
    _triage(qb, "reject_duplicate", dup_of=a)

    counts = review_apply.auto_apply_rejects()
    assert counts["applied_duplicate"] == 1
    assert counts["skipped_target_gone"] == 1
    assert _status(a) == "active"  # the cluster canonical survives
    assert _status(b) == "duplicate"
    assert _row(qa)[0] is False  # the survivor's own row stays open for a human
    assert _row(qb)[1]["auto_resolution"]["dup_of"] == a


# --- rail 4: state guard (raced by a human) ----------------------------------


def test_rows_raced_by_a_human_are_skipped_silently(kb, monkeypatch):
    """A human acting between selection and apply wins, and is counted NOWHERE."""
    keep_item, q_keep = _unverified("raced-row")
    promoted, q_promoted = _unverified("raced-trust")
    archived, q_archived = _unverified("raced-status")
    survivor, q_survivor = _unverified("survivor")
    for qid in (q_keep, q_promoted, q_archived, q_survivor):
        _triage(qid, "reject_noise")

    real = review_apply.eligible_rows

    def race(cooling_days, counts):
        rows = real(cooling_days, counts)
        review_mod.dismiss(q_keep)  # human closed the row
        review_mod.promote(q_promoted)  # human raised trust (row closes too)
        with db.transaction() as conn:  # human archived the item another way
            conn.execute("UPDATE items SET status = 'archived' WHERE id = %s", (archived,))
        return rows

    monkeypatch.setattr(review_apply, "eligible_rows", race)
    counts = review_apply.auto_apply_rejects()
    assert counts["applied_noise"] == 1  # only the survivor
    assert counts["skipped_cooling"] == 0
    assert counts["skipped_accessed"] == 0
    assert counts["skipped_target_gone"] == 0
    assert _status(keep_item) == "active"
    assert _status(promoted) == "active"
    assert _status(survivor) == "archived"
    # The human's own stamp stands: the machine never overwrote it with its own.
    assert _row(q_keep)[1]["auto_resolution"]["kind"] == "dismissed"
    assert "auto_resolution" not in _row(q_archived)[1]  # never stamped by the machine
    assert _row(q_archived)[0] is False  # left open for the moot sweep


def test_verified_knowledge_is_out_of_scope(kb):
    """Only unverified extractions are auto-appliable; trust is never lowered either."""
    item = ingest("p", "note", "verified", "trusted body", uri="v")
    qid = queue_review(item, "unverified_extraction", {})
    _triage(qid, "reject_noise")
    counts = review_apply.auto_apply_rejects()
    assert counts["applied_noise"] == 0 and counts["eligible_remaining"] == 0
    assert _status(item) == "active" and _row(qid)[0] is False


# --- 'keep' is never auto-applied (regression pin) ---------------------------


def test_keep_suggestions_are_never_touched(kb):
    item, qid = _unverified("keeper")
    _triage(qid, "keep", days_old=400)
    counts = review_apply.auto_apply_rejects()
    assert counts == dict.fromkeys(review_apply_rails.COUNT_KEYS, 0)
    assert _status(item) == "active" and _row(qid)[0] is False
    assert "keep" not in review_apply_rails.AUTO_APPLY_ACTIONS


# --- cap + eligible_remaining ------------------------------------------------


def test_cap_bounds_the_run_and_reports_the_remainder(kb):
    items = []
    for n in range(5):
        item, qid = _unverified(f"n{n}")
        _triage(qid, "reject_noise")
        items.append(item)

    first = review_apply.auto_apply_rejects(cap=2)
    assert first["applied_noise"] == 2 and first["eligible_remaining"] == 3
    assert [_status(i) for i in items] == ["archived", "archived", "active", "active", "active"]

    second = review_apply.auto_apply_rejects(cap=2)
    assert second["applied_noise"] == 2 and second["eligible_remaining"] == 1
    third = review_apply.auto_apply_rejects()  # config default 300
    assert third["applied_noise"] == 1 and third["eligible_remaining"] == 0
    assert all(_status(i) == "archived" for i in items)
    assert review_apply.auto_apply_rejects()["applied_noise"] == 0  # idempotent


def test_cap_defaults_to_config_and_zero_applies_nothing(kb, monkeypatch):
    item, qid = _unverified("a")
    _triage(qid, "reject_noise")
    assert review_apply.auto_apply_rejects(cap=0) == {
        "applied_noise": 0,
        "applied_duplicate": 0,
        "skipped_accessed": 0,
        "skipped_cooling": 0,
        "skipped_target_gone": 0,
        "eligible_remaining": 1,
    }
    assert _status(item) == "active"
    monkeypatch.setattr(settings, "kb_auto_apply_cap", 1)
    assert review_apply.auto_apply_rejects()["applied_noise"] == 1


def test_kill_switch_returns_zeros_without_touching_the_db(kb, monkeypatch):
    item, qid = _unverified("a")
    _triage(qid, "reject_noise")
    monkeypatch.setattr(settings, "kb_auto_apply_enabled", False)
    monkeypatch.setattr(review_apply, "eligible_rows", _no_db)
    assert review_apply.auto_apply_rejects() == dict.fromkeys(review_apply_rails.COUNT_KEYS, 0)
    assert _status(item) == "active" and _row(qid)[0] is False


def _no_db(*_a, **_kw):
    raise AssertionError("kill switch must not query the database")


# --- dry run -----------------------------------------------------------------


def test_dry_run_reports_but_changes_nothing(kb):
    canonical = ingest("p", "note", "canonical", "surviving body", uri="canon")
    noise, q_noise = _unverified("noise")
    dup, q_dup = _unverified("dup")
    fresh, q_fresh = _unverified("fresh")
    _triage(q_noise, "reject_noise")
    _triage(q_dup, "reject_duplicate", dup_of=canonical)
    _triage(q_fresh, "reject_noise", days_old=0)

    counts = review_apply.auto_apply_rejects(dry_run=True)
    assert counts["applied_noise"] == 1 and counts["applied_duplicate"] == 1
    assert counts["skipped_cooling"] == 1
    for item, qid in ((noise, q_noise), (dup, q_dup), (fresh, q_fresh)):
        assert _status(item) == "active"
        resolved, detail = _row(qid)
        assert resolved is False and "auto_resolution" not in detail


# --- rail 5: the stamp -------------------------------------------------------


def test_applied_rows_carry_a_full_undo_stamp(kb):
    canonical = ingest("p", "note", "canonical", "surviving body", uri="canon")
    _noise, q_noise = _unverified("noise")
    _dup, q_dup = _unverified("dup")
    _triage(q_noise, "reject_noise")
    _triage(q_dup, "reject_duplicate", dup_of=canonical)
    before = datetime.now(UTC)

    review_apply.auto_apply_rejects()

    noise_stamp = _row(q_noise)[1]["auto_resolution"]
    dup_stamp = _row(q_dup)[1]["auto_resolution"]
    assert set(noise_stamp) == {"kind", "action", "at", "cooling_days", "batch"}
    assert set(dup_stamp) == {"kind", "action", "at", "cooling_days", "batch", "dup_of"}
    assert noise_stamp["kind"] == dup_stamp["kind"] == "auto_applied_reject"
    assert noise_stamp["action"] == "reject_noise"
    assert dup_stamp["action"] == "reject_duplicate" and dup_stamp["dup_of"] == canonical
    assert noise_stamp["cooling_days"] == 3
    assert noise_stamp["batch"] == dup_stamp["batch"]  # one run stamp for the batch
    assert datetime.fromisoformat(noise_stamp["at"]) >= before
    assert _row(q_noise)[1]["rejected_count"] == 0  # jsonb merge kept prior detail
    assert _row(q_noise)[1]["triage"]["action"] == "reject_noise"


# --- rail 6: undo ------------------------------------------------------------


def test_undo_restores_items_reopens_rows_and_vetoes_reapply(kb):
    canonical = ingest("p", "note", "canonical", "surviving body", uri="canon")
    noise, q_noise = _unverified("noise")
    dup, q_dup = _unverified("dup")
    _triage(q_noise, "reject_noise")
    _triage(q_dup, "reject_duplicate", dup_of=canonical)
    since = datetime.now(UTC) - timedelta(seconds=1)
    assert review_apply.auto_apply_rejects()["applied_noise"] == 1

    assert review_apply.undo_auto_applied(since, dry_run=True) == [q_noise, q_dup]
    assert _status(noise) == "archived"  # dry run wrote nothing

    assert review_apply.undo_auto_applied(since) == [q_noise, q_dup]
    assert _status(noise) == "active" and _status(dup) == "active"
    for qid in (q_noise, q_dup):
        resolved, detail = _row(qid)
        assert resolved is False  # row reopened
        assert detail["auto_resolution"]["undone_at"]  # stamp annotated
        assert detail["auto_resolution"]["kind"] == "auto_applied_reject"  # history kept

    # THE VETO: tonight's run must not silently re-apply what the owner reversed.
    assert review_apply.auto_apply_rejects() == dict.fromkeys(review_apply_rails.COUNT_KEYS, 0)
    assert _status(noise) == "active" and _status(dup) == "active"
    assert review_apply.undo_auto_applied(since) == []  # idempotent


def test_undo_is_bounded_by_since_and_by_the_status_it_set(kb):
    old, q_old = _unverified("old")
    later, q_later = _unverified("later")
    human, q_human = _unverified("human")
    for qid in (q_old, q_later, q_human):
        _triage(qid, "reject_noise")
    assert review_apply.auto_apply_rejects(cap=1)["applied_noise"] == 1  # only `old`
    cutoff = datetime.now(UTC)
    assert review_apply.auto_apply_rejects()["applied_noise"] == 2

    with db.transaction() as conn:  # a human retired this one further, after the fact
        conn.execute("UPDATE items SET status = 'superseded' WHERE id = %s", (human,))

    undone = review_apply.undo_auto_applied(cutoff)
    assert undone == [q_later, q_human]  # the pre-cutoff batch is untouched
    assert _status(old) == "archived"
    assert _status(later) == "active"
    assert _status(human) == "superseded"  # never resurrected from a human's status
    assert _row(q_human)[0] is False  # the row still reopens for the human to see


def test_undo_requires_an_aware_timestamp(kb):
    with pytest.raises(InvalidOperation):
        review_apply.undo_auto_applied(datetime(2026, 8, 1))  # noqa: DTZ001 — the point


# --- CLI ---------------------------------------------------------------------


def test_cli_auto_apply_and_undo_auto(kb):
    item, qid = _unverified("cli")
    _triage(qid, "reject_noise")
    from kbcli.main import app

    dry = runner.invoke(app, ["review", "auto-apply", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "would apply: 1 noise" in dry.output
    assert _status(item) == "active"

    since = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    res = runner.invoke(app, ["review", "auto-apply", "--cap", "5"])
    assert res.exit_code == 0, res.output
    assert "applied: 1 noise, 0 duplicate" in res.output
    assert "eligible remaining: 0" in res.output
    assert _status(item) == "archived"

    undo = runner.invoke(app, ["review", "undo-auto", "--since", since])
    assert undo.exit_code == 0, undo.output
    assert f"reversed 1 auto-applied row(s): {qid}" in undo.output
    assert _status(item) == "active" and _row(qid)[0] is False

    bad = runner.invoke(app, ["review", "undo-auto", "--since", "yesterday"])
    assert bad.exit_code == 1
    naive = runner.invoke(app, ["review", "undo-auto", "--since", "2026-08-01T00:00:00"])
    assert naive.exit_code == 1


# --- notify.push_digest ------------------------------------------------------


def test_push_digest_never_raises_on_connection_failure(monkeypatch, caplog):
    """A dead channel must be a False, not an exception — the nightly still ran."""
    monkeypatch.setattr(settings, "kb_notify_url", "http://127.0.0.1:1")
    monkeypatch.setattr(settings, "kb_notify_topic", "kb-nightly")
    monkeypatch.setattr(settings, "kb_notify_token", "tk_0000000000notarealtoken")
    with caplog.at_level(logging.WARNING):
        assert notify.push_digest(["auto-apply: 12 applied"]) is False
    assert "tk_0000000000notarealtoken" not in caplog.text


def test_push_digest_is_off_without_a_channel(monkeypatch):
    monkeypatch.setattr(settings, "kb_notify_url", "")
    monkeypatch.setattr(settings, "kb_notify_topic", "")

    def boom(*_a, **_kw):
        raise AssertionError("unconfigured push must not hit the network")

    monkeypatch.setattr(httpx, "post", boom)
    assert notify.push_digest(["something"]) is False
    monkeypatch.setattr(settings, "kb_notify_url", "https://ntfy.example")
    monkeypatch.setattr(settings, "kb_notify_topic", "kb-nightly")
    assert notify.push_digest([]) is False  # nothing to say
    assert notify.push_digest(["", "   "]) is False


def test_push_digest_posts_the_digest_with_a_header_token(monkeypatch):
    monkeypatch.setattr(settings, "kb_notify_url", "https://ntfy.example/")
    monkeypatch.setattr(settings, "kb_notify_topic", "kb-nightly")
    monkeypatch.setattr(settings, "kb_notify_token", "tk_0000000000notarealtoken")
    seen: dict = {}

    def fake_post(url, **kw):
        seen.update(url=url, **kw)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    assert notify.push_digest(["auto-apply: 12 applied", "backlog: 41 left"]) is True
    assert seen["url"] == "https://ntfy.example/kb-nightly"
    assert seen["content"] == b"auto-apply: 12 applied\nbacklog: 41 left"
    assert seen["headers"]["Title"] == "KB nightly"
    assert seen["headers"]["Authorization"] == "Bearer tk_0000000000notarealtoken"
    assert seen["timeout"] == notify.PUSH_TIMEOUT

    # A 4xx/5xx is a failure, not a silent success.
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kw: httpx.Response(403, request=httpx.Request("POST", url)),
    )
    assert notify.push_digest(["x"]) is False


def test_a_noise_reject_cannot_orphan_a_duplicate_in_the_same_batch(kb):
    """THE VERIFIER'S REPRODUCTION, pinned. Selection computes cluster canonicals
    up front; within one batch a reject_noise row can archive the canonical
    BEFORE the reject_duplicate row flips. Without an apply-time liveness
    re-check, A retires as a duplicate whose derived_from points at an ARCHIVED
    item — a dangling canonical no automated step ever revisits."""
    from kbcore import review_apply

    b_item, b_qid = _unverified("canonical-b")
    a_item, a_qid = _unverified("duplicate-a")
    # B is noise-rejected; A is a duplicate whose canonical is B. B's lower qid
    # orders it first in the batch, archiving the canonical before A applies.
    _triage(b_qid, "reject_noise")
    _triage(a_qid, "reject_duplicate", dup_of=b_item)

    counts = review_apply.auto_apply_rejects()

    assert _status(b_item) == "archived", "the noise reject should have applied"
    assert _status(a_item) == "active", "A was retired onto a dead canonical"
    resolved, detail = _row(a_qid)
    assert not resolved, "A's row must stay open for re-selection next run"
    assert "auto_resolution" not in detail, "the claim stamp must be released"
    assert counts["applied_duplicate"] == 0

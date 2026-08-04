"""Package H — ``kbcore.yield_hygiene`` / ``kb repair summaries|judge-flagged``.

Two owner-fired recovery-residue cleanups, both status-flip/retag only (Inv 5, no
delete; Inv 10, no promotion):

* summary-noise retag: ``Summary — lang:<tag>`` titles are superseded by a
  clean-titled, still-unverified copy;
* judge-flagged prioritization: open review rows over ``secondary-judge-failed``
  items are stamped with a ``detail->'priority'`` marker (never resolved).

Real DB in kb_test (conftest ``kb`` fixture); embeddings from conftest's stub. No
Ollama, no live schema.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from typer.testing import CliRunner

from kbcli.main import app
from kbcore import db, review, scope, yield_hygiene
from kbcore.errors import InvalidOperation
from kbcore.ingest import ingest
from kbcore.review_triage import pending_triage

runner = CliRunner()

PREFIX = yield_hygiene.SUMMARY_TITLE_PREFIX  # "Summary — " with the real U+2014 em dash
PROJECT = "kb-system"


# --- helpers -----------------------------------------------------------------


def _noise_summary(tag: str = "lang:python", *, body: str = "a summary body", trust="unverified"):
    """Seed one active ``Summary — <tag>`` noise summary. Returns its item id."""
    item_id = ingest(
        PROJECT,
        "summary",
        f"{PREFIX}{tag}",
        body,
        kind="chat",
        uri=f"summary:{PROJECT}:{tag}:{body}",
        trust=trust,
        tags=[tag],
        meta={"topic": tag, "consolidation": True},
    )
    assert item_id is not None
    return item_id


def _judge_failed(*, title="flagged", label=yield_hygiene.JUDGE_FAILED_LABEL):
    """Seed an active judge-failed item + an OPEN unverified_extraction row -> (item, qid)."""
    item_id = ingest(
        PROJECT,
        "note",
        title,
        f"body of {title}",
        kind="session",
        uri=f"jf:{title}",
        trust="unverified",
        tags=["lang:python"],
        meta={"extraction_pipeline": {"verified_by": label}},
    )
    assert item_id is not None
    qid = review.queue_review(item_id, "unverified_extraction", {"note": "seed"})
    return item_id, qid


def _one(item_id: int, col: str):
    with db.connect() as conn:
        return conn.execute(f"SELECT {col} FROM items WHERE id = %s", (item_id,)).fetchone()[0]  # noqa: S608


def _trust(item_id: int):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT s.trust FROM sources s JOIN items i ON i.source_id = s.id WHERE i.id = %s",
            (item_id,),
        ).fetchone()
    return row[0] if row else None


def _detail(qid: int):
    with db.connect() as conn:
        return conn.execute("SELECT detail FROM review_queue WHERE id = %s", (qid,)).fetchone()[0]


def _resolved(qid: int):
    with db.connect() as conn:
        return conn.execute("SELECT resolved FROM review_queue WHERE id = %s", (qid,)).fetchone()[0]


def _snapshot():
    """(item count, non-active items, relations, review rows) — the 'nothing written' probe."""
    with db.connect() as conn:
        return (
            conn.execute("SELECT count(*) FROM items").fetchone()[0],
            conn.execute("SELECT count(*) FROM items WHERE status <> 'active'").fetchone()[0],
            conn.execute("SELECT count(*) FROM relations").fetchone()[0],
            conn.execute("SELECT count(*) FROM review_queue").fetchone()[0],
        )


@pytest.fixture()
def lock_free(monkeypatch):
    """Pin the consolidation advisory lock ACQUIRED (the schema-shared key is a race)."""

    @contextmanager
    def _lock(_key: int):
        yield True

    monkeypatch.setattr(db, "advisory_lock", _lock)


def _grantee():
    return scope.Principal(
        name="grantee",
        read_projects=frozenset({PROJECT}),
        write_projects=frozenset({PROJECT}),
        caps=scope.ALL_CAPS,
    )


# --- 1. summary noise: enumerate ---------------------------------------------


def test_enumerate_finds_only_lang_noise_summaries(kb):
    noisy = _noise_summary("lang:python")
    noisy2 = _noise_summary("lang:c")
    # A clean summary title (namespace already out) and a non-summary look-alike.
    ingest(PROJECT, "summary", f"{PREFIX}python", "clean", kind="chat", uri="s:clean")
    ingest(PROJECT, "note", f"{PREFIX}lang:rust", "not a summary", kind="chat", uri="n:1")

    items = yield_hygiene.enumerate_summary_noise()

    assert {i.item_id for i in items} == {noisy, noisy2}
    by_id = {i.item_id: i for i in items}
    assert by_id[noisy].leaked_tag == "lang:python"
    assert by_id[noisy].corrected_title == f"{PREFIX}python"
    assert by_id[noisy].trust == "unverified"


def test_enumerate_writes_nothing(kb):
    _noise_summary("lang:python")
    before = _snapshot()

    yield_hygiene.enumerate_summary_noise()

    assert _snapshot() == before


# --- 2. summary noise: dry run / apply ---------------------------------------


def test_clean_dry_run_writes_nothing(kb):
    nid = _noise_summary("lang:python")
    before = _snapshot()

    outcomes = yield_hygiene.clean_summary_noise()

    assert [(o.eligible, o.superseded, o.reason) for o in outcomes] == [(True, False, "ok")]
    assert _snapshot() == before
    assert _one(nid, "status") == "active"


def test_apply_supersedes_with_a_clean_unverified_replacement(kb):
    nid = _noise_summary("lang:python", body="the python gotcha summary")

    outcomes = yield_hygiene.clean_summary_noise(dry_run=False)

    assert len(outcomes) == 1
    o = outcomes[0]
    assert (o.eligible, o.superseded, o.reason) == (True, True, "ok")
    new_id = o.new_item_id
    assert new_id is not None
    assert _one(nid, "status") == "superseded", "the noise summary is retired, not deleted"
    assert _one(nid, "superseded_by") == new_id
    assert _one(new_id, "status") == "active"
    assert _one(new_id, "title") == f"{PREFIX}python", "namespace stripped from the title"
    assert _trust(new_id) == "unverified", "Inv 10: the replacement is NOT promoted"
    with db.connect() as conn:
        tags = conn.execute("SELECT tags FROM items WHERE id = %s", (new_id,)).fetchone()[0]
        rel = conn.execute(
            "SELECT 1 FROM relations WHERE src_id=%s AND rel='supersedes' AND dst_id=%s",
            (new_id, nid),
        ).fetchone()
        old_body = conn.execute("SELECT body FROM items WHERE id = %s", (nid,)).fetchone()[0]
    assert "lang:python" in tags, "the lang tag is preserved in tags, only removed from the title"
    assert rel is not None, "the supersedes audit edge new -> old"
    assert old_body == "the python gotcha summary", "Inv 1: the old body is untouched"
    assert yield_hygiene.enumerate_summary_noise() == [], "the noise has left the enumerable set"


def test_apply_is_idempotent(kb):
    _noise_summary("lang:python")
    yield_hygiene.clean_summary_noise(dry_run=False)
    after_first = _snapshot()

    again = yield_hygiene.clean_summary_noise(dry_run=False)

    assert again == [], "nothing active still matches the noise pattern"
    assert _snapshot() == after_first, "a re-run mints/supersedes nothing"


def test_reuses_an_existing_retag_after_a_partial_run(kb):
    """A prior run minted the replacement but died before the supersede: reuse it."""
    nid = _noise_summary("lang:python", body="reuse me")
    replacement = ingest(
        PROJECT,
        "summary",
        f"{PREFIX}python",
        "reuse me",
        kind="chat",
        uri="summary:kb-system:lang:python:retag:partial",
        trust="unverified",
        tags=["lang:python"],
        meta={yield_hygiene.RETAG_META_KEY: nid},
    )
    assert replacement is not None
    items_before = _snapshot()[0]

    outcomes = yield_hygiene.clean_summary_noise(dry_run=False)

    assert outcomes[0].new_item_id == replacement, "the existing retag is reused, not re-minted"
    assert _snapshot()[0] == items_before, "no second replacement minted"
    assert _one(nid, "status") == "superseded"


# --- 3. summary noise: guards ------------------------------------------------


def test_refuses_a_verified_summary(kb):
    nid = _noise_summary("lang:python", trust="verified")

    outcomes = yield_hygiene.clean_summary_noise(dry_run=False)

    assert [(o.eligible, o.reason) for o in outcomes] == [(False, yield_hygiene.REFUSE_VERIFIED)]
    assert _one(nid, "status") == "active"


def test_refuses_a_summary_read_in_the_last_30_days(kb):
    nid = _noise_summary("lang:python")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO access_log (item_id, tool, ts) "
            "VALUES (%s, 'kb_get', now() - make_interval(days => 3))",
            (nid,),
        )

    outcomes = yield_hygiene.clean_summary_noise(dry_run=False)

    assert [o.reason for o in outcomes] == [yield_hygiene.REFUSE_RECENT_ACCESS]
    assert _one(nid, "status") == "active"


def test_an_access_older_than_the_window_does_not_refuse(kb):
    nid = _noise_summary("lang:python")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO access_log (item_id, tool, ts) "
            "VALUES (%s, 'kb_get', now() - make_interval(days => 400))",
            (nid,),
        )

    outcomes = yield_hygiene.clean_summary_noise(dry_run=False)

    assert [o.superseded for o in outcomes] == [True]
    assert _one(nid, "status") == "superseded"


def test_both_summary_surfaces_are_owner_only(kb):
    _noise_summary("lang:python")
    with scope.use_principal(_grantee()):
        with pytest.raises(InvalidOperation, match="owner only"):
            yield_hygiene.enumerate_summary_noise()
        with pytest.raises(InvalidOperation, match="owner only"):
            yield_hygiene.clean_summary_noise(dry_run=False)


# --- 4. judge-flagged: enumerate / prioritize --------------------------------


def test_enumerate_judge_failed_finds_open_flagged_rows_only(kb):
    fid, fq = _judge_failed(title="flagged")
    # A non-flagged item with an open row (different verified_by) is excluded...
    ok_id = ingest(
        PROJECT,
        "note",
        "ok",
        "ok body",
        kind="session",
        uri="jf:ok",
        trust="unverified",
        meta={"extraction_pipeline": {"verified_by": "primary-source"}},
    )
    review.queue_review(ok_id, "unverified_extraction", {})
    # ...and a flagged item whose row is already resolved is excluded too.
    _rid, rq = _judge_failed(title="resolved")
    with db.transaction() as conn:
        conn.execute("UPDATE review_queue SET resolved = true WHERE id = %s", (rq,))

    rows = yield_hygiene.enumerate_judge_failed()

    assert [(r.queue_id, r.item_id) for r in rows] == [(fq, fid)]
    assert rows[0].already_prioritized is False


def test_prioritize_dry_run_writes_nothing(kb):
    _fid, fq = _judge_failed()
    before = _detail(fq)

    outcomes = yield_hygiene.prioritize_judge_failed()

    assert [(o.stamped, o.reason) for o in outcomes] == [(False, "ok")]
    assert _detail(fq) == before
    assert _resolved(fq) is False


def test_prioritize_stamps_the_marker_without_resolving_or_hiding_from_triage(kb):
    fid, fq = _judge_failed()

    outcomes = yield_hygiene.prioritize_judge_failed(dry_run=False)

    assert [(o.stamped, o.reason) for o in outcomes] == [(True, "ok")]
    detail = _detail(fq)
    assert detail["priority"]["reason"] == yield_hygiene.JUDGE_FAILED_LABEL
    assert _resolved(fq) is False, "the row stays OPEN — not auto-resolved"
    assert _one(fid, "status") == "active", "the item is untouched"
    # The whole point: the marker is NOT the 'triage' key, so the triage picker
    # still selects the row (pending_triage EXCLUDES rows carrying a 'triage' key).
    assert "triage" not in detail
    assert fq in {r["queue_id"] for r in pending_triage(50)}


def test_prioritize_is_idempotent(kb):
    _fid, fq = _judge_failed()
    yield_hygiene.prioritize_judge_failed(dry_run=False)
    stamped_detail = _detail(fq)

    again = yield_hygiene.prioritize_judge_failed(dry_run=False)

    assert [(o.stamped, o.reason) for o in again] == [(False, yield_hygiene.ALREADY_PRIORITIZED)]
    assert _detail(fq) == stamped_detail, "the marker is not rewritten on a re-run"


def test_both_judge_surfaces_are_owner_only(kb):
    _judge_failed()
    with scope.use_principal(_grantee()):
        with pytest.raises(InvalidOperation, match="owner only"):
            yield_hygiene.enumerate_judge_failed()
        with pytest.raises(InvalidOperation, match="owner only"):
            yield_hygiene.prioritize_judge_failed(dry_run=False)


# --- 5. CLI ------------------------------------------------------------------


def test_cli_summaries_default_is_a_dry_run_table(kb):
    nid = _noise_summary("lang:python")

    result = runner.invoke(app, ["repair", "summaries"])

    assert result.exit_code == 0, result.output
    assert f"#{nid}" in result.output and "would retag" in result.output
    assert "1 summary(ies), 1 eligible, 0 retagged" in result.output
    assert "dry run — nothing written" in result.output
    assert _one(nid, "status") == "active"


def test_cli_summaries_apply_confirms_then_retags_and_prints_undo(kb, lock_free):
    nid = _noise_summary("lang:python")

    result = runner.invoke(app, ["repair", "summaries", "--apply"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "[y/N]" in result.output, "--apply must ask before it writes"
    assert "RETAGGED" in result.output
    assert "1 summary(ies), 1 eligible, 1 retagged" in result.output
    assert "undo / audit trail" in result.output
    assert _one(nid, "status") == "superseded"


def test_cli_summaries_apply_refuses_while_a_consolidation_holds_the_lock(kb, monkeypatch):
    nid = _noise_summary("lang:python")

    @contextmanager
    def _held(_key: int):
        yield False

    monkeypatch.setattr(db, "advisory_lock", _held)

    result = runner.invoke(app, ["repair", "summaries", "--apply"], input="y\n")

    assert result.exit_code == 1
    assert "consolidation lock 42 is held" in result.output
    assert _one(nid, "status") == "active"


def test_cli_summaries_reports_an_empty_set(kb):
    result = runner.invoke(app, ["repair", "summaries"])

    assert result.exit_code == 0, result.output
    assert "no 'Summary — lang:<tag>' noise summaries found" in result.output


def test_cli_judge_flagged_default_is_a_dry_run_table(kb):
    _fid, fq = _judge_failed()

    result = runner.invoke(app, ["repair", "judge-flagged"])

    assert result.exit_code == 0, result.output
    assert f"#{fq}" in result.output and "would prioritize" in result.output
    assert "1 judge-failed row(s), 1 to prioritize, 0 stamped" in result.output
    assert "dry run — nothing written" in result.output
    assert _resolved(fq) is False


def test_cli_judge_flagged_apply_stamps_and_flags_the_ordering_gap(kb):
    _fid, fq = _judge_failed()

    result = runner.invoke(app, ["repair", "judge-flagged", "--apply"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "[y/N]" in result.output
    assert "PRIORITIZED" in result.output
    assert "1 stamped" in result.output
    assert "priority marker written" in result.output
    assert "ORDER BY" in result.output, "the CLI must flag the one-line pending_triage fix"
    assert "priority" in _detail(fq)
    assert _resolved(fq) is False

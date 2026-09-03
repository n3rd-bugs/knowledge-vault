"""Review #8 H13 — review chokepoint hardening + promote-run usability.

(a) ``promote()`` / ``reject()`` lock the row (``FOR UPDATE OF rq``) and refuse an
already-resolved one, exactly as ``dismiss()`` did — the auto-apply/bulk-reject vs
promote race can no longer end archived-but-verified. (b) ``kb review promote-run
--apply`` promotes EXACTLY the ids it previewed (V4 fidelity) and the preview shows
the project column. (c) ``kb review promote-runs`` lists the runs worth pasting —
read-only, owner-only.

Real DB (kb_test); Ollama never called.
"""

from __future__ import annotations

import threading

import pytest
import typer
from typer.testing import CliRunner

from kbcore import db, principals, review_promote, review_promote_runs, scope
from kbcore import review as review_mod
from kbcore.errors import InvalidOperation, KBError, NotFound
from kbcore.ingest import ingest
from kbcore.review import queue_review

runner = CliRunner()


def _unverified(
    tag: str, project: str = "hp", *, run: str | None = None, type_: str = "note"
) -> tuple[int, int]:
    """One unverified item + its open unverified_extraction row; ``run`` makes it a
    session-run member (kind='session', sources.uri == run)."""
    item_id = ingest(
        project,
        type_,
        f"t {tag}",
        f"b {tag}",
        kind="session" if run else "manual",
        uri=run or tag,
        trust="unverified",
    )
    return item_id, queue_review(item_id, "unverified_extraction", {"rejected_count": 0})


def _state(item_id: int) -> tuple[str, str]:
    with db.connect() as conn:
        r = conn.execute(
            "SELECT i.status, COALESCE(s.trust, 'verified') FROM items i "
            "LEFT JOIN sources s ON s.id = i.source_id WHERE i.id = %s",
            (item_id,),
        ).fetchone()
    return r[0], r[1]


def _resolved(qid: int) -> bool:
    with db.connect() as conn:
        return bool(
            conn.execute("SELECT resolved FROM review_queue WHERE id = %s", (qid,)).fetchone()[0]
        )


def _snapshot() -> tuple:
    with db.connect() as conn:
        items = conn.execute("SELECT id, status FROM items ORDER BY id").fetchall()
        sources = conn.execute("SELECT id, trust FROM sources ORDER BY id").fetchall()
        queue = conn.execute("SELECT id, resolved FROM review_queue ORDER BY id").fetchall()
    return items, sources, queue


def _grantee_with_resolve_cap(name: str, project: str) -> scope.Principal:
    principals.create_principal(name, kind="token", role="contributor")
    principals.grant(name, project, rw=True)
    p = principals.resolve_name(name)
    assert p is not None
    return scope.Principal(
        name=p.name,
        is_owner=False,
        read_projects=p.read_projects,
        write_projects=p.write_projects,
        kind=p.kind,
        role=p.role,
        caps=p.caps | {scope.CAP_REVIEW_RESOLVE},
    )


# --- (a) the resolved re-check on promote/reject --------------------------------


def test_promote_refuses_a_row_a_reject_already_resolved(kb):
    item, q = _unverified("a")
    assert review_mod.reject(q) == item
    with pytest.raises(InvalidOperation, match="already resolved"):
        review_mod.promote(q)
    assert _state(item) == ("archived", "unverified"), "never archived-but-verified"


def test_reject_refuses_a_row_a_promote_already_resolved(kb):
    item, q = _unverified("b")
    assert review_mod.promote(q) == item
    with pytest.raises(InvalidOperation, match="already resolved"):
        review_mod.reject(q)
    assert _state(item) == ("active", "verified"), "verified knowledge stays live"


def test_resolve_via_supersede_inherits_the_same_re_check(kb):
    item, q = _unverified("c")
    winner = ingest("hp", "note", "t winner", "b winner", uri="winner")
    review_mod.dismiss(q)
    with pytest.raises(InvalidOperation, match="already resolved"):
        review_mod.resolve_via_supersede(q, winner)
    assert _state(item) == ("active", "unverified")


def test_promote_waits_for_the_row_lock_then_refuses_the_row_rejected_meanwhile(kb):
    """THE RACE, for real: a bulk reject holds the row lock mid-transaction while an
    owner promote arrives. The promote BLOCKS on ``FOR UPDATE OF rq``; the reject
    commits (item archived, row resolved); the promote re-reads the committed row
    and refuses. No archived+verified item, ever."""
    item, q = _unverified("race")
    outcome: dict = {}

    def promote_in_another_session() -> None:
        try:
            outcome["value"] = review_mod.promote(q)
        except Exception as e:  # noqa: BLE001 — the assertion below reads the type
            outcome["error"] = e

    with db.transaction() as conn:  # the reject's transaction, mid-flight
        conn.execute("SELECT id FROM review_queue WHERE id = %s FOR UPDATE", (q,))
        t = threading.Thread(target=promote_in_another_session)
        t.start()
        t.join(1.0)
        assert t.is_alive(), f"the promote must WAIT on the row lock, not read past it: {outcome}"
        conn.execute(
            "UPDATE items SET status = 'archived', updated_at = now() WHERE id = %s", (item,)
        )
        conn.execute("UPDATE review_queue SET resolved = true WHERE id = %s", (q,))
    t.join(10.0)
    assert not t.is_alive(), "the promote never woke up after the lock was released"
    assert isinstance(outcome.get("error"), InvalidOperation), outcome
    assert _state(item) == ("archived", "unverified")


def test_a_withheld_resolved_row_stays_byte_identical_to_a_missing_one_for_a_grantee(kb):
    """No-oracle: the readability wall runs BEFORE the resolved re-check, so a
    grantee cannot learn whether a withheld project's row is open or resolved."""
    _item, q = _unverified("nda", project="secret")
    review_mod.dismiss(q)
    g = _grantee_with_resolve_cap("peer", "elsewhere")
    with scope.use_principal(g), pytest.raises(NotFound) as withheld:
        review_mod.reject(q)
    with scope.use_principal(g), pytest.raises(NotFound) as missing:
        review_mod.reject(q + 100000)
    assert str(withheld.value).replace(str(q), "N") == str(missing.value).replace(
        str(q + 100000), "N"
    )


# --- (b) the pinned preview + project column ------------------------------------


def test_promote_run_rows_carry_the_project(kb):
    a, _q = _unverified("r1", project="alpha", run="session:proj")
    rows = review_promote.promote_run("session:proj")
    assert len(rows) == 1
    assert rows[0]["item_id"] == a and rows[0]["project"] == "alpha"
    assert rows[0]["type"] == "note" and rows[0]["title"] == "t r1"
    assert rows[0]["outcome"].startswith("would promote")


def test_cli_promote_run_apply_promotes_exactly_the_previewed_ids(kb, monkeypatch):
    from kbcli.main import app

    a, qa = _unverified("pin-a", run="session:pin")
    b, qb = _unverified("pin-b", run="session:pin")
    late: dict = {}

    def confirm_while_a_late_item_lands(*args, **kwargs) -> bool:
        # Between the preview the owner read and the write: a same-run item arrives.
        late["id"], late["q"] = _unverified("pin-late", run="session:pin")
        return True

    monkeypatch.setattr(typer, "confirm", confirm_while_a_late_item_lands)
    res = runner.invoke(app, ["review", "promote-run", "session:pin", "--apply"])

    assert res.exit_code == 0, res.output
    assert f"[{a}] promoted (queue {qa} resolved)" in res.stdout
    assert f"[{b}] promoted (queue {qb} resolved)" in res.stdout
    assert f"[{late['id']}]" not in res.stdout
    assert "promote-run: 2 promoted, 0 error(s), 2 item(s)" in res.stdout
    assert _state(late["id"]) == ("active", "unverified"), "the unvetted late item is NOT promoted"
    assert not _resolved(late["q"])

    # ...and the next dry run shows it — with the project column — for the owner to vet.
    res = runner.invoke(app, ["review", "promote-run", "session:pin"])
    assert res.exit_code == 0, res.output
    assert f"[{late['id']}] hp" in res.stdout and "t pin-late" in res.stdout
    assert "dry run: 1 item(s) would promote" in res.stdout


# --- (c) the promote-runs listing ----------------------------------------------


def test_list_promote_runs_orders_largest_first_and_excludes_spent_old_and_non_runs(kb):
    _unverified("big-1", project="alpha", run="session:big")
    _unverified("big-2", project="beta", run="session:big")
    _unverified("big-3", project="alpha", run="session:big")
    _unverified("small-1", run="session:small")
    # A run whose only item is already verified: nothing to promote, not listed.
    ingest("hp", "note", "t done", "b done", kind="session", uri="session:done", trust="verified")
    # A promotable run from 10 days ago: outside the default window.
    _unverified("old-1", run="session:old")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE sources SET ingested_at = now() - interval '10 days' WHERE uri = 'session:old'"
        )
    # A manual source with a session-looking uri: wrong KIND, never a run.
    ingest(
        "hp",
        "note",
        "t manual",
        "b manual",
        kind="manual",
        uri="session:manual",
        trust="unverified",
    )
    before = _snapshot()

    rows = review_promote_runs.list_promote_runs(7)

    assert [(r["run_key"], r["candidates"], r["items"]) for r in rows] == [
        ("session:big", 3, 3),
        ("session:small", 1, 1),
    ]
    assert rows[0]["projects"] == ["alpha", "beta"]
    assert rows[0]["last_seen"].tzinfo is not None, "UTC-aware (Invariant 7)"
    assert [r["run_key"] for r in review_promote_runs.list_promote_runs(30)] == [
        "session:big",
        "session:small",
        "session:old",
    ]
    assert review_promote_runs.list_promote_runs(7, limit=1) == rows[:1]
    assert review_promote_runs.promote_runs_summary(7) == {
        "runs": 2,
        "items": 4,
        "largest_key": "session:big",
        "largest_items": 3,
    }
    assert review_promote_runs.promote_runs_summary(7) is not None
    assert _snapshot() == before, "READ-ONLY: the listing writes nothing"


def test_list_promote_runs_is_owner_only(kb):
    _unverified("o-1", project="alpha", run="session:owner")
    g = _grantee_with_resolve_cap("reader", "alpha")
    with scope.use_principal(g), pytest.raises(KBError):
        review_promote_runs.list_promote_runs(7)


def test_cli_promote_runs_lists_and_names_the_next_paste(kb):
    from kbcli.main import app

    res = runner.invoke(app, ["review", "promote-runs"])
    assert res.exit_code == 0, res.output
    assert "no session runs with promotable items in the last 7 day(s)" in res.stdout

    _unverified("c1", project="alpha", run="session:cli-big")
    _unverified("c2", project="alpha", run="session:cli-big")
    _unverified("c3", run="session:cli-small")
    before = _snapshot()

    res = runner.invoke(app, ["review", "promote-runs", "--days", "3"])

    assert res.exit_code == 0, res.output
    lines = res.stdout.splitlines()
    assert lines[0].lstrip().startswith("items") and lines[0].rstrip().endswith("run key")
    assert lines[1].lstrip().startswith("2 ") and "alpha" in lines[1]
    assert lines[1].rstrip().endswith("session:cli-big")
    assert lines[2].rstrip().endswith("session:cli-small")
    assert "2 run(s); next: kb review promote-run session:cli-big [--apply]" in res.stdout
    assert _snapshot() == before, "READ-ONLY at the CLI too"

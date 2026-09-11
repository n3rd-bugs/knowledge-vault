"""kb repair resend-dups — the owner repair for cross-project session resends (wave S, S5).

Real DB in kb_test only. Historical rows are inserted DIRECTLY in the pre-020 shape
(chunk_key / duplicate_of NULL): the live guard (S2) refuses exactly this
cross-project shape at enqueue, and these rows model the ~1,070 written before it.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from psycopg.types.json import Jsonb
from typer.testing import CliRunner

from kbcli.main import app
from kbcore import db, ingest_queue, resend_repair, review, scope
from kbcore.errors import InvalidOperation
from kbcore.ingest import ingest
from kbcore.queue_guard import chunk_key
from kbcore.resend_repair import RepairSelector
from kbcore.scrub import scrub

pytestmark = pytest.mark.usefixtures("kb")
runner = CliRunner()

SID = "7c26902a-3e49-43bf-8327-1add36ac9e93"
URI = f"session:{SID}"
CHUNK1 = "user: how do we mount the volume?\nassistant: bind-mount /srv/data read-only."
CHUNK2 = "user: and the second chunk?\nassistant: the compose file pins the image tag."


def _hist(
    project: str,
    body: str,
    *,
    uri: str = URI,
    status: str = "pending",
    saved: int | None = None,
    error: str | None = None,
    attempts: int = 0,
) -> int:
    clean, _ = scrub(body)
    h = hashlib.sha256(clean.encode()).hexdigest()
    with db.transaction() as c:
        return c.execute(
            """
            INSERT INTO ingest_queue (project, kind, content, content_hash, meta, status,
                                      saved_count, error, attempts, requeue_count, processed_at)
            VALUES (%s, 'session', %s, %s, %s, %s, %s, %s, %s, %s,
                    CASE WHEN %s IN ('extracted', 'failed') THEN now() END)
            RETURNING id
            """,
            (
                project,
                clean,
                h,
                Jsonb({"source_uri": uri, "submitted_by": "owner"}),
                status,
                saved,
                error,
                attempts,
                3 if error and error.startswith("parked") else 0,
                status,
            ),
        ).fetchone()[0]


def _row(rid: int) -> dict:
    with db.connect() as c:
        r = c.execute(
            "SELECT status, error, duplicate_of, chunk_key, processed_at, meta, saved_count "
            "FROM ingest_queue WHERE id = %s",
            (rid,),
        ).fetchone()
    return dict(
        zip(
            ("status", "error", "duplicate_of", "chunk_key", "processed_at", "meta", "saved"),
            r,
            strict=True,
        )
    )


def _item(project: str, title: str, quote: str, *, uri: str = URI, trust: str = "unverified"):
    iid = ingest(
        project,
        "note",
        title,
        f"{title} body",
        kind="session",
        uri=uri,
        trust=trust,
        meta={"quote": quote},
    )
    rq = review.queue_review(iid, "unverified_extraction", {"title": title})
    return iid, rq


def _item_state(iid: int) -> tuple[str, dict, str]:
    with db.connect() as c:
        return c.execute(
            "SELECT i.status, i.meta, s.trust FROM items i JOIN sources s ON s.id = i.source_id "
            "WHERE i.id = %s",
            (iid,),
        ).fetchone()


def _review_resolved(rq: int) -> bool:
    with db.connect() as c:
        return c.execute("SELECT resolved FROM review_queue WHERE id = %s", (rq,)).fetchone()[0]


def _seed_extracted_canonical(project_b: str = "docker-mount") -> tuple[list[int], list[int]]:
    """aquamonix mined the session (saved on the group's first id); B re-sent both chunks."""
    a1 = _hist("aquamonix", CHUNK1, status="extracted", saved=5)
    a2 = _hist("aquamonix", CHUNK2, status="extracted", saved=0)
    b1 = _hist(project_b, CHUNK1)
    b2 = _hist(project_b, CHUNK2)
    return [a1, a2], [b1, b2]


# --- step 0 -------------------------------------------------------------------


def test_backfill_keys_picks_min_id_and_is_idempotent():
    (a1, a2), (b1, b2) = _seed_extracted_canonical()
    solo = _hist("gateway", "a single-row group gets its key too", uri="session:remint-1")

    plan = resend_repair.backfill_keys(dry_run=True)
    assert plan.groups == 3 and _row(a1)["chunk_key"] is None, "dry run wrote a key"
    done = resend_repair.backfill_keys(dry_run=False)
    assert (done.keyed, done.marked) == (3, 2)
    assert _row(a1)["chunk_key"] == chunk_key(URI, _hash(CHUNK1))
    assert _row(solo)["chunk_key"] == chunk_key(
        "session:remint-1", _hash(solo_body := _content(solo))
    )
    assert _row(b1)["duplicate_of"] == a1 and _row(b2)["duplicate_of"] == a2
    assert _row(b1)["chunk_key"] is None, "the partial unique index tolerates NULL on later rows"
    assert _row(b1)["status"] == "pending", "backfill is column writes only — no status"
    assert solo_body

    again = resend_repair.backfill_keys(dry_run=False)
    assert (again.keyed, again.marked) == (0, 0), "second run must plan nothing"


def _hash(body: str) -> str:
    return hashlib.sha256(scrub(body)[0].encode()).hexdigest()


def _content(rid: int) -> str:
    with db.connect() as c:
        return c.execute("SELECT content FROM ingest_queue WHERE id = %s", (rid,)).fetchone()[0]


# --- step 1 rows --------------------------------------------------------------


def test_rows_only_flips_only_pending_later_duplicates():
    (a1, a2), (b1, b2) = _seed_extracted_canonical()
    b_extracted = _hist("rm3-tester", CHUNK1, status="extracted", saved=0)
    resend_repair.backfill_keys(dry_run=False)

    sel = RepairSelector(all=True, rows_only=True)
    plans = resend_repair.plan(sel)
    assert {(p.project, p.pend, p.extr) for p in plans} == {
        ("docker-mount", 2, 0),
        ("rm3-tester", 0, 1),
    }
    resend_repair.apply(plans, sel)

    for rid in (b1, b2):
        r = _row(rid)
        assert r["status"] == "duplicate"
        assert r["error"] == f"repair: resend duplicate of {r['duplicate_of']}"
        assert r["processed_at"] is None, "the repair must never write processed_at"
        assert r["meta"]["repair"]["by"] == "resend-dups" and r["meta"]["repair"]["at"]
    ex = _row(b_extracted)
    assert ex["status"] == "extracted" and ex["duplicate_of"] == a1, "only duplicate_of marks it"
    assert _row(a1)["status"] == "extracted" and _row(a1)["saved"] == 5, "canonical untouched"
    assert _row(a2)["status"] == "extracted"


def test_drain_never_sees_a_duplicate_row():
    _, (b1, b2) = _seed_extracted_canonical()
    resend_repair.backfill_keys(dry_run=False)
    sel = RepairSelector(session=SID[:8], rows_only=True)
    resend_repair.apply(resend_repair.plan(sel), sel)

    drainable = {r["id"] for r in ingest_queue.pending(limit=100)}
    grouped = {r["id"] for g in ingest_queue.pending_groups(quiet_minutes=0) for r in g["rows"]}
    assert not ({b1, b2} & drainable) and not ({b1, b2} & grouped)
    assert ingest_queue.queue_depth() == 0


# --- step 2 items: the canonical gate ----------------------------------------


@pytest.mark.parametrize(
    ("canon_status", "canon_saved", "why"),
    [("failed", None, "failed"), ("pending", None, "pending"), ("extracted", 0, "zero-yield")],
)
def test_unmined_canonical_archives_nothing(canon_status, canon_saved, why):
    _hist("aquamonix", CHUNK1, status=canon_status, saved=canon_saved, error="x")
    b1 = _hist("docker-mount", CHUNK1)
    iid, rq = _item("docker-mount", "mount decision", "bind-mount /srv/data read-only.")
    resend_repair.backfill_keys(dry_run=False)

    sel = RepairSelector(session=SID[:8])
    plans = resend_repair.plan(sel)
    (p,) = plans
    assert p.items == 1 and p.archive_ids == [] and p.keep == {f"canonical-unmined({why})": 1}
    assert p.canon_status == canon_status
    resend_repair.apply(plans, sel)
    assert _item_state(iid)[0] == "active" and not _review_resolved(rq)
    assert _row(b1)["status"] == "duplicate", "the rows-only flip still applies"


def test_extracted_canonical_with_yield_archives_with_reason():
    (a1, _a2), (b1, b2) = _seed_extracted_canonical()
    iid, rq = _item("docker-mount", "mount decision", "bind-mount /srv/data read-only.")
    other, rq_other = _item("aquamonix", "canonical's own item", "pins the image tag.")
    verified, _ = _item("docker-mount", "owner note", "hand written", trust="verified")
    resend_repair.backfill_keys(dry_run=False)

    sel = RepairSelector(session=SID[:8])
    plans = resend_repair.plan(sel)
    (p,) = plans
    assert (p.canon_status, p.canon_saved, p.canon_project) == ("extracted", 5, "aquamonix")
    assert p.archive_ids == [iid] and p.items == 1, "verified items are never listed"
    assert "flip 2 row(s)" in p.decision and "archive 1 item(s)" in p.decision

    resend_repair.apply(plans, sel)
    status, meta, trust = _item_state(iid)
    assert status == "archived" and trust == "unverified"
    assert meta["archived"]["reason"] == "repair:resend-duplicate"
    assert meta["archived"]["canonical_project"] == "aquamonix"
    assert meta["archived"]["queue_rows"] == sorted([b1, b2])
    assert meta["repair"] == {
        "at": meta["archived"]["at"],
        "by": "resend-dups",
        "reason": "repair:resend-duplicate",
        "review_ids": [rq],
    }
    assert _review_resolved(rq) and not _review_resolved(rq_other)
    assert _item_state(other)[0] == "active" and _item_state(verified)[0] == "active"


def test_recalled_items_are_kept_and_reserved_project_never_flipped():
    (a1, _), _ = _seed_extracted_canonical()
    kb1 = _hist("kb-system", CHUNK1)
    iid, _ = _item("docker-mount", "recalled", "read-only.")
    from kbcore.items import log_access

    log_access(iid, "kb_get")
    sys_item, _ = _item("kb-system", "system note", "read-only.")
    resend_repair.backfill_keys(dry_run=False)

    sel = RepairSelector(all=True)
    plans = resend_repair.plan(sel)
    by_project = {p.project: p for p in plans}
    assert by_project["docker-mount"].keep == {"recalled": 1}
    assert by_project["kb-system"].reserved and not by_project["kb-system"].eligible
    assert by_project["kb-system"].decision == "keep: reserved project"
    resend_repair.apply(plans, sel)
    assert _row(kb1)["status"] == "pending" and _row(kb1)["duplicate_of"] == a1
    assert _item_state(sys_item)[0] == "active" and _item_state(iid)[0] == "active"


def test_attribute_quotes_decides_a_mixed_group_by_quote_location():
    (a1, _), _ = _seed_extracted_canonical()
    with db.transaction() as c:  # drop B's second chunk: B keeps one genuine chunk of its own
        c.execute("DELETE FROM ingest_queue WHERE project = 'docker-mount'")
    _hist("docker-mount", CHUNK1)  # duplicate of aquamonix's
    _hist("docker-mount", "assistant: only docker-mount ever saw this line.")  # B's own
    from_dup, _ = _item("docker-mount", "dup item", "bind-mount /srv/data read-only.")
    from_own, _ = _item("docker-mount", "own item", "only docker-mount ever saw this line.")
    nowhere, _ = _item("docker-mount", "unfound", "a quote no row contains")
    resend_repair.backfill_keys(dry_run=False)

    (mixed,) = resend_repair.plan(RepairSelector(project="docker-mount"))
    assert mixed.rows == 2 and mixed.dup == 1 and mixed.archive_ids == []
    assert mixed.keep == {"mixed (use --attribute-quotes)": 3}

    sel = RepairSelector(project="docker-mount", attribute_quotes=True)
    (p,) = resend_repair.plan(sel)
    assert p.archive_ids == [from_dup] and p.keep == {"ambiguous": 2}
    resend_repair.apply([p], sel)
    assert _item_state(from_dup)[0] == "archived"
    assert _item_state(from_own)[0] == "active" and _item_state(nowhere)[0] == "active"


# --- --as-resend --rows-file / --uri ---------------------------------------


def test_as_resend_rows_file_flips_only_listed_rows_including_parked(tmp_path):
    a1 = _hist("aquamonix", CHUNK1, status="extracted", saved=3)
    parked = _hist(
        "rm3-tester",
        CHUNK1,
        status="failed",
        error="parked 2026-09-10 owner: resend residue",
        attempts=3,
    )
    listed_pending = _hist("docker-mount", CHUNK1)
    unlisted = _hist("gateway", CHUNK1)
    no_canon = _hist("rm3-tester", "an off-lattice chunk nobody else holds")
    ordinary_failed = _hist("rm3-tester", CHUNK2, status="failed", error="embed ReadTimeout")
    f = tmp_path / "parked_rows_2026-09-10.txt"
    f.write_text(
        "# id project status\n"
        f"{parked} rm3-tester failed\n{listed_pending}\t docker-mount\n{no_canon}\n"
        f"{ordinary_failed} rm3-tester failed\n"
    )
    result = runner.invoke(
        app,
        ["repair", "resend-dups", "--rows-file", str(f), "--as-resend", "--rows-only", "--apply"],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert "keep: no canonical (1)" in result.output
    for rid in (parked, listed_pending):
        r = _row(rid)
        assert r["status"] == "duplicate" and r["duplicate_of"] == a1
        assert r["processed_at"] is not None if rid == parked else r["processed_at"] is None
    assert _row(parked)["meta"]["repair"]["prev_status"] == "failed"
    assert _row(parked)["meta"]["repair"]["prev_error"].startswith("parked 2026-09-10")
    assert _row(unlisted)["status"] == "pending" and _row(unlisted)["duplicate_of"] is None
    assert _row(no_canon)["status"] == "pending"
    assert _row(ordinary_failed)["status"] == "failed", "only 'parked %' failed rows flip"
    assert _row(a1)["status"] == "extracted"


def test_as_resend_prefers_an_extracted_canonical_over_the_lowest_id():
    low_pending = _hist("gateway", CHUNK1)  # lowest id, not mined
    mined = _hist("aquamonix", CHUNK1, status="extracted", saved=2)
    resend = _hist("docker-mount", CHUNK1)
    sel = RepairSelector(project="docker-mount", as_resend=True, rows_only=True)
    (p,) = resend_repair.plan(sel)
    assert p.flip == {resend: mined}
    assert _row(low_pending)["status"] == "pending"


def test_uri_selector_reaches_exactly_that_session():
    _hist("aquamonix", "remint firing", uri="session:remint-1", status="extracted", saved=1)
    remint = _hist("gateway", "remint firing", uri="session:remint-1")
    _, (b1, _) = _seed_extracted_canonical()
    resend_repair.backfill_keys(dry_run=False)

    sel = RepairSelector(uri="session:remint-1", rows_only=True)
    plans = resend_repair.plan(sel)
    assert [(p.uri, p.project) for p in plans] == [("session:remint-1", "gateway")]
    resend_repair.apply(plans, sel)
    assert _row(remint)["status"] == "duplicate" and _row(b1)["status"] == "pending"
    with pytest.raises(InvalidOperation):
        resend_repair.require_selector(RepairSelector(uri="session:"))


# --- undo, guards, CLI --------------------------------------------------------


def test_undo_restores_rows_and_items_and_clears_the_marker():
    a1 = _hist("aquamonix", CHUNK1, status="extracted", saved=3)
    _hist("aquamonix", CHUNK2, status="extracted", saved=0)
    b1 = _hist("docker-mount", CHUNK1)
    parked = _hist("docker-mount", CHUNK2, status="failed", error="parked 2026-09-10 x", attempts=3)
    iid, rq = _item("docker-mount", "mount decision", "read-only.")
    sel = RepairSelector(session=SID[:8], as_resend=True)
    resend_repair.apply(resend_repair.plan(sel), sel)
    assert _item_state(iid)[0] == "archived" and _row(parked)["status"] == "duplicate"

    dry = resend_repair.undo(sel, dry_run=True)
    assert sorted(dry.row_ids) == sorted([b1, parked]) and dry.item_ids == [iid]
    assert _row(b1)["status"] == "duplicate", "undo dry run wrote nothing"
    done = resend_repair.undo(sel, dry_run=False)
    assert (done.rows_restored, done.items_restored, done.reviews_reopened) == (2, 1, 1)
    assert _row(b1)["status"] == "pending" and _row(b1)["error"] is None
    assert _row(parked)["status"] == "failed" and _row(parked)["error"] == "parked 2026-09-10 x"
    assert "repair" not in _row(b1)["meta"] and _row(b1)["duplicate_of"] == a1, "facts stay"
    status, meta, _ = _item_state(iid)
    assert status == "active" and "repair" not in meta and "archived" not in meta
    assert not _review_resolved(rq)
    assert resend_repair.undo(sel, dry_run=False).rows_restored == 0, "second undo is a no-op"


def test_owner_only_and_selector_required():
    grantee = scope.Principal(name="kbg:reader", read_projects=frozenset({"docker-mount"}))
    with scope.use_principal(grantee):
        with pytest.raises(InvalidOperation, match="owner only"):
            resend_repair.plan(RepairSelector(all=True))
        with pytest.raises(InvalidOperation, match="owner only"):
            resend_repair.backfill_keys()
        result = runner.invoke(app, ["repair", "resend-dups", "--all"])
    assert result.exit_code == 1 and "owner only" in result.output
    with pytest.raises(InvalidOperation, match="selector"):
        resend_repair.plan(RepairSelector())
    result = runner.invoke(app, ["repair", "resend-dups"])
    assert result.exit_code == 1 and "selector" in result.output


def test_cli_dry_run_writes_nothing_and_apply_prints_the_trail(monkeypatch):
    from kbcore.config import settings

    monkeypatch.setattr(settings, "kb_repair_mb_per_gpu_night", 28)
    _, (b1, b2) = _seed_extracted_canonical()
    iid, _ = _item("docker-mount", "mount decision", "read-only.")
    resend_repair.backfill_keys(dry_run=False)

    def snapshot():
        with db.connect() as c:
            return c.execute(
                "SELECT (SELECT count(*) FROM ingest_queue WHERE status = 'duplicate'),"
                " (SELECT count(*) FROM items WHERE status = 'archived'),"
                " (SELECT count(*) FROM review_queue WHERE resolved)"
            ).fetchone()

    before = snapshot()
    dry = runner.invoke(app, ["repair", "resend-dups", "--session", SID[:8]])
    assert dry.exit_code == 0, dry.output
    header = dry.output.splitlines()[0].split()
    columns = "session project rows dup pend extr MB canon(status,saved) items archive keep"
    assert header == [*columns.split(), "decision"]
    assert "dry run — nothing written" in dry.output and "GPU-nights reclaimed:" in dry.output
    assert "extracted,5" in dry.output and "flip 2 row(s); archive 1 item(s)" in dry.output
    assert snapshot() == before

    applied = runner.invoke(
        app, ["repair", "resend-dups", "--session", SID[:8], "--apply"], input="y\n"
    )
    assert applied.exit_code == 0, applied.output
    assert "FLIPPED 2 row(s); ARCHIVED 1 item(s)" in applied.output and "--undo" in applied.output
    assert snapshot() == (2, 1, 1)
    assert (
        _item_state(iid)[0] == "archived"
        and _row(b1)["status"] == _row(b2)["status"] == "duplicate"
    )

    undo = runner.invoke(
        app, ["repair", "resend-dups", "--session", SID[:8], "--undo", "--apply"], input="y\n"
    )
    assert undo.exit_code == 0, undo.output
    assert "restored 2 row(s), 1 item(s); re-opened 1 review row(s)" in undo.output
    assert snapshot() == before


def test_cli_apply_refuses_while_the_nightly_holds_the_lock():
    from kbcore.db import advisory_lock
    from kbworker.consolidate import ADVISORY_KEY

    _seed_extracted_canonical()
    resend_repair.backfill_keys(dry_run=False)
    with advisory_lock(ADVISORY_KEY) as held:
        assert held
        result = runner.invoke(
            app, ["repair", "resend-dups", "--all", "--rows-only", "--apply"], input="y\n"
        )
    assert result.exit_code == 1 and "consolidation lock" in result.output
    with db.connect() as c:
        assert (
            c.execute("SELECT count(*) FROM ingest_queue WHERE status='duplicate'").fetchone()[0]
            == 0
        )


def test_backfill_cli_and_inventory(tmp_path):
    _seed_extracted_canonical()
    dry = runner.invoke(app, ["repair", "resend-dups", "--backfill-keys"])
    assert dry.exit_code == 0 and "2 canonical(s) to key, 2 later row(s)" in dry.output
    assert resend_repair.duplicate_inventory()["unkeyed_groups"] == 2
    done = runner.invoke(app, ["repair", "resend-dups", "--backfill-keys", "--apply"], input="y\n")
    assert done.exit_code == 0 and "keyed 2 canonical(s), marked 2 later row(s)" in done.output
    inv = resend_repair.duplicate_inventory()
    assert inv["unkeyed_groups"] == 0 and inv["by_status"]["pending"]["rows"] == 2
    assert inv["by_status"]["pending"]["bytes"] > 0
    since = datetime.now(UTC).isoformat()
    result = runner.invoke(app, ["repair", "resend-dups", "--since", since, "--rows-only"])
    assert result.exit_code == 0 and "no session duplicate groups" in result.output

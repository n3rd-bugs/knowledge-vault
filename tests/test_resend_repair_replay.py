"""kb repair resend-dups --replayed — tier 3, the byte-range repair of a same-session
re-send (wave C.2, incident KB 49064).

Real DB in kb_test only. The session is built the way rm1's bd162e9f looked: a
seed-era UNSTAMPED prefix, stamped rows up to the covered end C, then — after
--since — the stale copy's re-send (stamped ranges inside [0, C), new chunk
boundaries, one row re-routed to kb-system) coalesced with genuinely NEW rows and
a legit pre-since row the same night drained. Rows and items are inserted directly
with explicit timestamps (the drain stamps processed_at when a GROUP completes,
after its items were minted — the fixture keeps that order).

The second half pins the 4-lens review's fixes (2026-10-11): successor / reprocess
items kept, quotes matched in scrubbed form and per-project concatenation, the W1
upper bound, --apply refused while R is undrained or the prefix CHECK is up, stamp
gaps -> NEW, the gate's real options, and tiers 1-2 never overwriting a replay marker.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb
from typer.testing import CliRunner

from kbcli.main import app
from kbcli.repair_resend_replay import replay_report
from kbcore import consolidation, db, resend_repair, resend_repair_replay, review
from kbcore import resend_repair_replay_rows as rows_step
from kbcore import resend_repair_rows as tier_rows
from kbcore.errors import InvalidOperation
from kbcore.ingest import ingest
from kbcore.items import log_access
from kbcore.resend_repair import GroupPlan, RepairSelector
from kbcore.scrub import scrub
from kbcore.supersede import supersede
from kbworker.sessions import assemble_text

pytestmark = pytest.mark.usefixtures("kb")
runner = CliRunner()

SID = "bd162e9f-1111-4222-8333-444455556666"
URI = f"session:{SID}"
PREFIX = SID[:13]  # 'bd162e9f-1111' — unique even beside a second bd162e9f session
T0 = datetime.now(UTC).replace(microsecond=0) - timedelta(days=10)
SINCE = T0 + timedelta(days=3)
DONE = SINCE + timedelta(days=1)  # the night the re-send's group completed (= W1)
LATE_AT = SINCE + timedelta(hours=20)  # items minted before the group's processed_at

A1 = "the tcg backend lowers guest loads through the softmmu slow-path helpers"
A2 = "we keep the tlb fill in a separate translation unit for the ld_le paths"
B1 = "the binary rewriter splits basic blocks at every indirect branch target"
B2 = "relocations are replayed after the trampoline pass so offsets stay stable"
NEW = 'set the "mmap_min_addr" knob to 0 before the "-R" reserved_va run'
LATE = "the late night fix pinned the guest base to keep the user-mode layout"
PAD = "seed-era context " + "lorem ipsum " * 60  # the prefix holds [0, first stamp)


def _msg(role: str, text: str) -> str:
    return json.dumps({"type": role, "message": {"role": role, "content": text}}) + "\n"


def _q(
    project: str,
    content: str,
    *,
    created: datetime,
    processed: datetime | None,
    status: str = "extracted",
    stamp: tuple[int, int] | None = None,
    uri: str = URI,
    attempts: int = 0,
    extra: dict | None = None,
    duplicate_of: int | None = None,
) -> int:
    meta: dict = {"source_uri": uri, "submitted_by": "i-qemu@rm1", **(extra or {})}
    if stamp is not None:
        meta["flush"] = {"offset": stamp[0], "bytes": stamp[1], "size": 400_000, "event": "Stop"}
    with db.transaction() as c:
        return c.execute(
            """
            INSERT INTO ingest_queue (project, kind, content, content_hash, meta, status,
                                      created_at, processed_at, attempts, duplicate_of)
            VALUES (%s, 'session', %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (
                project,
                content,
                hashlib.sha256(content.encode()).hexdigest(),
                Jsonb(meta),
                status,
                created,
                processed,
                attempts,
                duplicate_of,
            ),
        ).fetchone()[0]


def _item(
    project: str,
    title: str,
    quote: str,
    created: datetime,
    *,
    trust: str = "unverified",
    type_: str = "note",
    uri: str = URI,
    meta: dict | None = None,
) -> tuple[int, int]:
    iid = ingest(
        project, type_, title, f"{title} body", kind="session", uri=uri, trust=trust,
        meta={"quote": quote, **(meta or {})},
    )  # fmt: skip
    with db.transaction() as c:
        c.execute("UPDATE items SET created_at = %s WHERE id = %s", (created, iid))
    return iid, review.queue_review(iid, "unverified_extraction", {"title": title})


def _seed() -> dict[str, int]:
    """The bd162e9f shape. C = 2000; R = r1, r2 (r2 re-routed to kb-system);
    NEW = n1 (bytes past C) + late (enqueued before --since, drained after)."""
    day = timedelta(days=1)
    ids = {
        "u1": _q("i-qemu", _msg("user", PAD) + _msg("user", "how does tcg lower loads?")
                 + _msg("assistant", A1), created=T0, processed=T0 + timedelta(hours=1)),
        "u2": _q("i-qemu", _msg("assistant", A2), created=T0, processed=T0 + timedelta(hours=1)),
        "s1": _q("i-qemu", _msg("assistant", B1), created=T0 + day, processed=T0 + day,
                 stamp=(1000, 500)),
        "s2": _q("i-qemu", _msg("assistant", B2), created=T0 + day, processed=T0 + day,
                 stamp=(1500, 500)),
        "late": _q("i-qemu", _msg("assistant", LATE), created=SINCE - timedelta(hours=1),
                   processed=DONE),
        # the stale copy's re-send from byte 0: new boundaries, the same bytes
        "r1": _q("i-qemu", _msg("assistant", A1) + _msg("assistant", A2),
                 created=SINCE, processed=DONE, stamp=(0, 1000)),
        "r2": _q("kb-system", _msg("assistant", B1) + _msg("assistant", B2),
                 created=SINCE + timedelta(minutes=1), processed=DONE, stamp=(1000, 1000)),
        "n1": _q("i-qemu", _msg("assistant", NEW), created=SINCE + timedelta(minutes=2),
                 processed=DONE, stamp=(2000, 600)),
    }  # fmt: skip
    mid = SINCE + timedelta(hours=2)  # minted BEFORE the group's processed_at
    ids["dup1"], ids["rq_dup1"] = _item("i-qemu", "softmmu", "lowers guest loads", mid)
    ids["dup2"], ids["rq_dup2"] = _item("kb-system", "relocs", "relocations are replayed", LATE_AT)
    ids["new"], ids["rq_new"] = _item("i-qemu", "mmap", '"mmap_min_addr" knob to 0', LATE_AT)
    ids["late_i"], _ = _item("i-qemu", "guest base", "pinned the guest base", LATE_AT)
    ids["recalled"], _ = _item("i-qemu", "tlb", "tlb fill in a separate", LATE_AT)
    log_access(ids["recalled"], "kb_get")
    ids["noquote"], _ = _item("i-qemu", "no quote", "", LATE_AT)
    ids["old"], _ = _item("i-qemu", "old softmmu", "slow-path helpers", T0 + timedelta(hours=2))
    ids["verified"], _ = _item(
        "i-qemu", "owner note", "lowers guest loads", LATE_AT, trust="verified"
    )
    ids["summary"], _ = _item("i-qemu", "weekly", "lowers guest loads", LATE_AT, type_="summary")
    return ids


def _sel(**kw) -> RepairSelector:
    return RepairSelector(**{"session": PREFIX, "since": SINCE, "replayed": True, **kw})


def _snapshot() -> tuple:
    with db.connect() as c:
        items = c.execute("SELECT id, status, meta FROM items ORDER BY id").fetchall()
        reviews = c.execute("SELECT id, resolved FROM review_queue ORDER BY id").fetchall()
        rows = c.execute("SELECT id, status, meta FROM ingest_queue ORDER BY id").fetchall()
    return items, reviews, rows


def _item_row(iid: int) -> tuple[str, dict]:
    with db.connect() as c:
        return c.execute("SELECT status, meta FROM items WHERE id = %s", (iid,)).fetchone()


def _queue_meta(rid: int) -> tuple[str, dict]:
    with db.connect() as c:
        return c.execute("SELECT status, meta FROM ingest_queue WHERE id = %s", (rid,)).fetchone()


def _resolved(rq: int) -> bool:
    with db.connect() as c:
        return c.execute("SELECT resolved FROM review_queue WHERE id = %s", (rq,)).fetchone()[0]


BASE = ["repair", "resend-dups", "--session", PREFIX, "--replayed", "--since", SINCE.isoformat()]


def test_plan_reads_c_r_w0_new_and_decides_items_by_new_content():
    ids = _seed()
    p = resend_repair_replay.plan(_sel())
    assert p.uri == URI and p.covered_end == 2000 and p.first_stamped_offset == 1000
    assert p.pre_status == {"extracted": 5} and p.pre_unstamped_rows == 3
    assert not p.prefix_short and p.apply_refusals == [], "the seed prefix holds [0, 1000)"
    assert (p.gaps, p.gap_bytes, p.r_in_gap) == (0, 0, 0), "the seed prefix holds [0, 1000)"
    assert p.r_ids == [ids["r1"], ids["r2"]] and p.r_status == {"extracted": 2}
    assert p.w0 == SINCE, "W0 = the first R row's created_at, never its group's processed_at"
    assert p.first_done == DONE and p.w1 == DONE
    assert p.new_ids == [ids["late"], ids["n1"]], "the late-drained legit row is NEW too"
    assert p.mark_ids == p.r_ids and p.marked_before == 0
    qemu, system = p.projects["i-qemu"], p.projects["kb-system"]
    assert qemu.archive_ids == [ids["dup1"]], "minted before processed_at, still a candidate"
    assert (qemu.candidates, qemu.keep_recalled, qemu.keep_no_quote) == (5, 1, 1)
    assert qemu.keep_new_content == 2, "quotes matched on the ASSEMBLED text (escaped quotes)"
    assert (qemu.keep_successor, qemu.keep_reprocess) == (0, 0)
    assert system.archive_ids == [ids["dup2"]] and system.candidates == 1
    assert set(p.projects) == {"i-qemu", "kb-system"}


def test_apply_archives_resolves_reviews_marks_r_and_undo_restores_exactly(monkeypatch):
    ids = _seed()
    before = _snapshot()
    monkeypatch.setattr(resend_repair_replay, "ARCHIVE_BATCH", 1)  # batches, one txn each
    p = resend_repair_replay.apply(resend_repair_replay.plan(_sel()))
    assert p.rows_marked == 2 and p.archive_count == 2
    for key in ("dup1", "dup2"):
        status, meta = _item_row(ids[key])
        assert status == "archived"
        assert meta["archived"]["reason"] == "repair:resend-replay"
        assert meta["archived"]["source_uri"] == URI and meta["archived"]["covered_end"] == 2000
        assert meta["repair"]["by"] == "resend-dups" and meta["repair"]["mode"] == "replay"
        assert meta["repair"]["since"] == SINCE.isoformat()
        assert meta["repair"]["review_ids"] == [ids[f"rq_{key}"]]
        assert _resolved(ids[f"rq_{key}"])
    assert not _resolved(ids["rq_new"]) and _item_row(ids["new"])[0] == "active"
    for key in ("late_i", "recalled", "noquote", "old", "verified", "summary"):
        assert _item_row(ids[key])[0] == "active", key
    for key in ("r1", "r2"):
        status, meta = _queue_meta(ids[key])
        assert status == "extracted", "R rows keep their status — the marker is audit only"
        assert meta["repair"]["mode"] == "replay" and meta["repair"]["covered_end"] == 2000
        assert (
            meta["repair"]["since"] == SINCE.isoformat() and meta["repair"]["by"] == "resend-dups"
        )
    for key in ("u1", "s1", "late", "n1"):
        assert "repair" not in _queue_meta(ids[key])[1], key

    again = resend_repair_replay.plan(_sel())
    assert again.archive_count == 0 and again.mark_ids == [] and again.marked_before == 2
    assert not again.eligible, "an idempotent re-apply plans nothing"
    plain = resend_repair.undo(RepairSelector(session=PREFIX), dry_run=True)
    assert plain.row_ids == [] and plain.item_ids == [], "the plain undo never sees a replay"

    dry = resend_repair_replay.undo(_sel(), dry_run=True)
    assert dry.row_ids == [ids["r1"], ids["r2"]] and dry.item_ids == [ids["dup1"], ids["dup2"]]
    assert dry.stored_since is None, "a matching key needs no hint"
    done = resend_repair_replay.undo(_sel(), dry_run=False)
    assert (done.rows_restored, done.items_restored, done.reviews_reopened) == (2, 2, 2)
    assert _snapshot() == before, "statuses, metas and review flags restored byte for byte"
    assert resend_repair_replay.undo(_sel(), dry_run=False).items_restored == 0


def test_gate_refuses_with_the_owners_real_options_never_a_new_since():
    _seed()
    bad = _q("i-qemu", "never mined", created=T0 + timedelta(days=2), processed=None,
             status="pending", stamp=(2000, 10))  # fmt: skip
    retry = _q("i-qemu", "failed once", created=T0 + timedelta(days=2), processed=T0,
               status="failed", attempts=1)  # fmt: skip
    term_at = T0 + timedelta(hours=5)
    term = _q("i-qemu", "failed for good", created=T0 + timedelta(days=2), processed=term_at,
              status="failed", attempts=3)  # fmt: skip
    with pytest.raises(InvalidOperation, match="not extracted/duplicate") as exc:
        resend_repair_replay.plan(_sel())
    msg = str(exc.value)
    assert f"{bad}  i-qemu  pending" in msg and msg.split(f"{bad}  i-qemu  pending")[1].split("\n")[
        0
    ].endswith("wait")
    assert f"{retry}  i-qemu  failed" in msg and f"{term}  i-qemu  failed" in msg
    assert "2 'wait': pending or retryable — re-run after the nightly drain" in msg
    assert "1 'requeue': failed for good (attempts >= 3)" in msg
    assert f"since=d.fromisoformat('{term_at.isoformat()}')" in msg
    assert "--since" not in msg.split("not extracted/duplicate")[1], "never 'move --since'"
    code = re.search(r'uv run python -c "(.+?)" \(it requeues', msg).group(1)
    exec(compile(code, "<gate-hint>", "exec"), {})  # the named call really runs (kb_test)
    assert _queue_meta(term)[0] == "pending", "the hint requeued the terminal row"


def test_gate_tags_parked_residue_and_never_offers_it_a_requeue():
    """Review 2 (replay hardening 3): a parked resend-residue row (failed, error LIKE
    'parked %') is tagged 'parked' and pointed at --as-resend; requeue_failed is
    offered only for the genuinely failed row, and its reach (EVERY project, parked
    residue included) is said."""
    _seed()
    parked = _q("i-qemu", "parked residue", created=T0 + timedelta(days=2), processed=T0,
                status="failed", attempts=3)  # fmt: skip
    with db.transaction() as c:
        c.execute(
            "UPDATE ingest_queue SET error = %s WHERE id = %s",
            ("parked 2026-09-10 owner: resend residue", parked),
        )
    with pytest.raises(InvalidOperation, match="not extracted/duplicate") as exc:
        resend_repair_replay.plan(_sel())
    msg = str(exc.value)
    assert msg.rstrip().endswith(
        f"{parked}  i-qemu  failed  {(T0 + timedelta(days=2)).isoformat()}  parked"
    )
    assert "'requeue':" not in msg and "requeue_failed(" not in msg, "parked: never a requeue"
    assert (
        f"1 'parked': resend residue parked as failed — NEVER requeue it (that re-extracts a "
        f"re-send); settle it as a duplicate first: kb repair resend-dups --session {SID} "
        "--as-resend (dry run, then --apply), then re-run."
    ) in msg
    # Round 3: --as-resend pairs by an exact (uri, content_hash) twin only; an
    # unpaired parked row stays failed and the gate refuses again -- the hint says
    # so and names the owner's path for it (the gate reads no hashes).
    assert (
        "--as-resend settles ONLY a parked row with an exact (uri, content_hash) twin (or a "
        "duplicate_of mark already on it); its dry run counts any other 'unpaired' and leaves "
        "it failed, and this gate refuses on it again."
    ) in msg
    assert "An unpaired parked row is the OWNER's call, by queue id:" in msg
    assert "only after --as-resend --apply settled the paired ones" in msg
    assert "no repair settles it — it stays parked and this session is not replayed" in msg
    term = _q("gateway", "failed for good", created=T0 + timedelta(days=2), processed=T0,
              status="failed", attempts=3, uri=URI)  # fmt: skip
    with pytest.raises(InvalidOperation) as exc:
        resend_repair_replay.plan(_sel())
    msg = str(exc.value)
    assert msg.split("\n")[-1].endswith("requeue") and f"{term}  gateway  failed" in msg
    assert (
        f"(it requeues EVERY failed row in EVERY project processed at/after {T0.isoformat()}, "
        "parked residue included — settle the 'parked' rows first)"
    ) in msg


def test_session_must_resolve_to_exactly_one_uri_and_flags_are_closed():
    _seed()
    other = "session:bd162e9f-ffff-4222-8333-444455556666"
    _q("gateway", "another session", created=T0, processed=T0, uri=other)
    with pytest.raises(InvalidOperation, match="ambiguous") as exc:
        resend_repair_replay.plan(_sel(session="bd162e9f"))
    assert URI in str(exc.value) and other in str(exc.value)
    with pytest.raises(InvalidOperation, match="matches no session"):
        resend_repair_replay.plan(_sel(session="zzzz"))
    with pytest.raises(InvalidOperation, match="needs --since"):
        resend_repair_replay.plan(_sel(since=None))
    with pytest.raises(InvalidOperation, match="not --project, --attribute-quotes"):
        resend_repair_replay.plan(_sel(project="i-qemu", attribute_quotes=True))
    assert resend_repair_replay.plan(_sel()).uri == URI


def test_cli_dry_run_prints_the_plan_and_the_exact_apply_command():
    ids = _seed()
    before = _snapshot()
    dry = runner.invoke(app, BASE)
    assert dry.exit_code == 0, dry.output
    out = dry.output
    assert f"session  {URI}" in out and "C = 2,000 bytes" in out
    assert "gate     5 row(s) before --since: extracted 5 -- ok" in out
    assert "R = 2 row(s)" in out and "NEW = 2 row(s)" in out and "gap -> NEW 0" in out
    assert f"W1 {DONE.isoformat()} (no candidate after W1)" in out
    assert "archive = candidates - recalled - no-quote - new-content - successor - reprocess" in out
    assert "automated brief/turn-start recall hits" in out
    # No CHECK/HOLD LINE (the fixture's padded seed prefix prints a NOTE whose text
    # mentions the CHECK; only a line that STARTS with CHECK/HOLD refuses).
    assert not any(ln.startswith(("CHECK ", "HOLD ")) for ln in out.splitlines())
    assert any(
        ln.split()[:8] == ["i-qemu", "5", "1", "1", "1", "2", "0", "0"] for ln in out.splitlines()
    )
    assert any(ln.split()[:3] == ["kb-system", "1", "1"] for ln in out.splitlines())
    cmd = f"kb repair resend-dups --session {PREFIX} --replayed --since {SINCE.isoformat()} --apply"
    assert cmd in out and _snapshot() == before, "the dry run wrote nothing"

    applied = runner.invoke(app, [*BASE, "--apply"], input="y\n")
    assert applied.exit_code == 0, applied.output
    assert "marked 2 row(s); archived 2 item(s) (reason repair:resend-replay)" in applied.output
    assert _item_row(ids["dup1"])[0] == "archived"
    wrong = [*BASE[:-1], (SINCE + timedelta(minutes=1)).isoformat(), "--undo"]
    miss = runner.invoke(app, wrong)
    assert miss.exit_code == 0, miss.output
    assert "nothing to undo for this selection." in miss.output
    assert f"replay markers on this session carry --since: {SINCE.isoformat()}" in miss.output
    undo = runner.invoke(app, [*BASE, "--undo", "--apply"], input="y\n")
    assert undo.exit_code == 0, undo.output
    assert "restored 2 row(s), 2 item(s); re-opened 2 review row(s)" in undo.output
    assert _snapshot() == before
    refused = runner.invoke(app, [*BASE[:-2], "--apply"])
    assert refused.exit_code == 1 and "needs --since" in refused.output


# --- review fixes (2026-10-11) -------------------------------------------------


def test_successor_and_reprocess_items_are_kept():
    """MUST-FIX 3: the nightly evidence auto-resolve retires a pre-W0 item INTO an R
    paraphrase (supersede(retire=X, new_item_id=Y)); archiving Y would leave X
    superseded by an archived item. Same for a folded duplicate and kb reprocess."""
    ids = _seed()
    mid = SINCE + timedelta(hours=3)
    succ, _ = _item("i-qemu", "tlb unit", "separate translation unit", mid)  # R bytes only
    supersede(ids["old"], new_item_id=succ, reason="evidence auto-resolve")
    canon, _ = _item("i-qemu", "ld paths", "for the ld_le paths", mid)  # R bytes only
    folded, _ = _item("i-qemu", "ld paths (old)", "ld_le", T0 + timedelta(hours=3))
    assert consolidation.mark_duplicate(folded, canon)
    rep, _ = _item("i-qemu", "softmmu again", "softmmu slow-path", mid,
                   meta={"reprocessed_from": 4242})  # fmt: skip
    p = resend_repair_replay.plan(_sel())
    qemu = p.projects["i-qemu"]
    assert (qemu.keep_successor, qemu.keep_reprocess) == (2, 1)
    assert qemu.archive_ids == [ids["dup1"]], "the successor/reprocess items are never archived"
    assert qemu.candidates == 5 + 3


def test_scrubbed_quote_of_a_new_line_is_kept():
    """MUST-FIX 4a: a secret visible only in the DECODED text escapes the enqueue
    scrub; the drain grounds the raw quote, then stores scrub(quote)."""
    _seed()
    fake = "hunter2-fake-pw9"  # a fake credential shape, never a real one
    raw = _msg("assistant", f"rotate the db password:\n  {fake} before the next deploy")
    stored_content, _ = scrub(raw)  # what enqueue stores (JSON-escaped: \\n, no match)
    assert fake in stored_content, "the enqueue scrub cannot see the escaped secret"
    _q("i-qemu", stored_content, created=SINCE + timedelta(minutes=3), processed=DONE,
       stamp=(2600, 400))  # fmt: skip
    text = assemble_text(stored_content)
    quote = f"password:\n  {fake} before the next deploy"
    assert quote in text, "the drain grounds the UNSCRUBBED quote"
    stored_quote = scrub(quote)[0]
    assert stored_quote != quote and stored_quote not in text
    iid, _ = _item("i-qemu", "db password rotation", quote, LATE_AT)
    assert _item_row(iid)[1]["quote"] == stored_quote, "stored exactly as the drain stores it"
    p = resend_repair_replay.plan(_sel())
    assert iid not in p.projects["i-qemu"].archive_ids
    assert p.projects["i-qemu"].keep_new_content == 3


def test_items_after_w1_are_not_candidates():
    """MUST-FIX 4b: nothing created after the last R row completed can be R's."""
    ids = _seed()
    after, _ = _item("i-qemu", "later echo", "lowers guest loads", DONE + timedelta(hours=1))
    p = resend_repair_replay.plan(_sel())
    assert p.w1 == DONE
    assert p.projects["i-qemu"].candidates == 5 and after not in p.projects["i-qemu"].archive_ids
    assert p.projects["i-qemu"].archive_ids == [ids["dup1"]]


def test_quote_spanning_two_new_rows_of_one_project_is_kept_across_another_projects_row():
    """MUST-FIX 4c: the drain joins a (project, uri) group; a kb-system row between
    two i-qemu rows by id must not break the i-qemu line."""
    _seed()
    line = _msg("assistant", "the guest base moved to 0x7f0000 after the reserved_va fix")
    cut = line.index("0x7f") + 4
    at = SINCE + timedelta(minutes=4)
    _q("i-qemu", line[:cut], created=at, processed=DONE, stamp=(2600, cut))
    _q("kb-system", _msg("assistant", "unrelated kb-system chatter"), created=at,
       processed=DONE, stamp=(2600 + cut, 50))  # fmt: skip
    _q("i-qemu", line[cut:], created=at, processed=DONE, stamp=(2650 + cut, len(line) - cut))
    iid, _ = _item("i-qemu", "guest base move", "moved to 0x7f0000 after", LATE_AT)
    p = resend_repair_replay.plan(_sel())
    assert iid not in p.projects["i-qemu"].archive_ids
    assert p.projects["i-qemu"].keep_new_content == 3


def test_apply_refused_while_an_r_row_is_pending_or_retryable():
    """MUST-FIX 4b: the drain would mint an undrained R row's items AFTER the archive."""
    _seed()
    at = SINCE + timedelta(minutes=5)
    _q("i-qemu", _msg("assistant", A1 + " (re-sent)"), created=at, processed=None,
       status="pending", stamp=(0, 400))  # fmt: skip
    _q("i-qemu", _msg("assistant", A2 + " (re-sent)"), created=at, processed=DONE,
       status="failed", attempts=1, stamp=(400, 400))  # fmt: skip
    p = resend_repair_replay.plan(_sel())
    assert p.r_undrained == 2 and len(p.r_ids) == 4
    before = _snapshot()
    with pytest.raises(InvalidOperation, match="re-run after the drain"):
        resend_repair_replay.apply(p)
    dry = runner.invoke(app, BASE)
    assert dry.exit_code == 0, dry.output
    assert (
        "HOLD     2 R row(s) still pending or retryable -- --apply refused: re-run after the drain"
        in dry.output
    )
    assert "--apply is refused: 2 R row(s) still pending or retryable" in dry.output
    assert "--apply\n" not in dry.output, "no apply command is offered"
    refused = runner.invoke(app, [*BASE, "--apply"], input="y\n")
    assert refused.exit_code == 1 and "replay --apply refused" in refused.output
    assert _snapshot() == before, "nothing written"


def test_pending_new_row_protects_its_items_and_kb_system_new_items_are_kept():
    ids = _seed()
    pend = "the pending window found the qemu-user mmap hint path at last"
    _q("i-qemu", _msg("assistant", pend), created=SINCE + timedelta(minutes=6), processed=None,
       status="pending", stamp=(2600, 300))  # fmt: skip
    sys_new = "kb-system learned the reroute rule for qemu notes tonight"
    _q("kb-system", _msg("assistant", sys_new), created=SINCE + timedelta(minutes=6),
       processed=DONE, stamp=(2900, 300))  # fmt: skip
    pi, _ = _item("i-qemu", "mmap hint", "mmap hint path at last", LATE_AT)
    si, _ = _item("kb-system", "reroute rule", "learned the reroute rule", LATE_AT)
    p = resend_repair_replay.plan(_sel())
    assert p.apply_refusals == [], "a pending NEW row never blocks the apply"
    assert pi not in p.projects["i-qemu"].archive_ids
    assert p.projects["kb-system"].archive_ids == [ids["dup2"]]
    assert p.projects["kb-system"].keep_new_content == 1
    resend_repair_replay.apply(p)
    assert _item_row(pi)[0] == "active" and _item_row(si)[0] == "active"


def test_stamp_gap_rows_move_to_new_and_their_items_are_kept():
    uri = "session:0ddc0ffe-0000-4000-8000-000000000000"
    _q("p", "seed only", created=T0, processed=T0, uri=uri)
    with pytest.raises(InvalidOperation, match="carries a flush stamp"):
        resend_repair_replay.plan(_sel(session="0ddc0ffe"))
    gappy = "session:9a9a9a9a-0000-4000-8000-000000000000"
    alpha = "alpha bytes the first stamped send already carried to the server"
    gamma = "gamma bytes that sit in the stamp gap and were maybe never sent"
    _q("p", "a", created=T0, processed=T0, uri=gappy, stamp=(0, 500))
    _q("p", "b", created=T0, processed=T0, uri=gappy, stamp=(800, 200))
    gap_row = _q("p", _msg("assistant", gamma), created=SINCE, processed=DONE, uri=gappy,
                 stamp=(400, 500))  # fmt: skip
    r_row = _q("p", _msg("assistant", alpha), created=SINCE, processed=DONE, uri=gappy,
               stamp=(0, 300))  # fmt: skip
    ia, _ = _item("p", "alpha", "first stamped send already", LATE_AT, uri=gappy)
    ig, _ = _item("p", "gamma", "sit in the stamp gap", LATE_AT, uri=gappy)
    p = resend_repair_replay.plan(_sel(session="9a9a9a9a"))
    assert (p.covered_end, p.gaps, p.gap_bytes, p.r_in_gap) == (1000, 1, 300, 1)
    assert p.r_ids == [r_row] and gap_row in p.new_ids and gap_row not in p.mark_ids
    assert p.projects["p"].archive_ids == [ia] and p.projects["p"].keep_new_content == 1
    assert ig not in p.projects["p"].archive_ids and p.apply_refusals == []
    out = runner.invoke(app, ["repair", "resend-dups", "--session", "9a9a9a9a", "--replayed",
                              "--since", SINCE.isoformat()]).output  # fmt: skip
    assert "gap -> NEW 1" in out and "CHECK" not in out


def test_short_unstamped_prefix_is_a_check_that_refuses_apply():
    uri = "session:c4ec4ec4-0000-4000-8000-000000000000"
    _q("p", _msg("user", "tiny seed"), created=T0, processed=T0, uri=uri)  # far below 1000
    _q("p", "s", created=T0 + timedelta(days=1), processed=T0, uri=uri, stamp=(1000, 500))
    _q("p", _msg("assistant", B1), created=SINCE, processed=DONE, uri=uri, stamp=(0, 1000))
    iid, _ = _item("p", "rewriter", "splits basic blocks", LATE_AT, uri=uri)
    p = resend_repair_replay.plan(_sel(session="c4ec4ec4"))
    assert p.prefix_short and p.gaps == 0 and p.projects["p"].archive_ids == [iid]
    before = _snapshot()
    with pytest.raises(InvalidOperation, match="CHECK unstamped prefix"):
        resend_repair_replay.apply(p)
    base = ["repair", "resend-dups", "--session", "c4ec4ec4", "--replayed", "--since",
            SINCE.isoformat()]  # fmt: skip
    dry = runner.invoke(app, base)
    assert dry.exit_code == 0 and "R = 1 row(s)" in dry.output, "the dry run prints the plan"
    check = "CHECK    unstamped prefix 0.0 MB is more than 10% short of the first stamp at 1,000"
    assert check in dry.output
    assert "--apply is refused: CHECK unstamped prefix" in dry.output
    refused = runner.invoke(app, [*base, "--apply"], input="y\n")
    assert refused.exit_code == 1 and "CHECK unstamped prefix" in refused.output
    assert _snapshot() == before


@pytest.mark.parametrize(
    ("prefix", "first", "short", "ratio"),
    [
        (221_000_000, 221_030_428, False, None),  # a seed prefix the first stamp's size
        (200_000_000, 221_030_428, False, None),  # inside the 10% tolerance
        (243_100_000, 221_030_428, False, None),  # 1.0999x: still inside it, no NOTE
        (190_000_000, 221_030_428, True, None),
        (0, 0, False, None),  # first stamp at 0: nothing to cover
        # rm1's bd162e9f as it is live: 570 unstamped rows / 484.5 MB under a first
        # stamp at 221,030,428 — a RE-SENT prefix (the earlier dup-chunk incident).
        # The CHECK is one-sided: a NOTE with the ratio, never a refusal.
        (484_500_000, 221_030_428, False, "2.19"),
    ],
)
def test_prefix_check_tolerance(prefix, first, short, ratio, caplog):
    rows = [
        rows_step.Row(1, "i-qemu", "extracted", T0, T0, None, 0, prefix, None),
        rows_step.Row(2, "i-qemu", "extracted", T0, T0, first, 1000, 1000, None),
    ]
    with caplog.at_level("WARNING", logger="kbcore.resend_repair"):
        p, _ = rows_step.coverage(URI, SINCE, rows)
    assert p.prefix_short is short
    note = rows_step.prefix_note(p)
    report = replay_report(p)
    if ratio is None:
        assert note is None and "NOTE" not in caplog.text and "\nNOTE " not in report
        return
    assert note == (
        f"NOTE     unstamped prefix 484.5 MB / first stamp at 221,030,428 = ratio {ratio} "
        "(over 1 + 10%): a re-sent prefix (an earlier incident stored those bytes more than "
        "once), so its size cannot show that every byte below the first stamp was sent -- the "
        "CHECK passes on size alone and --apply treats [0, first stamp) as covered on the "
        "owner's word"
    )
    assert note in caplog.text and note.isascii(), "printed (stderr) by the dry run"
    assert note in report.splitlines(), "and in the dry-run report beside CHECK/HOLD"
    assert p.apply_refusals == [], "a NOTE never refuses --apply"


def test_replay_apply_keeps_a_successor_made_between_plan_and_prompt(caplog):
    """Review 2 (replay hardening 1): the successor keep is re-checked at WRITE time,
    so an item that becomes a supersession target — or a folded duplicate's twin —
    while the owner's prompt is open is kept, its review row stays open, and the
    skip is counted and logged."""
    ids = _seed()
    p = resend_repair_replay.plan(_sel())
    assert p.projects["i-qemu"].archive_ids == [ids["dup1"]]
    assert p.projects["kb-system"].archive_ids == [ids["dup2"]]
    supersede(ids["old"], new_item_id=ids["dup1"], reason="evidence auto-resolve")
    folded, _ = _item("kb-system", "relocs (old)", "replayed", T0 + timedelta(hours=3))
    assert consolidation.mark_duplicate(folded, ids["dup2"])
    with caplog.at_level("WARNING", logger="kbcore.resend_repair"):
        done = resend_repair_replay.apply(p)
    assert done.projects["i-qemu"].archived == done.projects["kb-system"].archived == 0
    assert done.rows_marked == 2, "R is still marked: the rows' audit is independent"
    for key in ("dup1", "dup2"):
        status, meta = _item_row(ids[key])
        assert status == "active" and "repair" not in meta and not _resolved(ids[f"rq_{key}"])
    assert caplog.text.count("kept 1 of 1 planned item(s) at write time -- a successor") == 2


def test_tiers_1_2_never_overwrite_a_replay_marker():
    """A replay-marked row requeued to 'pending' later must keep the marker the
    replay --undo is keyed on: skipped and counted 'other marker'."""
    uri = "session:7e7e7e7e-0000-4000-8000-000000000000"
    canon = _q("aquamonix", "canonical chunk", created=T0, processed=T0, uri=uri)
    marker = {"mode": "replay", "by": "resend-dups", "since": SINCE.isoformat()}
    marked = _q("docker-mount", "re-sent chunk a", created=SINCE, processed=None,
                status="pending", uri=uri, extra={"repair": marker},
                duplicate_of=canon)  # fmt: skip
    plain = _q("docker-mount", "re-sent chunk b", created=SINCE, processed=None,
               status="pending", uri=uri, duplicate_of=canon)  # fmt: skip
    sel = RepairSelector(session="7e7e7e7e", rows_only=True)
    (group,) = [g for g in resend_repair.plan(sel) if g.project == "docker-mount"]
    assert group.flip == {plain: canon} and group.pend == 1
    assert group.keep == {"other marker": 1} and "keep: other marker (1)" in group.decision
    resend_repair.apply([group], sel)
    assert _queue_meta(plain)[0] == "duplicate"
    status, meta = _queue_meta(marked)
    assert status == "pending" and meta["repair"] == marker, "the replay marker survives"
    as_resend = RepairSelector(session="7e7e7e7e", rows_only=True, as_resend=True)
    (g2,) = [g for g in resend_repair.plan(as_resend) if g.project == "docker-mount"]
    assert g2.flip == {} and g2.keep == {"other marker": 1}
    race = GroupPlan(project="docker-mount", uri=uri, rows=2, dup=2, pend=1, extr=0,
                     flip={marked: canon})  # fmt: skip
    assert tier_rows.flip(race, reason="resend duplicate", as_resend=False) == 0
    assert _queue_meta(marked)[1]["repair"] == marker

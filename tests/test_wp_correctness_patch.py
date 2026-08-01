"""Correctness-patch batch — surface coverage for the reviewed WP1/WP2/queue fixes.

MCP tool layer (FastMCP in-memory Client), REST (Starlette TestClient), CLI
(typer CliRunner), and the ingest-queue self-heal taxonomy. Real DB in kb_test;
Ollama mocked by conftest. Groups the tests that cross a public surface so the
WP1/WP2 unit files stay focused on kbcore behavior.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from starlette.testclient import TestClient
from typer.testing import CliRunner

from kbcore import db, graph, ingest_queue
from kbcore.config import settings
from kbcore.errors import ExtractionFailed, ExtractorUnavailable
from kbcore.ingest import ingest
from kbcore.items import get_item
from kbworker import consolidate, sessions

TEST_TOKEN = "kbtest-" + "0" * 32
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


def _blind(item_id: int) -> int:
    """NULL an item's embedding so only graph expansion can surface it."""
    with db.transaction() as conn:
        conn.execute("UPDATE chunks SET embedding = NULL WHERE item_id = %s", (item_id,))
    return item_id


# --- MCP tool layer (kb_search as_of/expand, kb_save/kb_supersede valid-time) --


def _mcp_call(tool: str, args: dict) -> str:
    from kbmcp.server import mcp

    async def run() -> str:
        async with Client(mcp) as client:
            res = await client.call_tool(tool, args)
            return res.content[0].text

    return asyncio.run(run())


def test_mcp_kb_save_and_search_valid_time(kb):
    out = _mcp_call(
        "kb_save",
        {
            "project": "mcpvt",
            "type": "note",
            "title": "vt fact",
            "body": "widgetzap is the fact",
            "valid_from": "2026-01-01",
            "valid_to": "2026-12-31",
        },
    )
    assert "saved item" in out
    assert "vt fact" in _mcp_call("kb_search", {"query": "widgetzap", "as_of": "2026-06-01"})
    assert "vt fact" not in _mcp_call("kb_search", {"query": "widgetzap", "as_of": "2027-06-01"})


def test_mcp_kb_save_rejects_inverted_interval(kb):
    with pytest.raises(ToolError, match="strictly before"):
        _mcp_call(
            "kb_save",
            {
                "project": "mcpinv",
                "type": "note",
                "title": "inv",
                "body": "x",
                "valid_from": "2026-12-01",
                "valid_to": "2026-01-01",
            },
        )


def test_mcp_kb_search_expand_pulls_the_linked_neighbor(kb):
    bug = ingest("mcpx", "bug", "Gearbox stalls", "the gearbox stalls under load")
    fix = ingest("mcpx", "note", "Coupling fix", "replace the coupling")
    graph.link(fix, "fixes", bug)
    _blind(fix)
    assert "Coupling fix" in _mcp_call("kb_search", {"query": "gearbox", "expand": True})
    assert "Coupling fix" not in _mcp_call("kb_search", {"query": "gearbox", "expand": False})


def test_mcp_kb_supersede_override_and_autoclose(kb):
    save = _mcp_call(
        "kb_save",
        {
            "project": "mcpss",
            "type": "note",
            "title": "orig",
            "body": "widgetsup original fact",
            "valid_from": "2024-01-01",
        },
    )
    old_id = int(re.search(r"item (\d+)", save).group(1))
    out = _mcp_call(
        "kb_supersede",
        {
            "old_id": old_id,
            "reason": "moved",
            "new_body": "widgetsup moved fact",
            "valid_from": "2025-01-01",
        },
    )
    assert "superseded by" in out
    assert get_item(old_id).valid_to == datetime(2025, 1, 1, tzinfo=UTC), "old window auto-closed"


# --- REST surface (as_of/expand search, valid-time item + supersede) ----------


@pytest.fixture()
def client(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbrest.server import build_app

    return TestClient(build_app())


def test_rest_post_item_valid_time_and_interval_dedupe(client):
    body = {
        "project": "rvt",
        "type": "note",
        "title": "proto",
        "body": "widget uses proto",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2026-07-01T00:00:00Z",
    }
    r = client.post("/api/items", headers=AUTH, json=body)
    assert r.status_code == 201 and r.json()["duplicate"] is False
    first_id = r.json()["item_id"]
    # same text, DIFFERENT interval → a distinct assertion, not a duplicate
    r2 = client.post(
        "/api/items",
        headers=AUTH,
        json={**body, "valid_from": "2026-07-01T00:00:00Z", "valid_to": None},
    )
    assert r2.json()["duplicate"] is False and r2.json()["item_id"] != first_id
    # exact same interval again → duplicate, pointing back at the first row
    r3 = client.post("/api/items", headers=AUTH, json=body)
    assert r3.json()["duplicate"] is True and r3.json()["item_id"] == first_id
    # GET echoes the interval
    g = client.get(f"/api/items/{first_id}", headers=AUTH)
    assert g.json()["valid_from"].startswith("2026-01-01")


def test_rest_post_item_rejects_inverted_interval(client):
    r = client.post(
        "/api/items",
        headers=AUTH,
        json={
            "project": "rinv",
            "type": "note",
            "title": "inv",
            "body": "x",
            "valid_from": "2026-12-01T00:00:00Z",
            "valid_to": "2026-01-01T00:00:00Z",
        },
    )
    assert r.status_code == 400 and "strictly before" in r.json()["error"]


def test_rest_supersede_override_and_autoclose(client):
    old = ingest(
        "rss", "note", "orig", "widget original", valid_from=datetime(2024, 1, 1, tzinfo=UTC)
    )
    r = client.post(
        f"/api/items/{old}/supersede",
        headers=AUTH,
        json={"reason": "moved", "new_body": "widget moved", "valid_from": "2025-01-01T00:00:00Z"},
    )
    assert r.status_code == 200
    new_id = r.json()["new_item_id"]
    assert client.get(f"/api/items/{new_id}", headers=AUTH).json()["valid_from"].startswith("2025")
    assert client.get(f"/api/items/{old}", headers=AUTH).json()["valid_to"].startswith("2025")


def test_rest_search_expand_and_as_of(client):
    bug = ingest(
        "rse",
        "bug",
        "Gearbox stalls",
        "the gearbox stalls",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
    )
    fix = ingest(
        "rse",
        "note",
        "Coupling fix",
        "replace the coupling",
        valid_from=datetime(2026, 9, 1, tzinfo=UTC),
    )
    graph.link(fix, "fixes", bug)
    _blind(fix)
    ids = [
        h["item_id"]
        for h in client.get(
            "/api/search", params={"q": "gearbox", "expand": "true"}, headers=AUTH
        ).json()
    ]
    assert bug in ids and fix in ids, "expand=true pulls the linked neighbor in"
    off = [
        h["item_id"]
        for h in client.get(
            "/api/search", params={"q": "gearbox", "expand": "false"}, headers=AUTH
        ).json()
    ]
    assert bug in off and fix not in off, "expand=false forces expansion OFF"
    # as_of before the fix's window + expand → the neighbor is walled out
    scoped = client.get(
        "/api/search",
        params={"q": "gearbox", "expand": "true", "as_of": "2026-06-01"},
        headers=AUTH,
    ).json()
    ids2 = [h["item_id"] for h in scoped]
    assert bug in ids2 and fix not in ids2, "neighbor outside the as_of window is excluded"


# --- CLI surface (--valid-from/--valid-to, --as-of) ---------------------------


def test_cli_add_valid_time_and_search_as_of(kb):
    from kbcli.main import app

    runner = CliRunner()
    r = runner.invoke(
        app,
        [
            "add",
            "-p",
            "clivt",
            "--title",
            "cli fact",
            "--body",
            "widgetcli fact",
            "--valid-from",
            "2026-01-01",
            "--valid-to",
            "2026-12-31",
        ],
    )
    assert r.exit_code == 0 and "saved item" in r.output
    assert "cli fact" in runner.invoke(app, ["search", "widgetcli", "--as-of", "2026-06-01"]).output
    assert (
        "cli fact"
        not in runner.invoke(app, ["search", "widgetcli", "--as-of", "2027-06-01"]).output
    )


def test_cli_add_rejects_inverted_interval(kb):
    from kbcli.main import app

    r = CliRunner().invoke(
        app,
        [
            "add",
            "-p",
            "cliinv",
            "--title",
            "inv",
            "--body",
            "x",
            "--valid-from",
            "2026-12-01",
            "--valid-to",
            "2026-01-01",
        ],
    )
    assert r.exit_code == 1 and "strictly before" in r.output


def test_cli_supersede_valid_from_autocloses(kb):
    from kbcli.main import app

    old = ingest(
        "cliss", "note", "orig", "widget original", valid_from=datetime(2024, 1, 1, tzinfo=UTC)
    )
    r = CliRunner().invoke(
        app,
        [
            "supersede",
            str(old),
            "--reason",
            "moved",
            "--body",
            "widget moved",
            "--valid-from",
            "2025-01-01",
        ],
    )
    assert r.exit_code == 0 and "superseded by" in r.output
    assert get_item(old).valid_to == datetime(2025, 1, 1, tzinfo=UTC)


# --- ingest-queue self-heal taxonomy (connectivity vs content) ----------------


def _one_row(project: str) -> tuple[str, int]:
    with db.connect() as conn:
        return conn.execute(
            "SELECT status, attempts FROM ingest_queue WHERE project = %s", (project,)
        ).fetchone()


def test_drain_connectivity_failure_leaves_row_pending_attempts_unchanged(kb, monkeypatch):
    ingest_queue.enqueue("qconn", "User: a\nAssistant: b", kind="session")

    def down(*a, **k):
        raise ExtractorUnavailable("ollama restarting")

    monkeypatch.setattr(sessions, "extract_and_ingest", down)
    consolidate._drain_ingest_queue()
    assert _one_row("qconn") == ("pending", 0), "an outage defers without burning an attempt"


def test_drain_content_failure_marks_failed_and_is_bounded(kb, monkeypatch):
    ingest_queue.enqueue("qcontent", "User: a\nAssistant: b", kind="session")

    def bad(*a, **k):
        raise ExtractionFailed("unparseable model output")

    monkeypatch.setattr(sessions, "extract_and_ingest", bad)
    for _ in range(5):  # more runs than the cap
        consolidate._drain_ingest_queue()
    assert _one_row("qcontent") == ("failed", ingest_queue.MAX_ATTEMPTS), "poison row stops at cap"


def test_terminal_row_is_requeued_on_resubmit(kb, monkeypatch):
    content = "User: alpha\nAssistant: beta"
    first = ingest_queue.enqueue("qrequeue", content, kind="session")
    assert first.id is not None and first.requeued is False

    def bad(*a, **k):
        raise ExtractionFailed("unparseable")

    monkeypatch.setattr(sessions, "extract_and_ingest", bad)
    for _ in range(5):
        consolidate._drain_ingest_queue()
    assert _one_row("qrequeue") == ("failed", ingest_queue.MAX_ATTEMPTS)

    # resubmitting identical content re-opens the terminal row (status flip)
    again = ingest_queue.enqueue("qrequeue", content, kind="session")
    assert again.id == first.id and again.requeued is True
    assert _one_row("qrequeue") == ("pending", 0)


def test_drain_deferral_cap_escalates_a_never_healing_transient(kb, monkeypatch):
    # A payload-deterministic 5xx surfaces as ExtractorUnavailable (transient) and
    # is DEFERRED forever with attempts untouched — until the deferral cap escalates
    # it to a terminal 'failed' row so it can't re-drain every night (finding).
    ingest_queue.enqueue("qdefer", "User: a\nAssistant: b", kind="session")

    def down(*a, **k):
        raise ExtractorUnavailable("deterministic 500 / runner crash")

    monkeypatch.setattr(sessions, "extract_and_ingest", down)
    for _ in range(ingest_queue.DEFERRAL_CAP + 2):
        consolidate._drain_ingest_queue()
    assert _one_row("qdefer") == ("failed", ingest_queue.MAX_ATTEMPTS), (
        "a transient that never heals is escalated to terminal after the deferral cap"
    )


def test_deferred_row_sorts_after_fresh_pending(kb):
    # The ordering amplifier: a stuck deferred low-id row must not sit at the HEAD
    # of every batch. Sorting by deferrals before id drifts it behind fresh work.
    old = ingest_queue.enqueue("qord", "User: old\nAssistant: x", kind="session").id
    ingest_queue.mark_deferred(old, "transient blip")  # deferrals=1, still pending
    new = ingest_queue.enqueue("qord", "User: new\nAssistant: y", kind="session").id
    ids = [r["id"] for r in ingest_queue.pending(limit=50)]
    assert ids.index(new) < ids.index(old), "fresh pending drains before a deferred low-id row"


def test_requeue_is_bounded_then_permanently_failed(kb, monkeypatch):
    # A recurring identical poison submission can re-arm a terminal row only
    # MAX_REQUEUES times; past that it stays failed and reports permanently_failed
    # (with the id) so the caller routes it to manual review (finding).
    content = "User: poison\nAssistant: payload"
    ingest_queue.enqueue("qpoison", content, kind="session")

    def bad(*a, **k):
        raise ExtractionFailed("deterministic poison")

    monkeypatch.setattr(sessions, "extract_and_ingest", bad)
    for _ in range(ingest_queue.MAX_REQUEUES):
        for _ in range(5):  # drive the (re-opened) row back to terminal
            consolidate._drain_ingest_queue()
        r = ingest_queue.enqueue("qpoison", content, kind="session")
        assert r.requeued is True and r.permanently_failed is False

    for _ in range(5):
        consolidate._drain_ingest_queue()
    final = ingest_queue.enqueue("qpoison", content, kind="session")
    assert final.permanently_failed is True and final.requeued is False
    assert final.id is not None, "the terminal row id is returned for manual review"
    assert _one_row("qpoison")[0] == "failed", "the row stays terminally failed, not re-armed"


def test_pending_row_resubmit_is_a_noop_not_a_requeue(kb):
    content = "User: gamma\nAssistant: delta"
    first = ingest_queue.enqueue("qnoop", content, kind="session")
    assert first.id is not None and first.requeued is False
    again = ingest_queue.enqueue("qnoop", content, kind="session")
    assert again.id is None and again.requeued is False, "a live pending row is never disturbed"


def test_mcp_ingest_session_message_distinguishes_requeue(kb, monkeypatch):
    content = "User: epsilon\nAssistant: zeta"
    ingest_queue.enqueue("qmcpmsg", content, kind="session")

    def bad(*a, **k):
        raise ExtractionFailed("unparseable")

    monkeypatch.setattr(sessions, "extract_and_ingest", bad)
    for _ in range(5):
        consolidate._drain_ingest_queue()
    out = _mcp_call("kb_ingest_session", {"project": "qmcpmsg", "transcript": content})
    assert "re-queued after prior failure" in out


# --- extractor determinism (temperature=0, fixed seed) ------------------------


def test_extract_items_pins_deterministic_options(monkeypatch):
    from kbcore import llm
    from kbcore.config import settings

    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": '{"items": []}'}}

    def fake_post(url, json=None, timeout=None):
        captured.update(json)
        return _Resp()

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    llm.extract_items("some transcript text")
    # Determinism-for-retry comes from the pinned SEED alone; temperature=0 (greedy
    # decoding, forbidden on qwen3 thinking mode) was dropped (review finding).
    # num_ctx rides along since 2026-07-18: the runtime default (4K) silently
    # truncated transcripts and cut JSON mid-object (see test_llm_num_ctx).
    assert captured["options"] == {"seed": llm.EXTRACT_SEED, "num_ctx": settings.kb_llm_num_ctx}
    assert "temperature" not in captured["options"], "no greedy decoding; seed pins the retry"


# --------------------------------------------------------------------------
# Session coalescing. The network path windows RAW JSONL at 900 KB while the
# local path assembles to text first, so the same session arrives as ~42x more
# rows over MCP/REST (measured on a real transcript: 114 MB of JSONL is 2.7 MB
# of conversation — 350 rows vs 9 for a 300 MB session). Extracting each raw
# window separately spent the entire 200-row nightly budget on ONE session.
# --------------------------------------------------------------------------


def _queue_session_chunks(project: str, uri: str, n: int) -> list[int]:
    """n sequential chunks of one session, as the hook submits them."""
    import json

    ids = []
    for i in range(n):
        line = json.dumps({"type": "user", "i": i, "text": f"chunk {i} body"}) + "\n"
        res = ingest_queue.enqueue(project, line, kind="session", source_uri=uri)
        ids.append(res.id)
    return ids


def test_one_sessions_chunks_drain_as_ONE_extraction(kb, monkeypatch):
    """The whole point: N rows for one session must become ONE unit of work."""
    from kbcore.db import transaction

    ids = _queue_session_chunks("coalesce-proj", "session:big", 6)
    assert all(ids), "setup did not queue six rows"

    # Age them past the quiet period — a live session is still being written.
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET created_at = now() - interval '2 hours' "
            "WHERE meta->>'source_uri' = 'session:big'"
        )

    groups = ingest_queue.pending_groups(limit=200)
    # Groups are keyed by (project, session) now — assert on the raw session id,
    # which the group carries alongside its composite key.
    mine = [g for g in groups if g["source_uri"] == "session:big"]
    assert len(mine) == 1, f"the session did not coalesce: {len(mine)} groups"
    assert len(mine[0]["rows"]) == 6, "not every chunk joined the group"
    # Row order is the hook's byte order; concatenation must reconstitute it.
    assert [r["id"] for r in mine[0]["rows"]] == sorted(ids)


def test_a_session_still_being_written_is_NOT_drained_in_halves(kb):
    """Coalescing eagerly would extract half a session and the rest later."""
    _queue_session_chunks("coalesce-proj", "session:live", 4)
    uris = {g["source_uri"] for g in ingest_queue.pending_groups(limit=200)}
    assert "session:live" not in uris, "a fresh multi-chunk session was drained mid-flight"


def test_a_single_submission_is_never_delayed(kb):
    """The wait exists only to avoid splitting a session. One row cannot split,
    so an ad-hoc submission must stay as responsive as before coalescing."""
    res = ingest_queue.enqueue("coalesce-proj", "a one-shot note", kind="session")
    uris = {g["uri"] for g in ingest_queue.pending_groups(limit=200)}
    assert ingest_queue.group_key("coalesce-proj", f"queue:session:{res.id}") in uris, (
        "a single submission was delayed by the quiet period"
    )


def test_a_failed_group_fails_WHOLE(kb):
    """Marking one row and leaving the rest pending would split the session on
    the next run — exactly what coalescing prevents."""
    from kbcore.db import connect, transaction

    ids = _queue_session_chunks("coalesce-proj", "session:bad", 3)
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET created_at = now() - interval '2 hours' "
            "WHERE meta->>'source_uri' = 'session:bad'"
        )
    for rid in ids:
        ingest_queue.mark_failed(rid, "boom")
    with connect() as conn:
        states = [
            r[0]
            for r in conn.execute(
                "SELECT status FROM ingest_queue WHERE id = ANY(%s)", (ids,)
            ).fetchall()
        ]
    assert set(states) == {"failed"}, f"the group split across states: {states}"


def test_marking_a_group_extracted_is_atomic(kb):
    from kbcore.db import connect

    ids = _queue_session_chunks("coalesce-proj", "session:ok", 4)
    ingest_queue.mark_group_extracted(ids, saved=2, rejected=1)
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, saved_count FROM ingest_queue WHERE id = ANY(%s)", (ids,)
        ).fetchall()
    assert {r[0] for r in rows} == {"extracted"}, "not every row in the group was flipped"
    assert {r[1] for r in rows} == {2}

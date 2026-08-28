"""Wave G (review #6) — the typed-edge capture nudge (kbcore.save_nudge).

A kb_save of a bug/gotcha (or a zero-link fix-shaped save) gains up to 3
similar same-project ACTIVE bug items and a one-line kb_link hint in its
RESPONSE — text only, never an auto-link (Invariant 4 untouched), never under
public-only scope (no oracle), never able to fail the save. The weekly
typed-edge delta reads :func:`save_nudge.typed_edge_counts` (the recorded
contract: a cheap query for the weekly's checkpoint-diff pattern, not a
nightly counts key). Real DB in kb_test; Ollama is conftest's deterministic
fake_embeddings stub.
"""

from __future__ import annotations

import pytest

from kbcore import db, graph, save_nudge
from kbcore.config import settings
from kbcore.ingest import ingest

pytestmark = pytest.mark.usefixtures("kb")


# --- helpers -----------------------------------------------------------------


def _vec(*head: float) -> str:
    v = [float(x) for x in head] + [0.0] * (settings.kb_embed_dim - len(head))
    return "[" + ",".join(repr(x) for x in v) + "]"


def _force_lead(item_id: int, vec: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE chunks SET embedding = %s::vector WHERE item_id = %s AND ord = 0",
            (vec, item_id),
        )


def _bug(project: str, title: str, vec: str, *, status: str = "active") -> int:
    item_id = ingest(project, "bug", title, f"body of {title}", uri=f"u:{project}:{title}")
    _force_lead(item_id, vec)
    if status != "active":
        with db.transaction() as conn:
            conn.execute("UPDATE items SET status = %s WHERE id = %s", (status, item_id))
    return item_id


def _saved(project: str, type_: str, title: str, vec: str) -> int:
    item_id = ingest(project, type_, title, f"body of {title}", uri=f"u:new:{title}")
    _force_lead(item_id, vec)
    return item_id


# --- the probe: same-project active bugs, cosine-ordered, capped at 3 ---------


def test_bug_save_nudges_top3_same_project_active_bugs(kb):
    near = _bug("p", "spi cs glitch", _vec(1.0, 0.0))
    mid = _bug("p", "spi timing drift", _vec(0.9, 0.1))
    far = _bug("p", "uart parity flake", _vec(0.5, 0.5))
    _bug("p", "watchdog reset loop", _vec(0.0, 1.0))  # 4th — capped out
    other = _bug("q", "spi cs glitch elsewhere", _vec(1.0, 0.0))  # other PROJECT
    archived = _bug("p", "old spi bug", _vec(1.0, 0.0), status="archived")
    saved = _saved("p", "bug", "spi chip-select races", _vec(1.0, 0.01))

    text = save_nudge.suffix("p", "bug", "spi chip-select races", saved, False)

    assert "similar active bug items in p:" in text
    assert f"[{near}] spi cs glitch" in text
    assert f"[{mid}] spi timing drift" in text
    assert f"[{far}] uart parity flake" in text
    assert text.index(f"[{near}]") < text.index(f"[{mid}]") < text.index(f"[{far}]")
    assert "watchdog reset loop" not in text, "capped at NUDGE_CANDIDATES"
    assert str(other) not in text, "same-project only"
    assert str(archived) not in text, "active only"
    assert f"kb_link src_item_id={saved} rel=fixes dst_item_id=<id>" in text
    with db.connect() as conn:  # NEVER an auto-link (Invariant 4)
        n = conn.execute("SELECT count(*) FROM relations WHERE rel = 'fixes'").fetchone()[0]
    assert n == 0


def test_trigger_matrix_bug_gotcha_and_fix_shaped_zero_link_saves(kb):
    bug = _bug("p", "i2c bus lockup", _vec(1.0))
    saved = _saved("p", "note", "workaround for i2c lockup", _vec(1.0, 0.001))

    assert save_nudge.suffix("p", "gotcha", "any title", saved, False), "gotcha always probes"
    assert save_nudge.suffix("p", "note", "Fixed the i2c lockup", saved, False), "fix-shaped title"
    assert save_nudge.suffix("p", "note", "Fixed the i2c lockup", saved, True) == "", (
        "explicit links = the agent already linked; zero-link saves only"
    )
    assert save_nudge.suffix("p", "note", "meeting notes", saved, False) == ""
    assert save_nudge.suffix("p", "bug", "another lockup", saved, True), (
        "bug/gotcha probe even WITH links"
    )
    assert str(bug) in save_nudge.suffix("p", "gotcha", "any title", saved, False)


def test_public_only_scope_skips_the_nudge_entirely(kb, monkeypatch):
    _bug("p", "public bug", _vec(1.0))
    saved = _saved("p", "bug", "public save", _vec(1.0, 0.001))
    monkeypatch.setattr(settings, "kb_public_only", True)
    assert save_nudge.suffix("p", "bug", "public save", saved, False) == ""


def test_probe_failure_is_swallowed_never_raised(kb, monkeypatch):
    def _boom():
        raise RuntimeError("db went away")

    monkeypatch.setattr(save_nudge, "connect", _boom)
    assert save_nudge.suffix("p", "bug", "any", 1, False) == ""


def test_no_candidates_or_no_lead_embedding_nudges_nothing(kb):
    saved = _saved("p", "bug", "lonely bug", _vec(1.0))
    assert save_nudge.suffix("p", "bug", "lonely bug", saved, False) == ""
    with db.transaction() as conn:
        conn.execute("UPDATE chunks SET embedding = NULL WHERE item_id = %s", (saved,))
    _bug("p", "some bug", _vec(1.0))
    assert save_nudge.suffix("p", "bug", "lonely bug", saved, False) == ""


# --- end to end: the MCP kb_save RESPONSE carries the nudge -------------------


def test_mcp_kb_save_response_gains_the_nudge_suffix(kb):
    from kbmcp import server

    bug = _bug("p", "pwm dead-time bug", _vec(1.0))
    msg = server.kb_save("p", "gotcha", "pwm dead-time gotcha", "set dead-time before enable")
    assert msg.startswith("saved item ")
    assert f"[{bug}] pwm dead-time bug" in msg
    assert "rel=fixes" in msg

    msg = server.kb_save("p", "note", "plain reference note", "nothing fix-shaped here")
    assert msg.startswith("saved item ") and "rel=fixes" not in msg


# --- the weekly's delta source ------------------------------------------------


def test_typed_edge_counts_zero_filled_and_counting(kb):
    assert save_nudge.typed_edge_counts() == {"fixes": 0, "depends_on": 0, "implements": 0}
    a = ingest("p", "bug", "bug a", "body a", uri="u:a")
    b = ingest("p", "note", "fix b", "body b", uri="u:b")
    c = ingest("p", "note", "impl c", "body c", uri="u:c")
    graph.link(b, "fixes", a)
    graph.link(c, "implements", a)
    graph.link(c, "mentions", a)  # not a tracked typed edge
    assert save_nudge.typed_edge_counts() == {"fixes": 1, "depends_on": 0, "implements": 1}

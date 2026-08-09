"""Wave-3 search surface + ranking dials (design review #2, KB 16601).

Four additive changes, each shipped so TODAY's ranking is byte-identical (a flag
defaulted False, or a dial defaulted to its no-op value):

(1) include_duplicate — Invariant-5 'retrievable with flags' for marked-duplicate
    items, which no agent-surface knob reached before;
(2) kb_trust_factor — the real form of V4's "unverified ranked slightly lower":
    a multiplicative, near-tie-only nudge toward verified in _apply_bonuses;
(3) kb_id_leg_rare_tokens — for a NON-id-dominant query, admit the identifier leg
    for corpus-rare identifier tokens, fused below any real lexical hit;
(4) kb_collapse_alternatives — rank-time collapse of cross-project 'alternative_to'
    twins within one result set.

Real DB in kb_test via the `kb` fixture; Ollama mocked by conftest. The default
(flag-off / dial-at-1.0) paths are asserted here; the whole-corpus byte-identity
is the main agent's golden-eval gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kbcore import consolidation, db, graph
from kbcore.config import settings
from kbcore.ingest import ingest
from kbcore.search import _apply_bonuses, search
from kbcore.search_legs import RRF_K

pytestmark = pytest.mark.usefixtures("kb")


def _blind(item_id: int) -> int:
    """NULL the item's current-model embedding so the vector leg skips it."""
    with db.transaction() as conn:
        conn.execute("UPDATE chunks SET embedding = NULL WHERE item_id = %s", (item_id,))
    return item_id


# --- (1) include_duplicate ----------------------------------------------------


def test_marked_duplicate_absent_by_default_present_with_flag():
    canonical = ingest("p", "note", "canonical dup topic", "the duplitest canonical body text")
    dup = ingest("p", "note", "duplicate dup topic", "the duplitest duplicate body text")
    assert consolidation.mark_duplicate(dup, canonical) is True

    default_ids = [h.item_id for h in search("duplitest", k=10)]
    assert canonical in default_ids
    assert dup not in default_ids, "a marked-duplicate item is absent by default"

    with_dup = [h.item_id for h in search("duplitest", include_duplicate=True, k=10)]
    assert dup in with_dup, "the same item is present with include_duplicate=True"


def test_kb_search_surfaces_archived_and_duplicate_flags():
    """The MCP surface must expose BOTH new status flags and wire them through."""
    src = (Path(__file__).resolve().parent.parent / "src/kbmcp/server.py").read_text()
    assert "include_archived: bool = False" in src
    assert "include_duplicate: bool = False" in src
    assert "include_archived=include_archived" in src
    assert "include_duplicate=include_duplicate" in src


# --- (2) kb_trust_factor ------------------------------------------------------


def test_trust_factor_is_inert_at_one_and_flips_near_ties(monkeypatch):
    v = ingest("p", "note", "verified answer", "the verified canonical body", trust="verified")
    u = ingest("p", "note", "unverified answer", "the unverified other body", trust="unverified")
    s_v = 2.0 / (RRF_K + 2)  # verified sits just BELOW
    s_u = 2.0 / (RRF_K + 1)  # unverified just above it (a near-tie)

    # Default 1.0 is inert: scores untouched, order preserved (byte-identical).
    with db.connect() as conn:
        out = _apply_bonuses(conn, [(u, s_u, ""), (v, s_v, "")], None, [], {})
    scores = {i: s for i, s, _ in out}
    assert scores[u] == s_u and scores[v] == s_v, "at 1.0 the trust dial changes nothing"
    assert scores[u] > scores[v]

    # A small factor flips the near-tie toward the verified item.
    monkeypatch.setattr(settings, "kb_trust_factor", 1.05)
    with db.connect() as conn:
        out = _apply_bonuses(conn, [(u, s_u, ""), (v, s_v, "")], None, [], {})
    scores = {i: s for i, s, _ in out}
    assert scores[v] == pytest.approx(s_v * 1.05), "verified score multiplied by the factor"
    assert scores[u] == s_u, "unverified score untouched"
    assert scores[v] > scores[u], "the near-tie flipped toward verified"


def test_trust_factor_cannot_lift_weak_verified_over_strong_unverified(monkeypatch):
    v = ingest("p", "note", "weak verified", "weak verified body", trust="verified")
    u = ingest("p", "note", "strong unverified", "strong unverified body", trust="unverified")
    strong = 3.0 / (RRF_K + 1)  # unverified, strong (rank-1 in all legs)
    weak = 1.0 / (RRF_K + 1)  # verified, weak (single-leg rank-1)
    monkeypatch.setattr(settings, "kb_trust_factor", 1.05)
    with db.connect() as conn:
        out = _apply_bonuses(conn, [(u, strong, ""), (v, weak, "")], None, [], {})
    scores = {i: s for i, s, _ in out}
    assert scores[u] > scores[v], "a strong unverified answer stays above a weak verified one"


# --- (3) kb_id_leg_rare_tokens ------------------------------------------------


def test_rare_token_id_leg_surfaces_a_symbol_that_misses_today(monkeypatch):
    """A rare identifier hiding in a path token (tsv-invisible) on a vector-blinded
    item is unreachable by a NON-id-dominant query today; the rare-token leg reaches
    it once enabled, without changing the id-dominant path."""
    # Body shares NO lexeme with the query, and the symbol sits inside a path token
    # (lexed as one whole-path lexeme), so tsquery for zprq_write cannot reach it.
    target = ingest("p", "note", "fixup", "Fixed at /src/drv/zprq_write.c around offset 9.")
    for i in range(3):
        ingest("p", "note", f"filler {i}", f"unrelated chatter paragraph number {i}")
    _blind(target)  # vector leg cannot return it; the path token hides it from tsquery
    q = "where is the zprq_write routine defined in the driver code"  # non-id-dominant

    off = [h.item_id for h in search(q, k=10)]
    assert target not in off, "the symbol misses today (blinded + tsv-invisible + not dominant)"

    monkeypatch.setattr(settings, "kb_id_leg_rare_tokens", True)
    monkeypatch.setattr(settings, "kb_id_leg_rare_max_df", 2)
    on = [h.item_id for h in search(q, k=10)]
    assert target in on, "the rare-token identifier leg surfaces the symbol"


def test_rare_token_leg_leaves_a_common_token_query_unchanged(monkeypatch):
    """A ubiquitous acronym has too high a corpus df to be admitted, so the
    candidate set is identical with the flag on and off."""
    for i in range(6):
        ingest("p", "note", f"rxtx doc {i}", f"the RXTX bus is described here in document {i}")
    q = "how does the RXTX bus arbitration behave in practice"  # non-dominant, common id token

    monkeypatch.setattr(settings, "kb_id_leg_rare_tokens", False)
    off = [h.item_id for h in search(q, k=10)]
    monkeypatch.setattr(settings, "kb_id_leg_rare_tokens", True)
    monkeypatch.setattr(settings, "kb_id_leg_rare_max_df", 2)
    on = [h.item_id for h in search(q, k=10)]
    assert on == off, "a common id token admits nothing new — candidate set unchanged"


# --- (4) kb_collapse_alternatives ---------------------------------------------


def test_cross_project_alternative_twins_collapse_under_the_flag(monkeypatch):
    a = ingest("alpha", "note", "widget approach A", "the widgetsync approach A body text")
    b = ingest("beta", "note", "widget approach B", "the widgetsync approach B body text alt")
    graph.link(a, "alternative_to", b)  # cross-project edge
    q = "widgetsync approach"

    off_hits = search(q, k=10)
    off_ids = [h.item_id for h in off_hits]
    assert a in off_ids and b in off_ids, "without the flag both twins appear"
    scores = {h.item_id: h.score for h in off_hits}
    winner, loser = (a, b) if (scores[a], a) >= (scores[b], b) else (b, a)

    monkeypatch.setattr(settings, "kb_collapse_alternatives", True)
    on_ids = [h.item_id for h in search(q, k=10)]
    assert winner in on_ids, "the higher-scored twin is kept"
    assert loser not in on_ids, "the lower-scored twin is collapsed out of the rendered hits"


def test_collapse_needs_both_the_flag_and_a_cross_project_edge(monkeypatch):
    # An UNLINKED pair, and a SAME-project linked pair, both survive the flag.
    c = ingest("gamma", "note", "widget approach C", "the widgetsync approach C body")
    d = ingest("delta", "note", "widget approach D", "the widgetsync approach D body")
    e = ingest("omega", "note", "widget approach E", "the widgetsync approach E body")
    f = ingest("omega", "note", "widget approach F", "the widgetsync approach F body")
    graph.link(e, "alternative_to", f)  # SAME project → not a cross-project twin

    monkeypatch.setattr(settings, "kb_collapse_alternatives", True)
    ids = [h.item_id for h in search("widgetsync approach", k=20)]
    assert c in ids and d in ids, "an unlinked pair is never collapsed"
    assert e in ids and f in ids, "a same-project alternative_to pair is not collapsed"

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

from kbcore import consolidation, db, graph, search_rank
from kbcore.config import settings
from kbcore.ingest import ingest
from kbcore.search import BOOST_FACTOR, EXACT_ID_BONUS, _apply_bonuses, search
from kbcore.search_legs import CANDIDATES, RRF_K

pytestmark = pytest.mark.usefixtures("kb")


def _blind(item_id: int) -> int:
    """NULL the item's current-model embedding so the vector leg skips it."""
    with db.transaction() as conn:
        conn.execute("UPDATE chunks SET embedding = NULL WHERE item_id = %s", (item_id,))
    return item_id


def _twins(a: int, b: int) -> None:
    """Make the linked pair a LINKER twin (E1, KB 31855): the collapse fold now
    reads only pairs whose ord-0 chunks sit strictly above kb_xtwin_threshold on
    the current model, and the conftest fake embedder gives two different bodies
    ~orthogonal vectors — so copy a's ord-0 embedding onto b (cosine 1.0).
    Test-only chunks UPDATE in kb_test, like _blind."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE chunks SET embedding = (SELECT embedding FROM chunks "
            "WHERE item_id = %s AND ord = 0) WHERE item_id = %s AND ord = 0",
            (a, b),
        )


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
    src = (Path(__file__).resolve().parent.parent / "src/kbmcp/tools_core.py").read_text()
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
    it once enabled, without changing the id-dominant path.

    Off phase PINNED: .env carries KB_ID_LEG_RARE_TOKENS=true (validation week)."""
    monkeypatch.setattr(settings, "kb_id_leg_rare_tokens", False)
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


# --- (3b) the rare-only never-outrank guarantee holds WITH bonuses ------------
# Review #4 P1 (Opus): the rank-offset bounds the rare row's RRF contribution,
# but the exact-identifier bonus then composed ON TOP for the SAME token —
# lifting a rare-only candidate above real lexical hits. The fuse now reports
# which ids it INTRODUCED and _apply_bonuses exempts those from the admitting
# tokens' bonus.


def test_rare_only_candidate_never_outranks_weakest_lexical_hit_with_bonuses():
    weak = ingest("p", "note", "weak real hit", "ordinary lexical body with no symbol")
    lurker = ingest("p", "note", "rare lurker", "the ZRQX9 symbol body")
    weakest = 1.0 / (RRF_K + CANDIDATES)  # weakest possible single-leg lexical hit
    fused, introduced = search_rank.fuse_rare_id_rows(
        [(weak, weakest, "w")], [(lurker, "the ZRQX9 symbol body")]
    )
    assert introduced == {lurker}, "the fuse reports the id it introduced (provenance)"
    base = {i: s for i, s, _ in fused}
    # The finding's arithmetic, pinned non-vacuously: WITHOUT the exemption the
    # admitting token's bonus would lift the rare-only candidate past the hit.
    assert base[lurker] + EXACT_ID_BONUS > weakest

    with db.connect() as conn:
        out = _apply_bonuses(conn, fused, None, ["ZRQX9"], {}, introduced, ["ZRQX9"])

    scores = {i: s for i, s, _ in out}
    assert scores[lurker] == base[lurker], "no exact-id bonus from the admitting token"
    assert scores[lurker] < scores[weak], "rare-only stays below the weakest real hit"


def test_a_real_hit_nudged_by_the_rare_leg_keeps_its_exact_id_bonus():
    """Provenance is INTRODUCTION, not participation: a candidate the legs already
    found is merely nudged by the rare fold and keeps every bonus (and the
    id-DOMINANT path never reaches the fold at all, so it is untouched)."""
    real = ingest("p", "note", "real hit with symbol", "lexical body containing ZRQY7 too")
    fused, introduced = search_rank.fuse_rare_id_rows(
        [(real, 2.0 / (RRF_K + 1), "r")], [(real, "lexical body containing ZRQY7 too")]
    )
    assert introduced == set(), "an already-fused candidate is never 'introduced'"
    base = {i: s for i, s, _ in fused}[real]
    with db.connect() as conn:
        out = _apply_bonuses(conn, fused, None, ["ZRQY7"], {}, introduced, ["ZRQY7"])
    assert {i: s for i, s, _ in out}[real] == pytest.approx(base + EXACT_ID_BONUS)


def test_dominant_path_is_byte_identical_with_the_rare_dial_on(monkeypatch):
    """An id-DOMINANT query never runs the rare leg: ids AND scores must be
    byte-identical whether the dial is on or off."""
    target = ingest("p", "note", "dom fix", "Fix lives in /src/drv/zqdom_write.c line 4.")
    for i in range(3):
        ingest("p", "note", f"dom filler {i}", f"dominant filler body {i}")
    _blind(target)
    monkeypatch.setattr(settings, "kb_id_leg_rare_tokens", False)
    off = [(h.item_id, h.score) for h in search("zqdom_write", k=10, tool=None)]
    monkeypatch.setattr(settings, "kb_id_leg_rare_tokens", True)
    monkeypatch.setattr(settings, "kb_id_leg_rare_max_df", 2)
    on = [(h.item_id, h.score) for h in search("zqdom_write", k=10, tool=None)]
    assert on == off and target in [i for i, _ in on]


def test_rare_only_candidate_ranks_below_real_lexical_hits_end_to_end(monkeypatch):
    monkeypatch.setattr(settings, "kb_id_leg_rare_tokens", True)
    monkeypatch.setattr(settings, "kb_id_leg_rare_max_df", 2)
    real = ingest("p", "note", "zqfoo remediation", "zqfoo remediation steps body")
    lurk = ingest("p", "note", "lurker", "Fixed at /src/ZQRARE9/mod.c line 2.")
    _blind(lurk)  # rare-leg-only: vector-blind, and the path token hides it from tsquery

    hits = search("zqfoo remediation ZQRARE9 steps", k=10, tool=None)  # non-dominant

    ids = [h.item_id for h in hits]
    assert real in ids and lurk in ids, "both the lexical hit and the rare lurker surface"
    assert ids.index(real) < ids.index(lurk), "the rare-only candidate ranks below"


# --- (4) kb_collapse_alternatives ---------------------------------------------


def test_cross_project_alternative_twins_collapse_under_the_flag(monkeypatch):
    a = ingest("alpha", "note", "widget approach A", "the widgetsync approach A body text")
    b = ingest("beta", "note", "widget approach B", "the widgetsync approach B body text alt")
    graph.link(a, "alternative_to", b)  # cross-project edge
    _twins(a, b)
    q = "widgetsync approach"

    # PIN the off phase: the box's .env carries KB_COLLAPSE_ALTERNATIVES=true
    # since validation week (2026-08-14) — the A/B must not inherit it.
    monkeypatch.setattr(settings, "kb_collapse_alternatives", False)
    off_hits = search(q, k=10)
    off_ids = [h.item_id for h in off_hits]
    assert a in off_ids and b in off_ids, "without the flag both twins appear"
    scores = {h.item_id: h.score for h in off_hits}
    winner, loser = (a, b) if (scores[a], a) >= (scores[b], b) else (b, a)

    monkeypatch.setattr(settings, "kb_collapse_alternatives", True)
    on_ids = [h.item_id for h in search(q, k=10)]
    assert winner in on_ids, "the higher-scored twin is kept"
    assert loser not in on_ids, "the lower-scored twin is collapsed out of the rendered hits"


def test_collapse_prefers_the_verified_twin_in_a_near_tie():
    """Review #4 P2: winner selection was post-rerank score ALONE, so a verified
    item could be dropped for an unverified twin at +0.001. Inside
    COLLAPSE_TRUST_EPSILON the verified twin survives; the kept score is still
    the pair MAX (dedupe semantics untouched)."""
    v = ingest("alpha", "note", "verified twin", "twin body v", trust="verified")
    u = ingest("beta", "note", "unverified twin", "twin body u", trust="unverified")
    graph.link(v, "alternative_to", u)
    _twins(v, u)
    hi = 2.0 / (RRF_K + 1)
    near = hi - 0.001  # the review's observed gap — a near-tie

    with db.connect() as conn:
        pruned, folded = search_rank.collapse_alternatives(conn, [(u, hi, "u"), (v, near, "v")])

    assert folded == {v: [u]}, "the verified twin is the winner inside the epsilon"
    assert [(i, s) for i, s, _ in pruned] == [(v, hi)], "kept at the pair MAX, never boosted"


def test_collapse_verified_twin_loses_only_when_clearly_outscored():
    v = ingest("alpha", "note", "verified twin 2", "twin body v2", trust="verified")
    u = ingest("beta", "note", "unverified twin 2", "twin body u2", trust="unverified")
    graph.link(v, "alternative_to", u)
    _twins(v, u)
    lo = 2.0 / (RRF_K + 1)
    hi = lo + 2 * search_rank.COLLAPSE_TRUST_EPSILON  # beyond the near-tie window

    with db.connect() as conn:
        pruned, folded = search_rank.collapse_alternatives(conn, [(u, hi, "u"), (v, lo, "v")])

    assert folded == {u: [v]}, "clearly outscored: the rerank verdict stands, trust defers"
    assert [(i, s) for i, s, _ in pruned] == [(u, hi)]


def test_collapse_same_trust_near_tie_keeps_the_higher_score():
    """No trust difference -> the pre-existing score pick, byte-identical."""
    x = ingest("alpha", "note", "same trust x", "twin body x", trust="unverified")
    y = ingest("beta", "note", "same trust y", "twin body y", trust="unverified")
    graph.link(x, "alternative_to", y)
    _twins(x, y)
    hi = 2.0 / (RRF_K + 1)

    with db.connect() as conn:
        pruned, folded = search_rank.collapse_alternatives(
            conn, [(y, hi, "y"), (x, hi - 0.001, "x")]
        )

    assert folded == {y: [x]}, "same trust: the higher score wins exactly as before"
    assert [(i, s) for i, s, _ in pruned] == [(y, hi)]


def test_collapse_needs_both_the_flag_and_a_cross_project_edge(monkeypatch):
    # An UNLINKED pair, and a SAME-project linked pair, both survive the flag.
    c = ingest("gamma", "note", "widget approach C", "the widgetsync approach C body")
    d = ingest("delta", "note", "widget approach D", "the widgetsync approach D body")
    e = ingest("omega", "note", "widget approach E", "the widgetsync approach E body")
    f = ingest("omega", "note", "widget approach F", "the widgetsync approach F body")
    graph.link(e, "alternative_to", f)  # SAME project → not a cross-project twin
    _twins(e, f)  # even as true twins: the project wall still holds

    monkeypatch.setattr(settings, "kb_collapse_alternatives", True)
    ids = [h.item_id for h in search("widgetsync approach", k=20)]
    assert c in ids and d in ids, "an unlinked pair is never collapsed"
    assert e in ids and f in ids, "a same-project alternative_to pair is not collapsed"


# --- (3b) rare-only seal: the ACTUAL bound (review #5 ret-seal-1) -------------


def test_rare_only_seal_residual_crossings_pinned():
    """The seal comment (search_rank.RARE_ID_RANK_OFFSET) claims only this: a
    rare-only candidate stays below any lexical hit ABSENT independent
    identifier evidence and the near-tie dials. These pins prove the three
    residual crossings it names are REAL with the live constants — if any
    constant moves and a pin flips, re-derive the comment, don't delete the pin.
    (Do NOT 'fix' the crossings by clamping rare-only candidates below every
    non-rare candidate post-bonus — 'independent evidence stays rewarded' is the
    settled composition; review #5 guard.)"""
    rank1_rare = 1.0 / (RRF_K + search_rank.RARE_ID_RANK_OFFSET + 1)  # 1/101
    weakest_lexical = 1.0 / (RRF_K + CANDIDATES)  # 1/100

    # The base seal DOES hold: every dial inert, no independent evidence.
    assert rank1_rare < weakest_lexical
    # Crossing 1 — exact-id bonus from a NON-admitting token:
    # 1/101 + 1/122 = 0.01810 > 1/61 (a rank-1 single-leg lexical hit).
    assert rank1_rare + EXACT_ID_BONUS > 1.0 / (RRF_K + 1)
    # Crossing 2 — the trust dial at its documented near-tie size: 1/101 * 1.02 > 1/100.
    assert rank1_rare * 1.02 > weakest_lexical
    # Crossing 3 — the project boost: 1.25/101 > 1/81 (a rank-21 lexical hit).
    assert BOOST_FACTOR * rank1_rare > 1.0 / (RRF_K + 21)

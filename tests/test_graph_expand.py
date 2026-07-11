"""WP2 — GRAPH-TRAVERSAL retrieval expansion in search.

Two layers: (1) graph.expand_items — the item↔item BFS primitive (min-hop,
item-only, cycle-safe, seed-excluding); (2) search(hops=...) — surfacing a
graph neighbor the keyword/vector legs missed, with the SAME scope/status/
valid-time safety the legs enforce. Real DB in kb_test; Ollama mocked by
conftest.

Deterministic hits: only the SEED item carries the query keyword. Neighbors are
made invisible to both legs by (a) a body that lacks the keyword and (b) a
NULL'd current-model embedding — so they can only reach the result set through
graph expansion. That isolates the expansion path from the fake vector leg,
which otherwise returns every embedded item (no distance threshold).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kbcore import db, graph, search_expand
from kbcore.config import settings
from kbcore.ingest import ingest
from kbcore.search import _filters, search

pytestmark = pytest.mark.usefixtures("kb")

T = datetime(2026, 6, 1, tzinfo=UTC)
JAN = datetime(2026, 1, 1, tzinfo=UTC)
SEP = datetime(2026, 9, 1, tzinfo=UTC)


# --- helpers -----------------------------------------------------------------


def _blind(item_id: int) -> int:
    """NULL the item's current-model embedding so the vector leg skips it.

    Combined with a keyword-free body this makes the item unreachable by either
    search leg — it can only surface via graph expansion.
    """
    with db.transaction() as conn:
        conn.execute("UPDATE chunks SET embedding = NULL WHERE item_id = %s", (item_id,))
    return item_id


def _make_public(*projects: str) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE projects SET public = TRUE WHERE name = ANY(%s)", (list(projects),))


# --- graph.expand_items primitive --------------------------------------------


def test_expand_one_hop_both_directions():
    a = ingest("p", "note", "A", "body a")
    b = ingest("p", "note", "B", "body b")
    c = ingest("p", "note", "C", "body c")
    graph.link(a, "depends_on", b)  # a -> b  (out-edge of a)
    graph.link(c, "fixes", a)  # c -> a  (in-edge of a)
    with db.connect() as conn:
        reached = graph.expand_items(conn, [a], 1)
    assert reached == {b: 1, c: 1}, "both out- and in-neighbors at hop 1; seed excluded"


def test_expand_min_hop_distance_over_a_chain():
    a = ingest("p", "note", "A", "body a")
    b = ingest("p", "note", "B", "body b")
    c = ingest("p", "note", "C", "body c")
    graph.link(a, "depends_on", b)
    graph.link(b, "depends_on", c)
    with db.connect() as conn:
        assert graph.expand_items(conn, [a], 1) == {b: 1}
        assert graph.expand_items(conn, [a], 2) == {b: 1, c: 2}


def test_expand_ignores_entity_edges():
    # The tag derives an entity + a 'mentions' edge; expansion must skip it and
    # return only the item neighbor (entity/item ids share no namespace).
    a = ingest("p", "note", "A", "body a", tags=["protocol:spi"])
    b = ingest("p", "note", "B", "body b")
    graph.link(a, "depends_on", b)
    with db.connect() as conn:
        reached = graph.expand_items(conn, [a], 2)
    assert reached == {b: 1}, "entity 'mentions' neighbor is ignored (item edges only)"


def test_expand_is_cycle_safe():
    a = ingest("p", "note", "A", "body a")
    b = ingest("p", "note", "B", "body b")
    graph.link(a, "depends_on", b)
    graph.link(b, "depends_on", a)  # cycle a<->b
    with db.connect() as conn:
        reached = graph.expand_items(conn, [a], 5)
    assert reached == {b: 1}, "cycle terminates; seed a never re-enters"


def test_expand_empty_inputs():
    a = ingest("p", "note", "A", "body a")
    with db.connect() as conn:
        assert graph.expand_items(conn, [], 1) == {}
        assert graph.expand_items(conn, [a], 0) == {}


# --- search(hops=...) integration --------------------------------------------


def _bug_and_fix() -> tuple[int, int]:
    """A gearbox bug (keyword-matches 'gearbox') fixed by a note that does NOT."""
    bug = ingest("p", "bug", "Gearbox stalls", "The gearbox stalls under load at 3000 rpm.")
    fix = ingest("p", "note", "Coupling fix", "Replace the coupling and re-torque the flange.")
    graph.link(fix, "fixes", bug)  # fix -fixes-> bug  (bug is an in-neighbor's dst)
    _blind(fix)  # fix reaches the results only through the graph
    return bug, fix


def test_hops0_returns_only_the_direct_hit():
    bug, fix = _bug_and_fix()
    ids = [h.item_id for h in search("gearbox", hops=0)]
    assert bug in ids
    assert fix not in ids, "no expansion at hops=0 — zero-regression path"


def test_hops1_surfaces_the_linked_fix_below_the_bug():
    bug, fix = _bug_and_fix()
    hits = search("gearbox", hops=1)
    ids = [h.item_id for h in hits]
    assert bug in ids and fix in ids, "the graph neighbor is pulled in at hops=1"
    assert ids.index(bug) < ids.index(fix), "the decayed neighbor ranks below the direct hit"
    # the neighbor carries a body-derived snippet (no matching chunk of its own)
    fix_hit = next(h for h in hits if h.item_id == fix)
    assert "coupling" in fix_hit.snippet.lower()
    bug_hit = next(h for h in hits if h.item_id == bug)
    assert fix_hit.score < bug_hit.score, "hop decay puts the neighbor strictly below"


def test_superseded_neighbor_is_not_surfaced():
    from kbcore.supersede import supersede

    bug, fix = _bug_and_fix()
    supersede(fix, new_body="Superseded coupling procedure.", reason="revised")  # fix -> superseded
    ids = [h.item_id for h in search("gearbox", hops=1)]
    assert fix not in ids, "a superseded neighbor fails the active-status scope check"


def test_neighbor_in_a_different_project_is_scoped_out():
    bug = ingest("pa", "bug", "Gearbox stalls", "The gearbox stalls under load.")
    fix = ingest("pb", "note", "Coupling fix", "Replace the coupling.")
    graph.link(fix, "fixes", bug)
    _blind(fix)
    # Unrestricted: the cross-project neighbor surfaces.
    assert fix in [h.item_id for h in search("gearbox", hops=1)]
    # Restricted to the bug's project: the neighbor (project pb) is scoped out.
    scoped = [h.item_id for h in search("gearbox", project="pa", hops=1)]
    assert bug in scoped and fix not in scoped


def test_non_public_neighbor_withheld_under_public_only(monkeypatch):
    bug = ingest("pub", "bug", "Gearbox stalls", "The gearbox stalls under load.")
    fix = ingest("priv", "note", "Coupling fix", "Replace the coupling.")
    graph.link(fix, "fixes", bug)
    _blind(fix)
    _make_public("pub")  # bug's project public; fix's project stays private
    monkeypatch.setattr(settings, "kb_public_only", True)
    ids = [h.item_id for h in search("gearbox", hops=1)]
    assert bug in ids, "the public seed is found"
    assert fix not in ids, "a non-public neighbor is withheld by the public-only scope check"


# --- live regression: a weak neighbor already present in fused ----------------


def test_weak_neighbor_in_the_seed_window_is_still_boosted():
    """A genuine neighbor of the top hit that is ALSO weakly present in fused.

    The blinded-neighbor tests above never exercised this: with real embeddings
    every item gets a small vector score, so a true neighbor of the top hit
    lands in the weak tail — inside the old flat top-N seed window. Because
    expand_items excludes seeds, the old code left that neighbor stuck at its
    weak rank (this was the live symptom: hops=1 identical to hops=0). Seeds are
    now the strong hits only, so the neighbor stays boost-eligible and receives
    the ``best * DECAY`` floor. Driven through expand_fused directly with a
    synthetic RRF list so it does not depend on the fake vector leg's ordering.
    """
    bug = ingest("p", "bug", "Gearbox stalls", "The gearbox stalls under load.")
    fix = ingest("p", "note", "Coupling fix", "Replace the coupling.")
    filler = ingest("p", "note", "Filler", "an unrelated weak tail hit")
    graph.link(fix, "fixes", bug)  # fix is a true 1-hop neighbor of the bug
    where, params = _filters(None, None, None, None, ["active"], None)
    # Mimic real embeddings: bug strong; filler and the linked fix both in the
    # weak tail (fix even weaker than the unrelated filler), both inside the
    # top-10 the old code seeded from.
    best = 0.0328
    fused = [(bug, best, "gearbox"), (filler, 0.0161, "unrelated"), (fix, 0.0122, "coupling")]
    with db.connect() as conn:
        out, origin = search_expand.expand_fused(conn, fused, 1, where, params, 10)
    scores = {i: s for i, s, _ in out}
    order = [i for i, _, _ in out]
    # The fix is lifted to exactly the one-hop decay floor (never its weak raw).
    assert scores[fix] == pytest.approx(best * search_expand.GRAPH_DECAY)
    assert scores[fix] > scores[filler], "boosted above the unrelated tail hit"
    assert order.index(bug) < order.index(fix) < order.index(filler)
    # The unrelated filler (not a neighbor of any strong seed) is left untouched.
    assert scores[filler] == pytest.approx(0.0161)
    # `fix` is a DIRECT hit here (present in fused), just boosted by expansion — so
    # it is not "expansion-only" and carries no origin entry.
    assert fix not in origin


def test_weak_tail_hit_does_not_expand_its_own_neighborhood():
    """A barely-relevant tail hit must not drag in its neighbors as noise.

    Only hits at/above best*DECAY seed expansion. An item sitting in the weak
    tail is below that floor, so its private neighborhood stays out of results.
    """
    strong = ingest("p", "note", "Strong", "gearbox topic")
    tail = ingest("p", "note", "Tail", "weak tail item")
    tail_nbr = ingest("p", "note", "TailNeighbor", "only reachable via the tail")
    graph.link(tail, "depends_on", tail_nbr)  # neighbor of the WEAK tail hit
    where, params = _filters(None, None, None, None, ["active"], None)
    best = 0.0328
    fused = [(strong, best, "gearbox"), (tail, 0.0100, "weak")]  # tail << best*DECAY
    with db.connect() as conn:
        out, _origin = search_expand.expand_fused(conn, fused, 1, where, params, 10)
    assert tail_nbr not in {i for i, _, _ in out}, "weak tail hit does not seed expansion"


# --- effective-hops resolution: param precedence + cap -----------------------


def _chain(n: int) -> list[int]:
    """A depends_on chain h0 -> h1 -> ... -> h(n-1); only h0 matches 'sprocket'."""
    ids = [ingest("p", "note", "H0", "The sprocket registry root note.")]
    for i in range(1, n):
        nid = ingest("p", "note", f"H{i}", f"chain link number {i} with no query word")
        graph.link(ids[-1], "depends_on", nid)
        _blind(nid)
        ids.append(nid)
    return ids


def test_effective_hops_is_capped(monkeypatch):
    # A config default above the cap is clamped to GRAPH_MAX_HOPS (3): the 4-hop
    # node is unreachable even though the config asks for 10. hops=None (unset)
    # falls through to the config default (explicit 0 would force expansion OFF).
    ids = _chain(5)  # h0..h4 ; h4 is 4 hops from h0
    monkeypatch.setattr(settings, "kb_graph_expand_hops", 10)
    got = {h.item_id for h in search("sprocket", hops=None)}  # None -> config 10 -> capped 3
    assert ids[3] in got, "3-hop node reached (cap = 3)"
    assert ids[4] not in got, "4-hop node beyond the cap is not reached"


def test_hops_param_takes_precedence_over_settings(monkeypatch):
    ids = _chain(3)  # h0 -> h1 -> h2
    monkeypatch.setattr(settings, "kb_graph_expand_hops", 5)  # would reach h2
    got = {h.item_id for h in search("sprocket", hops=1)}  # param 1 wins over config 5
    assert ids[1] in got, "1-hop node reached"
    assert ids[2] not in got, "2-hop node not reached — the param value (1) overrode config (5)"


# --- per-seed scoring: neighbor scores off ITS seed, not the global best ------


def test_neighbor_scores_off_its_own_seed_not_the_global_best():
    """The regression fix: a neighbor of a weaker (but qualifying) seed gets that
    seed's score*DECAY, not the top hit's score*DECAY (which used to let a
    zero-relevance neighbor tie/outrank genuine direct hits)."""
    top = ingest("p", "note", "Top", "gearbox topic root")
    weak_seed = ingest("p", "note", "WeakSeed", "gearbox weak but qualifying")
    nbr = ingest("p", "note", "Nbr", "reachable only via the weak seed")
    graph.link(weak_seed, "depends_on", nbr)
    where, params = _filters(None, None, None, None, ["active"], None)
    best = 0.0328
    # weak_seed sits exactly at the seed floor (best*DECAY), so it qualifies as a
    # seed; its neighbor must decay from IT, not from the global best.
    fused = [(top, best, "top"), (weak_seed, best * search_expand.GRAPH_DECAY, "weak")]
    with db.connect() as conn:
        out, origin = search_expand.expand_fused(conn, fused, 1, where, params, 10)
    scores = {i: s for i, s, _ in out}
    d = search_expand.GRAPH_DECAY
    assert scores[nbr] == pytest.approx(best * d * d), "neighbor decays from the weak seed"
    assert scores[nbr] < scores[weak_seed], "neighbor ranks strictly below its vouching seed"
    assert origin[nbr] == weak_seed, "vouching seed is the weak seed, not the global best"


def test_unbonused_rank1_hit_can_still_seed_under_a_project_boost():
    """seed_floor is computed on RAW scores now, so a boost on the top hit no
    longer inflates the floor above the max raw RRF and starves un-boosted seeds:
    the linked fix of an un-boosted rank-1 hit is still pulled in."""
    bug = ingest("motor-ctl", "bug", "Gearbox stalls", "the gearbox stalls under load")
    fix = ingest("motor-ctl", "note", "Coupling fix", "replace the coupling")
    graph.link(fix, "fixes", bug)
    _blind(fix)
    # boost a DIFFERENT project; the bug is rank-1 raw but carries no bonus.
    ids = [h.item_id for h in search("gearbox", boost_project="power-board", hops=1)]
    assert bug in ids and fix in ids, "un-boosted rank-1 hit still seeds its neighborhood"


# --- scope-walled BFS: a withheld node is neither surfaced NOR a relay hop -----


def test_bfs_does_not_relay_through_a_withheld_node(monkeypatch):
    a = ingest("pub", "bug", "Gearbox stalls", "the gearbox stalls under load")
    x = ingest("priv", "note", "Private relay", "internal relay note")
    b = ingest("pub", "note", "Public advisory", "downstream public advisory")
    graph.link(a, "depends_on", x)  # a -> x  (x is private)
    graph.link(x, "depends_on", b)  # x -> b  (b public, reachable ONLY via x)
    _blind(x)
    _blind(b)
    _make_public("pub")  # priv stays private
    monkeypatch.setattr(settings, "kb_public_only", True)
    ids = [h.item_id for h in search("gearbox", hops=3)]
    assert a in ids, "the public seed is found"
    assert x not in ids, "the private relay is withheld"
    assert b not in ids, "a public node reachable only THROUGH the private relay is not surfaced"


# --- as_of + expand together: the neighbor re-checks the SAME where ------------


def test_as_of_and_expand_exclude_a_neighbor_outside_the_interval():
    bug = ingest("p", "bug", "Gearbox stalls", "the gearbox stalls under load", valid_from=JAN)
    fix = ingest("p", "note", "Coupling fix", "replace the coupling", valid_from=SEP)  # future vs T
    graph.link(fix, "fixes", bug)
    _blind(fix)
    # No as_of: the neighbor is pulled in.
    assert fix in [h.item_id for h in search("gearbox", hops=1)]
    # as_of=T is before the fix's valid_from — the SAME where wall drops the neighbor.
    got = {h.item_id for h in search("gearbox", hops=1, as_of=T)}
    assert bug in got and fix not in got, "an expanded neighbor outside as_of is excluded"


# --- BFS breadth cap + expand-flag mapping ------------------------------------


def test_expand_scored_respects_the_breadth_cap():
    seed = ingest("p", "note", "Hub", "hub node")
    kids = [ingest("p", "note", f"K{i}", f"kid {i}") for i in range(5)]
    for kid in kids:
        graph.link(seed, "part_of", kid)
    with db.connect() as conn:
        reached = graph.expand_items_scored(
            conn, {seed: 1.0}, 1, 0.5, lambda ids: set(ids), max_visited=3
        )
    assert len(reached) == 3, "BFS stops once the breadth cap is hit"


def test_hops_from_expand_mapping(monkeypatch):
    monkeypatch.setattr(settings, "kb_graph_expand_hops", 2)
    assert search_expand.hops_from_expand(None) is None  # use the server default
    assert search_expand.hops_from_expand(False) == 0  # force OFF
    assert search_expand.hops_from_expand(True) == 2  # >= config default
    monkeypatch.setattr(settings, "kb_graph_expand_hops", 0)
    assert search_expand.hops_from_expand(True) == 1  # never fewer than one hop

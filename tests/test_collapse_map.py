"""Wave C (review #3 P2, KB 19663): the rank-time collapse map is KEPT.

Two halves, both additive and inert while ``kb_collapse_alternatives`` is off:
* ``SearchHit.collapsed`` — the ``{winner: [losers]}`` map is stamped on the KEPT
  hit instead of discarded; REST/MCP payloads inherit the field (it defaults to
  ``[]``, so pre-collapse payloads still validate);
* ACCESS PARITY — the folded LOSER is access-logged at the same chokepoint the
  winner's hit uses, so being collapsed never counts as being forgotten (the
  quiet-expiry/archive paths key on 'never recalled'). Loser rows carry the
  ``search_rank.COLLAPSE_PARITY_SUFFIX`` tool marker (review #4 P2) so counting
  surfaces (the promote queue) can exclude folds while every EXISTS-style
  never-recalled consumer still sees the row.

Real DB in kb_test; Ollama mocked by conftest; the REST leg runs in-process via
Starlette's TestClient with kb-mcp's bearer, like tests/test_phase6_rest.py.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from kbcore import db, graph
from kbcore.config import settings
from kbcore.ingest import ingest
from kbcore.models import SearchHit
from kbcore.search import search
from kbcore.search_rank import COLLAPSE_PARITY_SUFFIX

pytestmark = pytest.mark.usefixtures("kb")

TEST_TOKEN = "kbtest-" + "0" * 32
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}

Q = "widgetfold approach"


@pytest.fixture()
def client(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbrest.server import build_app

    return TestClient(build_app())


def _twin_embed(a: int, b: int, *, model: str | None = None) -> None:
    """Make ``b`` a LINKER twin of ``a``: copy a's ord-0 embedding onto b's ord-0
    chunk (cosine 1.0 > kb_xtwin_threshold). The conftest fake embedder is
    content-hash seeded, so two different bodies are ~orthogonal and would NOT
    pass the E1 twin predicate (KB 31855). A direct chunks UPDATE in kb_test is a
    test-only shortcut; production code never edits embeddings this way.
    ``model`` overrides b's ``embed_model`` stamp (the model-swap lag case)."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE chunks SET embedding = (SELECT embedding FROM chunks "
            "WHERE item_id = %s AND ord = 0), embed_model = COALESCE(%s, embed_model) "
            "WHERE item_id = %s AND ord = 0",
            (a, model, b),
        )


def _linked_pair(twins: bool = True) -> tuple[int, int]:
    """Cross-project 'alternative_to' pair that both match the Q query; a true
    linker twin pair by default, a DISSIMILAR (ADR-alternatives, 297/294-class)
    pair when ``twins`` is False."""
    a = ingest("alpha", "note", "widget approach A", "the widgetfold approach A body text")
    b = ingest("beta", "note", "widget approach B", "the widgetfold approach B body text alt")
    assert a is not None and b is not None
    graph.link(a, "alternative_to", b)
    if twins:
        _twin_embed(a, b)
    return a, b


def _linked_twins() -> tuple[int, int]:
    return _linked_pair(twins=True)


def _winner_loser(a: int, b: int) -> tuple[int, int]:
    """Resolve which twin the collapse keeps, from the flag-OFF scores."""
    scores = {h.item_id: h.score for h in search(Q, k=10, tool=None)}
    return (a, b) if (scores[a], a) >= (scores[b], b) else (b, a)


def _access_rows(item_id: int) -> list[tuple[str, str]]:
    with db.connect() as conn:
        return conn.execute(
            "SELECT tool, query FROM access_log WHERE item_id = %s", (item_id,)
        ).fetchall()


# --- the map is stamped on the kept hit --------------------------------------


def test_the_winner_carries_the_collapse_map(monkeypatch):
    a, b = _linked_twins()
    winner, loser = _winner_loser(a, b)

    off = search(Q, k=10, tool=None)
    assert all(h.collapsed == [] for h in off), "flag off: the field exists and stays empty"

    monkeypatch.setattr(settings, "kb_collapse_alternatives", True)
    hits = search(Q, k=10, tool=None)
    ids = [h.item_id for h in hits]
    assert winner in ids and loser not in ids
    kept = next(h for h in hits if h.item_id == winner)
    assert kept.collapsed == [loser], "the {winner: [losers]} map rides the kept hit"
    assert all(h.collapsed == [] for h in hits if h.item_id != winner)


def test_collapsed_round_trips_rest(client, monkeypatch):
    a, b = _linked_twins()
    winner, loser = _winner_loser(a, b)
    monkeypatch.setattr(settings, "kb_collapse_alternatives", True)

    r = client.get("/api/search", params={"q": Q}, headers=AUTH)

    assert r.status_code == 200
    hits = r.json()
    ids = [h["item_id"] for h in hits]
    assert winner in ids and loser not in ids
    kept = next(h for h in hits if h["item_id"] == winner)
    assert kept["collapsed"] == [loser], "the REST schema inherited the additive field"


def test_collapsed_round_trips_the_shared_model_schema(monkeypatch):
    """MCP validates against the SAME pydantic shape (kbcore.models.SearchHit), so
    the round-trip contract is pinned on the model: dump -> validate preserves the
    map, an OLD payload without the key still validates (default []), and the
    field is present in the published JSON schema."""
    a, b = _linked_twins()
    winner, _loser = _winner_loser(a, b)
    monkeypatch.setattr(settings, "kb_collapse_alternatives", True)
    kept = next(h for h in search(Q, k=10, tool=None) if h.item_id == winner)

    assert SearchHit.model_validate(kept.model_dump()) == kept
    legacy = kept.model_dump()
    legacy.pop("collapsed")
    assert SearchHit.model_validate(legacy).collapsed == [], "pre-collapse payloads validate"
    assert "collapsed" in SearchHit.model_json_schema()["properties"]


# --- access parity: folded is recalled, not forgotten -------------------------


def test_the_folded_loser_is_access_logged_as_recalled(monkeypatch):
    a, b = _linked_twins()
    winner, loser = _winner_loser(a, b)
    monkeypatch.setattr(settings, "kb_collapse_alternatives", True)

    hits = search(Q, k=10)  # default tool='search' -> the access-log chokepoint

    assert loser not in [h.item_id for h in hits], "suppressed from the rendered set"
    assert _access_rows(winner) == [("search", Q)]
    assert _access_rows(loser) == [("search" + COLLAPSE_PARITY_SUFFIX, Q)], (
        "the loser was RECALLED (folded into the winner) — same chokepoint, same query,"
        " marked as a fold so counting surfaces can tell it from a rendered hit"
    )


def test_no_collapse_means_no_extra_access_rows(monkeypatch):
    """The parity rows appear ONLY when a collapse fires: with no edge the flag
    changes nothing, and with the flag off both twins log as ordinary hits."""
    c = ingest("gamma", "note", "widget approach C", "the widgetfold approach C body")
    d = ingest("delta", "note", "widget approach D", "the widgetfold approach D body")
    monkeypatch.setattr(settings, "kb_collapse_alternatives", True)

    hits = search(Q, k=10)

    assert {c, d} <= {h.item_id for h in hits}, "an unlinked pair is never collapsed"
    assert _access_rows(c) == [("search", Q)] and _access_rows(d) == [("search", Q)], (
        "exactly one row each — no phantom parity rows without a collapse"
    )


def test_the_eval_replay_still_logs_nothing(monkeypatch):
    """tool=None is the eval-harness contract (no 'recently used' stamping) and
    it must cover the folded loser too."""
    a, b = _linked_twins()
    monkeypatch.setattr(settings, "kb_collapse_alternatives", True)

    search(Q, k=10, tool=None)

    assert _access_rows(a) == [] and _access_rows(b) == []


# --- E1 (KB 31855): only LINKER twins fold; ADR alternatives render both ------


def test_a_dissimilar_linked_pair_is_not_folded(monkeypatch):
    """The 297/294 class: a correct manual alternative_to edge between two ADR
    alternatives (ord-0 cosine far below kb_xtwin_threshold) is NOT a twin — both
    hits survive, no collapse map, no parity access rows."""
    a, b = _linked_pair(twins=False)
    monkeypatch.setattr(settings, "kb_collapse_alternatives", True)

    hits = search(Q, k=10)

    assert {a, b} <= {h.item_id for h in hits}, "both ADR alternatives render"
    assert all(h.collapsed == [] for h in hits), "no fold, no map"
    assert _access_rows(a) == [("search", Q)] and _access_rows(b) == [("search", Q)], (
        "exactly one ordinary row each — an unfolded pair logs like any two hits"
    )


@pytest.mark.parametrize("lag", ["null", "other-model"])
def test_a_pair_whose_lead_embedding_lags_is_not_folded(monkeypatch, lag):
    """An identical pair whose loser's ord-0 chunk is NULL or stamped with
    another embed_model is unprovable under the current model and renders both
    (embeddings lagging a model swap must never hide an item)."""
    a, b = _linked_twins()
    if lag == "null":
        with db.transaction() as conn:
            conn.execute("UPDATE chunks SET embedding = NULL WHERE item_id = %s", (b,))
    else:
        _twin_embed(a, b, model="stale")  # the same lead vector, stamped with another model
    monkeypatch.setattr(settings, "kb_collapse_alternatives", True)

    hits = search(Q, k=10)

    assert {a, b} <= {h.item_id for h in hits}
    assert all(h.collapsed == [] for h in hits)
    assert _access_rows(a) == [("search", Q)] and _access_rows(b) == [("search", Q)]


def test_the_twin_threshold_is_strict(monkeypatch):
    """Mirrors twin_probe: cosine EQUAL to kb_xtwin_threshold is not a twin. With
    identical lead vectors (cosine 1.0) a threshold of 1.0 refuses the fold and
    anything below it accepts — the same single knob the linker reads."""
    a, b = _linked_twins()
    monkeypatch.setattr(settings, "kb_collapse_alternatives", True)

    monkeypatch.setattr(settings, "kb_xtwin_threshold", 1.0)
    assert {a, b} <= {h.item_id for h in search(Q, k=10, tool=None)}, "1.0 > 1.0 is false"

    monkeypatch.setattr(settings, "kb_xtwin_threshold", 0.93)
    assert len({a, b} & {h.item_id for h in search(Q, k=10, tool=None)}) == 1, "folded"

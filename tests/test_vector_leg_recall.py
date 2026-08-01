"""Vector-leg recall: the HNSW candidate window must survive the post-scan wall.

THE DEFECT. pgvector's default ``hnsw.ef_search`` is 40 — the same number as the
vector leg's ``LIMIT CANDIDATES`` — and the status/scope wall lives on ``items``,
so it is applied AFTER the index scan. Non-active near-duplicates (which
supersession mints permanently, by design, Inv. 5) therefore crowd out the
candidate window and the leg returns SHORT: live probes saw as few as 24 of 40
candidates and the golden eval's hit@5 dipped 0.91 -> 0.88.

The tripwire below seeds exactly that shape — far more than CANDIDATES nearest
neighbours are non-active — and asserts the leg still returns a FULL window of
ACTIVE rows. It first proves the corpus is genuinely adversarial by running the
same leg with the tuning disabled and watching it starve, so the tripwire can
never pass for the wrong reason.

Real DB in kb_test via the `kb` fixture; Ollama mocked (conftest + an explicit
query-vector stub, because these tests need CONTROLLED geometry, not the
near-orthogonal hashes conftest hands out).
"""

from __future__ import annotations

import json
import math

import psycopg
import pytest

from kbcore import db, embed, search_legs
from kbcore.config import settings
from kbcore.search import _filters
from kbcore.search_legs import CANDIDATES, VECTOR_EF_SEARCH_FALLBACK, _vector_leg

pytestmark = pytest.mark.usefixtures("kb")

# Enough non-active near neighbours to bury the window several times over: with
# a fixed 40-wide window every single candidate is a decoy, so a regressed leg
# returns ~nothing rather than "a few less". Deliberately deeper than
# VECTOR_EF_SEARCH_FALLBACK too — only a scan that KEEPS GOING gets past this.
DECOYS = 200
ACTIVES = 60
# The shallow corpus sits BETWEEN the two fixed widths (40 < 50 < 120), which is
# the only shape where the legacy ef_search fallback is supposed to help.
SHALLOW_DECOYS = 50
SHALLOW_ACTIVES = 80
QUERY = "vector leg candidate window"


def _unit(axis: int, weight: float) -> str:
    """Unit vector at e0 + weight*e_axis, as the JSON text pgvector parses.

    cos(v, e0) = 1/sqrt(1 + weight^2): a SMALL weight is a near neighbour of the
    query vector e0, a large one is far. That gives a clean, deterministic
    distance ordering with no reliance on hash geometry.
    """
    v = [0.0] * settings.kb_embed_dim
    v[0] = 1.0
    v[axis % settings.kb_embed_dim] += weight
    norm = math.sqrt(sum(x * x for x in v))
    return json.dumps([x / norm for x in v], separators=(",", ":"))


def _query_vector() -> list[float]:
    v = [0.0] * settings.kb_embed_dim
    v[0] = 1.0
    return v


def _seed(decoys: int, actives: int) -> None:
    """Seed `decoys` NON-active chunks nearer the query than `actives` active ones.

    Superseded chunks land at ~0.0002 cosine distance from the query vector,
    active ones at ~0.29 — so every decoy sorts ahead of every active row and an
    HNSW window narrower than `decoys` contains nothing that survives the wall.
    """
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO projects (name, domain) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            ("recall-probe", "embedded"),
        )
        for n, status, weight in ((decoys, "superseded", 0.02), (actives, "active", 1.0)):
            for i in range(n):
                title = f"{status} neighbour {i}"
                item_id = conn.execute(
                    "INSERT INTO items (project, type, title, body, status) "
                    "VALUES (%s, 'note', %s, %s, %s) RETURNING id",
                    ("recall-probe", title, f"body of {title}", status),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO chunks (item_id, ord, text, text_hash, embedding, embed_model) "
                    "VALUES (%s, 0, %s, %s, %s::vector, %s)",
                    (
                        item_id,
                        f"chunk for {title}",
                        f"hash-{status}-{i}",
                        _unit(i + 1, weight),
                        settings.kb_embed_model,
                    ),
                )


@pytest.fixture()
def _stub_query_vector(monkeypatch) -> None:
    """Pin the query embedding to e0 — these tests need CONTROLLED geometry."""
    monkeypatch.setattr(embed, "embed_query", lambda text: _query_vector())


@pytest.fixture()
def buried_window(_stub_query_vector) -> None:
    """Non-active neighbours deeper than ANY fixed window: only an iterative
    scan can reach the active rows behind them."""
    _seed(DECOYS, ACTIVES)


@pytest.fixture()
def shallow_window(_stub_query_vector) -> None:
    """Non-active neighbours deeper than the DEFAULT window but shallower than
    the fallback one — the shape the legacy ef_search path has to handle."""
    _seed(SHALLOW_DECOYS, SHALLOW_ACTIVES)


def _active_wall() -> tuple[str, list]:
    return _filters(None, None, None, None, ["active"], None)


def _leg_conn() -> psycopg.Connection:
    """Connection pinned to the HNSW plan the production corpus actually gets.

    At a few hundred rows the planner prefers a full scan + quicksort, which
    applies the wall to EVERY row and so hides the bug entirely. Disabling the
    sort node leaves the ordered HNSW index scan as the only way to satisfy the
    ORDER BY — i.e. the plan a real corpus takes on its own. Verified by
    EXPLAIN ANALYZE on the buried corpus:
    without the fix that plan is
    ``Limit <- Nested Loop <- Index Scan using chunks_vec_idx (rows=40)``
    joined to ``items ... Filter: status = ANY('{active}')`` yielding **rows=0**.
    """
    conn = db.connect()
    conn.execute("SET LOCAL enable_sort = off")
    return conn


def _statuses(item_ids: list[int]) -> set[str]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT status FROM items WHERE id = ANY(%s)", (item_ids,)
        ).fetchall()
    return {r[0] for r in rows}


@pytest.mark.parametrize("corpus", ["buried_window", "shallow_window"])
def test_untuned_vector_leg_starves_on_a_buried_window(corpus, request, monkeypatch):
    """Corpus sanity: WITHOUT the widening both corpora really do return short.

    This is the pre-fix behaviour, asserted so the tripwires that follow cannot
    quietly degrade into tests that would pass on the broken code too.
    """
    request.getfixturevalue(corpus)
    monkeypatch.setattr(search_legs, "_tune_vector_scan", lambda conn: "disabled-for-test")
    conn = _leg_conn()
    try:
        rows = _vector_leg(conn, QUERY, *_active_wall())
    finally:
        conn.close()
    assert len(rows) < CANDIDATES, (
        f"corpus {corpus} is not adversarial: an untuned window returned "
        f"{len(rows)} of {CANDIDATES} rows, so the tripwires below prove nothing"
    )


def test_vector_leg_returns_full_active_window_behind_buried_neighbours(buried_window):
    """TRIPWIRE: >CANDIDATES nearest neighbours are non-active, yet the leg still
    returns a FULL candidate window and every row is active."""
    conn = _leg_conn()
    try:
        rows = _vector_leg(conn, QUERY, *_active_wall())
    finally:
        conn.close()
    assert len(rows) == CANDIDATES, (
        f"vector leg returned {len(rows)} of {CANDIDATES} candidates — the "
        "post-scan status wall is starving the HNSW window again"
    )
    item_ids = [item_id for item_id, _text in rows]
    assert len(set(item_ids)) == CANDIDATES, "candidate window contains duplicates"
    assert _statuses(item_ids) == {"active"}, "non-active rows leaked past the wall"


def test_vector_leg_sets_strict_order_iterative_scan(buried_window):
    """RRF fusion ranks by ROW POSITION, so the widening must preserve distance
    order: strict_order, never relaxed_order."""
    conn = _leg_conn()
    try:
        _vector_leg(conn, QUERY, *_active_wall())
        applied = conn.execute("SELECT current_setting('hnsw.iterative_scan')").fetchone()[0]
    finally:
        conn.close()
    assert applied == "strict_order", (
        f"expected strict_order (RRF depends on distance order), got {applied!r}"
    )


def test_vector_leg_tuning_is_transaction_local(buried_window):
    """The widening must not outlive the leg's transaction (no leak onto a
    reused connection)."""
    conn = db.connect()
    try:
        _vector_leg(conn, QUERY, *_active_wall())
        assert (
            conn.execute("SELECT current_setting('hnsw.iterative_scan')").fetchone()[0]
            == "strict_order"
        )
        conn.commit()
        after = conn.execute("SELECT current_setting('hnsw.iterative_scan')").fetchone()[0]
    finally:
        conn.close()
    assert after == "off", f"tuning leaked past the transaction as {after!r}"


def test_vector_leg_falls_back_to_wider_ef_search_without_iterative_scan(
    shallow_window, monkeypatch
):
    """On pgvector too old for iterative scans the leg buys a wider FIXED window
    instead — detected, not assumed, and it recovers the window on a corpus
    shallower than that width (a fixed window is a mitigation, not a guarantee,
    which is exactly why iterative_scan is preferred when present)."""
    monkeypatch.setattr(search_legs, "_hnsw_capabilities", lambda conn: (False, True))
    conn = _leg_conn()
    try:
        rows = _vector_leg(conn, QUERY, *_active_wall())
        ef = conn.execute("SELECT current_setting('hnsw.ef_search')").fetchone()[0]
        iterative = conn.execute("SELECT current_setting('hnsw.iterative_scan')").fetchone()[0]
    finally:
        conn.close()
    assert ef == str(VECTOR_EF_SEARCH_FALLBACK), "fallback did not widen ef_search"
    assert iterative == "off", "fallback path must not touch iterative_scan"
    assert len(rows) == CANDIDATES, (
        f"the wider fixed window returned {len(rows)} of {CANDIDATES} rows past "
        f"{SHALLOW_DECOYS} non-active neighbours"
    )
    assert _statuses([i for i, _t in rows]) == {"active"}


def test_hnsw_capability_probe_survives_a_cold_connection():
    """pgvector's GUCs only exist once its library is loaded; the probe must
    force that load, or every fresh connection reports 'unsupported' and the
    leg silently takes the fallback path forever."""
    conn = db.connect()
    try:
        cold = conn.execute("SELECT current_setting('hnsw.iterative_scan', true)").fetchone()[0]
        iterative, ef_search = search_legs._hnsw_capabilities(conn)
    finally:
        conn.close()
    assert cold is None, "pgvector was already loaded — the probe's premise no longer holds"
    assert iterative and ef_search, "capability probe failed to see pgvector's GUCs"


def test_fallback_window_stays_wider_than_the_candidate_limit():
    """Constant tripwire: the fallback's headroom over CANDIDATES IS the fix, so
    raising CANDIDATES must not silently re-open the hole."""
    assert VECTOR_EF_SEARCH_FALLBACK >= 2 * CANDIDATES, (
        "hnsw.ef_search fallback must stay well above the leg's LIMIT"
    )

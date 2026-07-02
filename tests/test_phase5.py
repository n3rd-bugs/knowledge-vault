"""Phase 5 — entity graph, trace, similar, stats, and the exact-symbol ranking
bump (PLAN.md §5.5 / §6 / §11 Phase 5). Real DB in kb_test; Ollama mocked by
conftest's deterministic fake_embeddings.
"""

from __future__ import annotations

import pytest

from kbcore import db, graph, render, stats
from kbcore.config import settings
from kbcore.entities import backfill_entities
from kbcore.ingest import ingest
from kbcore.search import _identifier_tokens, search
from kbcore.supersede import supersede

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


def _entities() -> set[tuple[str, str]]:
    with db.connect() as conn:
        return {(n, k) for n, k in conn.execute("SELECT name, kind FROM entities").fetchall()}


def _mention_targets(item_id: int) -> set[int]:
    with db.connect() as conn:
        return {
            r[0]
            for r in conn.execute(
                "SELECT dst_id FROM relations "
                "WHERE src_id=%s AND rel='mentions' AND dst_kind='entity'",
                (item_id,),
            ).fetchall()
        }


# --- entity derivation from tags + code_ref ----------------------------------


def test_entities_derived_from_tags_and_code_ref_on_ingest():
    item = ingest(
        "motor-ctl",
        "gotcha",
        "SPI polarity",
        "The DRV8706 needs SPI mode 1.",
        tags=["hw:tps55288", "protocol:spi", "lang:c", "tool:docker", "domain:power", "bogus"],
        code_ref={"repo": "motor-ctl", "path": "src/x.c"},
    )
    derived = _entities()
    assert ("tps55288", "hardware") in derived
    assert ("spi", "protocol") in derived
    assert ("c", "concept") in derived
    assert ("docker", "tool") in derived
    assert ("power", "concept") in derived
    assert ("motor-ctl", "repo") in derived
    assert not any(name == "bogus" for name, _ in derived), "namespaceless tag → no entity"
    # every derived entity is linked by a mentions edge from the item
    assert len(_mention_targets(item)) == len(derived)


def test_entity_upsert_is_idempotent_across_items():
    ingest("p", "note", "one", "body one about spi", tags=["protocol:spi"], uri="a")
    ingest("p", "note", "two", "body two about spi", tags=["protocol:spi"], uri="b")
    with db.connect() as conn:
        n = conn.execute(
            "SELECT count(*) FROM entities WHERE name='spi' AND kind='protocol'"
        ).fetchone()[0]
    assert n == 1, "shared tag upserts a single entity (ON CONFLICT)"


def test_backfill_entities_populates_pre_phase5_items():
    ingest("p", "note", "n", "body", tags=["hw:tps55288"], code_ref={"repo": "r", "path": "p"})
    # simulate a pre-Phase-5 item: wipe its derived graph
    with db.transaction() as conn:
        conn.execute("DELETE FROM relations WHERE rel='mentions' AND dst_kind='entity'")
        conn.execute("DELETE FROM entities")
    assert _entities() == set()

    processed = backfill_entities()
    assert processed >= 1
    derived = _entities()
    assert ("tps55288", "hardware") in derived and ("r", "repo") in derived


# --- trace -------------------------------------------------------------------


def _tree_ids(node: dict) -> set[tuple[str, int]]:
    ids = {(node["kind"], node["id"])}
    for edge in node.get("edges", []):
        ids |= _tree_ids(edge["node"])
    return ids


def _tree_rels(node: dict) -> set[str]:
    rels = set()
    for edge in node.get("edges", []):
        rels.add(edge["rel"])
        rels |= _tree_rels(edge["node"])
    return rels


def test_trace_walks_supersession_derived_from_and_entity_mention():
    c = ingest("p", "note", "C", "the original root cause writeup", uri="c")
    b = ingest("p", "decision", "B", "the interim decision", tags=["hw:tps55288"], uri="b")
    graph.link(b, "derived_from", c)
    a = ingest("p", "decision", "A", "the current decision", uri="a")
    supersede(b, new_item_id=a, reason="revised")  # A -supersedes-> B

    tree = graph.trace("item", a, depth=2)

    ids = _tree_ids(tree)
    assert ("item", a) in ids and ("item", b) in ids and ("item", c) in ids
    assert any(kind == "entity" for kind, _ in ids), "entity mention reached"
    rels = _tree_rels(tree)
    assert {"supersedes", "derived_from", "mentions"} <= rels
    # rendered tree carries ids/status/rels
    text = render.render_trace(tree)
    assert f"decision {a}" in text  # A renders with its type + id
    assert "-supersedes->" in text and "(superseded)" in text


def test_trace_missing_node_raises():
    from kbcore.errors import NotFound

    with pytest.raises(NotFound):
        graph.trace("item", 999999)


# --- similar -----------------------------------------------------------------


def test_similar_surfaces_the_fix_for_a_bug():
    bug = ingest("p", "bug", "UART drops bytes", "RX overrun at 115200 baud.", uri="bug")
    fix = ingest("p", "note", "UART fix", "Add a ring buffer in the RX ISR.", uri="fix")
    d1 = ingest("p", "note", "d1", "totally unrelated note about docker", uri="d1")
    d2 = ingest("p", "note", "d2", "another unrelated note about css", uri="d2")
    graph.link(fix, "fixes", bug)
    _force_lead(bug, _vec(1.0))
    _force_lead(fix, _vec(0.99, 0.141))  # cosine ≈ 0.99 to the bug
    _force_lead(d1, _vec(0.0, 1.0))
    _force_lead(d2, _vec(0.0, 0.0, 1.0))

    hits = graph.similar(bug, k=3)

    assert hits, "similar returns neighbours"
    assert fix in [h.item_id for h in hits[:3]], "the fix must surface in top-3"
    assert bug not in [h.item_id for h in hits], "self is excluded"


def test_similar_on_missing_item_raises_not_found():
    # Review finding: a stale/guessed id must raise a clean NotFound, not fall
    # through to log_access and trip the access_log FK with a raw psycopg error.
    from kbcore.errors import NotFound

    with pytest.raises(NotFound):
        graph.similar(999999, k=3)


def test_similar_excludes_non_active_items():
    a = ingest("p", "note", "anchor", "anchor body", uri="a")
    old = ingest("p", "note", "old", "will be superseded", uri="old")
    new = ingest("p", "note", "new", "the replacement", uri="new")
    supersede(old, new_item_id=new, reason="x")  # old → superseded
    _force_lead(a, _vec(1.0))
    _force_lead(old, _vec(1.0))  # identical vector, but superseded → excluded
    _force_lead(new, _vec(0.0, 1.0))

    ids = [h.item_id for h in graph.similar(a, k=5)]
    assert old not in ids, "superseded items are excluded from similar()"


# --- stats -------------------------------------------------------------------


def test_stats_counts():
    ingest("p1", "note", "n1", "body one", tags=["protocol:spi"])
    ingest("p1", "decision", "d1", "a decision body")
    ingest("p2", "gotcha", "g1", "a gotcha body", trust="unverified")

    s = stats.stats()
    assert s["items_total"] == 3
    assert s["by_type"]["note"] == 1 and s["by_type"]["decision"] == 1
    assert s["by_trust"].get("unverified") == 1 and s["by_trust"].get("verified") == 2
    assert s["by_project"]["p1"] == 2
    assert s["entities"] >= 1 and s["chunks"] >= 3
    assert s["review_queue_open"] == 0
    assert s["embed_model"] == settings.kb_embed_model
    assert "note=1" in render.render_stats(s)


# --- exact-symbol ranking bump ----------------------------------------------


def test_identifier_tokens_classification():
    assert _identifier_tokens("drv_spi_write gotcha") == ["drv_spi_write"]
    assert _identifier_tokens("TPS55288 I2C output") == ["TPS55288", "I2C"]
    assert _identifier_tokens("PageNumber current field") == ["PageNumber"]
    assert _identifier_tokens("why did we pick the regulator") == []
    # Tightened (review): a sentence-initial capital / proper noun / single
    # letter is NOT an identifier — the bonus must stay a no-op for plain prose.
    assert _identifier_tokens("How does Postgres handle A or I") == []
    assert _identifier_tokens("SPI OBD mode") == ["SPI", "OBD"]  # all-caps acronyms kept


def test_exact_symbol_survives_a_diluting_word():
    # Query "drv_spi_write gotcha": the FTS AND-leg matches neither item and the
    # vector leg is diluted by "gotcha"; the exact-identifier bonus lifts the
    # item that actually contains the whole symbol.
    a = ingest(
        "p", "snippet", "SPI write helper", "Call drv_spi_write to set the register.", uri="a"
    )
    ingest("p", "note", "General notes", "A common gotcha when debugging. gotcha gotcha.", uri="b")

    hits = search("drv_spi_write gotcha")

    assert hits and hits[0].item_id == a, "exact-symbol item ranks first despite the extra word"

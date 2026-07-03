"""Phase 8 — the projects.public allow-list (public/cloud safety gate).

A public-only instance (settings.kb_public_only, toggled per-test) sees ONLY
projects flagged public; private/NDA projects are invisible with NO existence
leak (withheld → NotFound). Writes to non-public projects are rejected. Real DB
in kb_test; Ollama mocked by conftest.
"""

from __future__ import annotations

import pytest

from kbcore import db, graph, ingest_queue, stats
from kbcore.config import settings
from kbcore.errors import InvalidOperation, NotFound
from kbcore.ingest import ingest
from kbcore.items import get_item, list_projects, set_project, timeline
from kbcore.search import search


def _vec(*head: float) -> str:
    v = [float(x) for x in head] + [0.0] * (settings.kb_embed_dim - len(head))
    return "[" + ",".join(repr(x) for x in v) + "]"


def _force_lead(item_id: int, vec: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE chunks SET embedding = %s::vector WHERE item_id = %s AND ord = 0",
            (vec, item_id),
        )


@pytest.fixture()
def seeded(kb):
    """A public project 'pub' and a private project 'priv', each with one item."""
    set_project("pub", domain="web", public=True)
    set_project("priv", domain="embedded", public=False)
    p = ingest(
        "pub",
        "note",
        "public widget note",
        "the public widget uses SPI mode 1",
        tags=["protocol:spi"],
    )
    q = ingest(
        "priv",
        "gotcha",
        "secret client gotcha",
        "the private client uses SPI mode 1 too",
        tags=["protocol:spi"],
    )
    return {"p": p, "q": q}


def _public_only(monkeypatch):
    monkeypatch.setattr(settings, "kb_public_only", True)


# --- default (full access) unchanged -----------------------------------------


def test_full_access_sees_everything(seeded):
    assert settings.kb_public_only is False
    hits = search("SPI mode 1", k=10)
    assert {seeded["p"], seeded["q"]} <= {h.item_id for h in hits}
    assert get_item(seeded["q"]).project == "priv"  # private visible on a full instance
    assert {"pub", "priv"} <= {p.name for p in list_projects()}


# --- public-only reads -------------------------------------------------------


def test_public_only_search_returns_only_public(seeded, monkeypatch):
    _public_only(monkeypatch)
    hits = search("SPI mode 1", k=10)
    assert {h.project for h in hits} == {"pub"}
    assert seeded["q"] not in {h.item_id for h in hits}
    assert seeded["p"] in {h.item_id for h in hits}


def test_public_only_get_private_item_is_notfound(seeded, monkeypatch):
    _public_only(monkeypatch)
    assert get_item(seeded["p"]).project == "pub"  # public still visible
    with pytest.raises(NotFound):  # private withheld — looks absent, no "forbidden"
        get_item(seeded["q"])


def test_public_only_list_projects_only_public(seeded, monkeypatch):
    _public_only(monkeypatch)
    projs = list_projects()
    assert {p.name for p in projs} == {"pub"}
    assert projs[0].public is True


def test_public_only_timeline_private_is_notfound(seeded, monkeypatch):
    _public_only(monkeypatch)
    assert timeline("pub")  # public project → items
    with pytest.raises(NotFound):
        timeline("priv")


def test_public_only_similar_excludes_private(seeded, monkeypatch):
    # Force pub & priv items to be nearest neighbours; without the gate priv would top.
    _force_lead(seeded["p"], _vec(1.0))
    _force_lead(seeded["q"], _vec(1.0))
    _public_only(monkeypatch)
    hits = graph.similar(seeded["p"], k=5)
    assert seeded["q"] not in {h.item_id for h in hits}
    assert all(h.project == "pub" for h in hits)


def test_public_only_trace_excludes_private(seeded, monkeypatch):
    graph.link(seeded["p"], "alternative_to", seeded["q"])  # cross-boundary edge (full access)
    _public_only(monkeypatch)

    tree = graph.trace("item", seeded["p"], depth=2)

    def ids(node):
        out = {(node["kind"], node["id"])}
        for e in node.get("edges", []):
            out |= ids(e["node"])
        return out

    reached = ids(tree)
    assert ("item", seeded["p"]) in reached
    assert ("item", seeded["q"]) not in reached, "trace must not traverse into a private item"
    with pytest.raises(NotFound):  # a private root is withheld
        graph.trace("item", seeded["q"])


def test_public_only_stats_counts_only_public(seeded, monkeypatch):
    _public_only(monkeypatch)
    s = stats.stats()
    assert s["items_total"] == 1
    assert set(s["by_project"]) == {"pub"}
    assert s["entities"] >= 1  # pub's spi entity, not priv's


# --- public-only writes ------------------------------------------------------


def test_public_only_writes_rejected_for_private_allowed_for_public(seeded, monkeypatch):
    _public_only(monkeypatch)
    # ingest / kb_save
    with pytest.raises(InvalidOperation):
        ingest("priv", "note", "t", "b")
    assert ingest("pub", "note", "another public note", "a public body") is not None
    # a brand-new (default-private) project is also not writable
    with pytest.raises(InvalidOperation):
        ingest("brand-new", "note", "t", "b")
    # link from a private src
    with pytest.raises(InvalidOperation):
        graph.link(seeded["q"], "alternative_to", seeded["p"])
    # enqueue
    with pytest.raises(InvalidOperation):
        ingest_queue.enqueue("priv", "some memory", kind="memory")
    assert ingest_queue.enqueue("pub", "public memory to queue", kind="memory") is not None


# --- flipping the flag + migration ------------------------------------------


def test_project_set_public_makes_it_visible(seeded, monkeypatch):
    _public_only(monkeypatch)
    with pytest.raises(NotFound):
        get_item(seeded["q"])  # private now
    # flip priv → public (a full-access admin action; here just the DB write)
    monkeypatch.setattr(settings, "kb_public_only", False)
    set_project("priv", public=True)
    monkeypatch.setattr(settings, "kb_public_only", True)
    assert get_item(seeded["q"]).project == "priv"  # now visible under public-only
    assert seeded["q"] in {h.item_id for h in search("SPI mode 1", k=10)}


def test_migration_003_applied_and_idempotent(kb):
    from kbcore.migrate import migrate

    with db.connect() as conn:
        cols = [
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'projects' AND table_schema = %s",
                (settings.kb_db_schema,),
            ).fetchall()
        ]
    assert "public" in cols
    assert migrate() == [], "re-running migrations is a no-op"


def test_public_item_relations_hide_private_endpoint(kb, monkeypatch):
    # A public item linked to a PRIVATE item must not expose the private id.
    from kbcore import graph
    from kbcore.config import settings
    from kbcore.ingest import ingest
    from kbcore.items import get_item, set_project

    set_project("pubp", public=True)
    set_project("privp", public=False)
    pub = ingest("pubp", "note", "public fix", "the fix", uri="pf")
    priv = ingest("privp", "bug", "private bug", "the bug", uri="pb")
    graph.link(pub, "fixes", priv)  # created by the full instance

    # full instance: relation visible
    assert any(r.dst_id == priv for r in get_item(pub).relations)
    # public-only instance: the private endpoint is filtered out
    monkeypatch.setattr(settings, "kb_public_only", True)
    rels = get_item(pub).relations
    leaked = [r for r in rels if priv in (r.dst_id, r.src_id)]
    assert not leaked, "private id leaked via relations"


def test_public_only_review_list_hides_private(kb, monkeypatch):
    from kbcore import review
    from kbcore.config import settings
    from kbcore.ingest import ingest
    from kbcore.items import set_project
    from kbcore.review import queue_review

    set_project("pubr", public=True)
    set_project("privr", public=False)
    pi = ingest("pubr", "note", "public review item", "body", uri="a", trust="unverified")
    si = ingest(
        "privr", "gotcha", "SECRET CLIENT NDA gotcha", "secret", uri="b", trust="unverified"
    )
    queue_review(pi, "unverified_extraction")
    queue_review(si, "unverified_extraction")
    monkeypatch.setattr(settings, "kb_public_only", True)
    titles = [e.title for e in review.list_open_review()]
    assert "public review item" in titles
    assert "SECRET CLIENT NDA gotcha" not in titles, "private title leaked via review list"


def test_public_only_supersede_rejects_private_replacement(kb, monkeypatch):
    from kbcore.config import settings
    from kbcore.errors import NotFound
    from kbcore.ingest import ingest
    from kbcore.items import set_project
    from kbcore.supersede import supersede

    set_project("pubs", public=True)
    set_project("privs", public=False)
    pub_old = ingest("pubs", "note", "pub old", "old", uri="o")
    priv_new = ingest("privs", "note", "priv new", "new", uri="n")
    monkeypatch.setattr(settings, "kb_public_only", True)
    with pytest.raises(NotFound):  # private replacement id must look absent
        supersede(pub_old, new_item_id=priv_new, reason="oracle attempt")


def test_public_only_link_evidence_and_dup_no_private_oracle(kb, monkeypatch):
    from kbcore import graph
    from kbcore.config import settings
    from kbcore.errors import NotFound
    from kbcore.ingest import find_duplicate, ingest
    from kbcore.items import set_project

    set_project("publ", public=True)
    set_project("privl", public=False)
    a = ingest("publ", "note", "a", "aa", uri="a")
    b = ingest("publ", "note", "b", "bb", uri="b")
    priv = ingest("privl", "note", "secret", "the secret body", uri="s", kind="chat")
    monkeypatch.setattr(settings, "kb_public_only", True)
    with pytest.raises(NotFound):  # evidence pointing at a private id → looks absent
        graph.link(a, "mentions", b, evidence_item=priv)
    # find_duplicate must not hand back the private id for matching content
    assert find_duplicate("secret", "the secret body", kind="chat", uri="s") is None

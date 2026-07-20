"""Per-project sharing — the NO-ORACLE walling matrix (the security-review target).

A grantee sees its granted projects and NOTHING else; a withheld project/item is
INDISTINGUISHABLE FROM NONEXISTENT across every read path (search + graph expansion
+ get + timeline + trace + similar + browse + stats + projects + relations-hide +
link-dst + supersede + duplicate-content), and rw is enforced on every write path
with a uniform message that leaks neither existence nor the read/write distinction.

Principals are bound directly via ``scope.use_principal`` (the auth-layer resolution
is covered in test_principals.py), so these assertions are transport-independent.
Real DB in kb_test; Ollama mocked by conftest.
"""

from __future__ import annotations

import pytest

from kbcore import browse, db, graph, ingest_queue, principals, scope, stats
from kbcore.config import settings
from kbcore.errors import InvalidOperation, NotFound
from kbcore.ingest import find_duplicate, ingest
from kbcore.items import get_item, list_projects, set_project, timeline
from kbcore.search import search
from kbcore.supersede import supersede

pytestmark = pytest.mark.usefixtures("kb")


def _vec(*head: float) -> str:
    v = [float(x) for x in head] + [0.0] * (settings.kb_embed_dim - len(head))
    return "[" + ",".join(repr(x) for x in v) + "]"


def _force_lead(item_id: int, vec: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE chunks SET embedding = %s::vector WHERE item_id = %s AND ord = 0",
            (vec, item_id),
        )


def _grantee(name: str, grants: dict[str, bool], role: str = "reader") -> scope.Principal:
    """A live token grantee. Since migration 012, WRITE-capable grantees carry
    role='contributor' (the backfill: an existing rw-grantee could already
    kb_save, so the migration seeds contributor — capabilities gate the write
    FEATURE, rw grants keep gating the ROWS). Tests that model a working writer
    pass role='contributor'; read-side tests keep the 'reader' default."""
    principals.create_principal(name, kind="token", role=role)
    for project, rw in grants.items():
        principals.grant(name, project, rw=rw)
    p = principals.resolve_name(name)
    assert p is not None
    return p


@pytest.fixture()
def world():
    """Projects a/b/c (all private) with one SPI item each; a grantee reads 'a'."""
    for proj in ("a", "b", "c"):
        set_project(proj, public=False)
    ids = {
        "a": ingest(
            "a",
            "note",
            "alpha spi note",
            "the alpha widget uses SPI mode 1",
            tags=["protocol:spi"],
            uri="a",
        ),
        "b": ingest(
            "b",
            "gotcha",
            "beta NDA gotcha",
            "the beta client uses SPI mode 1 too",
            tags=["protocol:spi"],
            uri="b",
        ),
        "c": ingest(
            "c",
            "note",
            "gamma spi note",
            "the gamma part uses SPI mode 1 also",
            tags=["protocol:spi"],
            uri="c",
        ),
    }
    return ids


# --- reads: only granted projects are visible --------------------------------


def test_search_returns_only_granted(world):
    g = _grantee("reader", {"a": False})
    with scope.use_principal(g):
        hits = search("SPI mode 1", k=10)
    projects = {h.project for h in hits}
    assert projects == {"a"}, "grantee search must not surface ungranted projects"
    ids = {h.item_id for h in hits}
    assert world["b"] not in ids and world["c"] not in ids


def test_get_withheld_is_byte_identical_notfound(world):
    g = _grantee("reader", {"a": False})
    # Baseline: a genuinely-missing id raises this exact message for the owner.
    with pytest.raises(NotFound) as missing:
        get_item(9_999_999)
    with scope.use_principal(g):
        assert get_item(world["a"]).project == "a"  # granted → visible
        with pytest.raises(NotFound) as withheld:
            get_item(world["b"])  # ungranted → looks absent
    assert str(withheld.value) == str(missing.value).replace("9999999", str(world["b"]))


def test_timeline_withheld_is_notfound(world):
    g = _grantee("reader", {"a": False})
    with scope.use_principal(g):
        assert timeline("a")  # granted project → items
        with pytest.raises(NotFound):
            timeline("b")  # ungranted → looks absent
        with pytest.raises(NotFound):
            timeline("does-not-exist")  # truly absent → same error class


def test_similar_excludes_withheld(world):
    _force_lead(world["a"], _vec(1.0))
    _force_lead(world["b"], _vec(1.0))  # force a & b to be nearest neighbours
    g = _grantee("reader", {"a": False})
    with scope.use_principal(g):
        hits = graph.similar(world["a"], k=5)
        assert world["b"] not in {h.item_id for h in hits}
        assert all(h.project == "a" for h in hits)
        with pytest.raises(NotFound):  # withheld anchor looks absent
            graph.similar(world["b"])


def test_trace_walls_off_withheld(world):
    graph.link(world["a"], "alternative_to", world["b"])  # cross-boundary (owner)
    g = _grantee("reader", {"a": False})
    with scope.use_principal(g):
        tree = graph.trace("item", world["a"], depth=2)

        def ids(node):
            out = {(node["kind"], node["id"])}
            for e in node.get("edges", []):
                out |= ids(e["node"])
            return out

        reached = ids(tree)
        assert ("item", world["a"]) in reached
        assert ("item", world["b"]) not in reached, "trace must not enter an ungranted item"
        with pytest.raises(NotFound):
            graph.trace("item", world["b"])  # withheld root looks absent


def test_projects_and_stats_only_granted(world):
    g = _grantee("reader", {"a": False})
    with scope.use_principal(g):
        assert {p.name for p in list_projects()} == {"a"}
        s = stats.stats()
        assert s["items_total"] == 1
        assert set(s["by_project"]) == {"a"}


def test_browse_hides_withheld(world):
    g = _grantee("reader", {"a": False})
    with scope.use_principal(g):
        refs = browse.item_refs([world["a"], world["b"], world["c"]])
        assert set(refs) == {world["a"]}, "withheld ids absent from item_refs"
        tag_items = {r.item_id for r in browse.items_with_tag("protocol:spi")}
        assert tag_items == {world["a"]}
        tags = dict(browse.list_tags())
        assert tags.get("protocol:spi") == 1  # only the granted item counted


def test_relations_hide_withheld_endpoint(world):
    graph.link(world["a"], "fixes", world["b"])  # granted item → withheld item
    g = _grantee("reader", {"a": False})
    with scope.use_principal(g):
        rels = get_item(world["a"]).relations
    leaked = [r for r in rels if world["b"] in (r.src_id, r.dst_id)]
    assert not leaked, "withheld item id leaked through a granted item's relation list"


def test_empty_grant_sees_nothing(world):
    g = _grantee("nobody", {})  # authenticated but no grants
    with scope.use_principal(g):
        assert search("SPI mode 1", k=10) == []
        assert list_projects() == []
        assert stats.stats()["items_total"] == 0
        with pytest.raises(NotFound):
            get_item(world["a"])


# --- graph expansion walling (search hops) -----------------------------------


def test_graph_expansion_never_surfaces_withheld_neighbor(world):
    # a-item links to a b-item; expanding a's neighborhood must not pull the b-item.
    graph.link(world["a"], "depends_on", world["b"])
    g = _grantee("reader", {"a": False})
    with scope.use_principal(g):
        hits = search("alpha widget SPI", hops=2, k=10)
    assert world["b"] not in {h.item_id for h in hits}, "expansion leaked a withheld neighbor"


def test_withheld_item_is_never_a_relay_hop(world):
    # X(granted a) — M(withheld b) — W(withheld c). W is reachable ONLY by relaying
    # through the withheld M, so expansion must surface NEITHER M NOR W: a withheld
    # node can neither be shown nor used to bridge to further items.
    x = ingest("a", "note", "relay source alpha", "alpha relay origin token", uri="x")
    w = ingest("c", "note", "relay sink withheld", "distinct withheld payload qqq", uri="w")
    m = world["b"]
    graph.link(x, "depends_on", m)
    graph.link(m, "depends_on", w)
    g = _grantee("reader", {"a": False})
    with scope.use_principal(g):
        hits = {h.item_id for h in search("alpha relay origin", hops=3, k=10)}
    assert m not in hits, "a withheld neighbor must never surface"
    assert w not in hits, "a withheld item must not relay expansion to another item"


# --- writes: rw enforced, uniform message ------------------------------------


def test_write_requires_rw_grant(world):
    g = _grantee("writer", {"a": True, "b": False}, role="contributor")  # rw on a, ro on b
    with scope.use_principal(g):
        assert ingest("a", "note", "new alpha", "written by grantee") is not None
        with pytest.raises(InvalidOperation):
            ingest("b", "note", "sneaky", "should be rejected")  # read-only grant
        with pytest.raises(InvalidOperation):
            ingest("c", "note", "sneaky", "no grant at all")  # no grant
        with pytest.raises(InvalidOperation):
            ingest_queue.enqueue("b", "memory dump", kind="memory")  # ro grant
        assert ingest_queue.enqueue("a", "memory dump", kind="memory") is not None


def test_write_rejection_message_is_uniform(world):
    """The write-rejection wording must not distinguish read-only-grant vs
    no-grant vs nonexistent (no existence/permission oracle)."""
    g = _grantee("writer", {"a": True, "b": False}, role="contributor")

    def normalized_reject(project: str) -> str:
        with scope.use_principal(g), pytest.raises(InvalidOperation) as ei:
            ingest(project, "note", "t", "b")
        return str(ei.value).replace(repr(project), "<P>")

    ro_grant = normalized_reject("b")  # exists, read-only
    no_grant = normalized_reject("c")  # exists, not granted
    ghost = normalized_reject("phantom-project")  # does not exist
    assert ro_grant == no_grant == ghost, "write rejection leaks project state"


def test_link_dst_and_evidence_no_oracle(world):
    g = _grantee("writer", {"a": True}, role="contributor")
    with pytest.raises(NotFound) as missing:
        with scope.use_principal(g):
            graph.link(world["a"], "mentions", 9_999_999)  # truly missing dst
    with scope.use_principal(g):
        with pytest.raises(NotFound) as withheld:
            graph.link(world["a"], "mentions", world["b"])  # withheld dst
        # withheld dst is byte-identical to a missing dst
        assert str(withheld.value) == str(missing.value).replace("9999999", str(world["b"]))
        with pytest.raises(NotFound):  # withheld evidence id looks absent too
            graph.link(world["a"], "mentions", world["a"], evidence_item=world["c"])


def test_supersede_withheld_replacement_no_oracle(world):
    g = _grantee("writer", {"a": True}, role="contributor")
    a2 = ingest("a", "note", "alpha v2 candidate", "alpha replacement body")
    with scope.use_principal(g):
        # a valid same-project supersede works
        assert supersede(world["a"], new_item_id=a2, reason="update") == a2
    a_old = ingest("a", "note", "another alpha", "another alpha body", uri="ao")
    with scope.use_principal(g):
        with pytest.raises(NotFound):  # withheld replacement id looks absent
            supersede(a_old, new_item_id=world["b"], reason="oracle attempt")


def test_link_withheld_src_no_oracle(world):
    """Linking FROM a withheld src must be byte-identical to a missing src id: the
    SAME NotFound (type + message), hence the SAME REST 404 / MCP ToolError string —
    never an InvalidOperation naming the withheld project. Read-only AND zero-grant
    grantees both, since the src write-check must fire identically for either."""
    for i, grants in enumerate(({"a": False}, {})):  # read-only on 'a', and no grants
        g = _grantee(f"linksrc_{i}", grants)
        with scope.use_principal(g), pytest.raises(NotFound) as missing:
            graph.link(9_999_999, "mentions", world["a"])  # truly missing src
        with scope.use_principal(g), pytest.raises(NotFound) as withheld:
            graph.link(world["b"], "mentions", world["a"])  # existent, withheld project
        assert str(withheld.value) == str(missing.value).replace("9999999", str(world["b"]))


def test_supersede_withheld_old_id_no_oracle(world):
    """Superseding a withheld old_id must be byte-identical to a missing old_id — same
    NotFound type + message (same 404 / ToolError), never a project-naming
    InvalidOperation. Read-only AND zero-grant grantees both."""
    for i, grants in enumerate(({"a": False}, {})):
        g = _grantee(f"supold_{i}", grants)
        with scope.use_principal(g), pytest.raises(NotFound) as missing:
            supersede(9_999_999, new_body="x", reason="y")  # truly missing old
        with scope.use_principal(g), pytest.raises(NotFound) as withheld:
            supersede(world["b"], new_body="x", reason="y")  # existent, withheld
        assert str(withheld.value) == str(missing.value).replace("9999999", str(world["b"]))


def test_superseded_by_masked_when_successor_withheld(world):
    """A denormalized cross-project supersession pointer must not leak the withheld
    successor's id or free-text reason through the scalar column (get_item AND search
    AND timeline). The reciprocal relation edge is already hidden; the scalar must
    match. Owner sees the real values."""
    a_item = world["a"]
    # Owner retires a shared 'a' item in favour of a withheld 'b' item.
    supersede(a_item, new_item_id=world["b"], reason="secret cross-project reason STD-9")
    g = _grantee("reader", {"a": False})
    with scope.use_principal(g):
        d = get_item(a_item)
        assert d.superseded_by is None, "withheld successor id leaked via get_item scalar"
        assert d.supersede_reason is None, "withheld supersede reason leaked via get_item"
        tl = [it for it in timeline("a") if it.item_id == a_item]
        assert tl, "superseded 'a' item should appear in its reader's timeline"
        assert tl[0].superseded_by is None and tl[0].supersede_reason is None
        h = next(
            (
                x
                for x in search("alpha widget SPI", include_superseded=True, k=10)
                if x.item_id == a_item
            ),
            None,
        )
        assert h is not None, "superseded 'a' item should still surface for its reader"
        assert h.superseded_by is None, "withheld successor id leaked via search hit"
    # Owner sees the true pointer + reason (no masking).
    od = get_item(a_item)
    assert od.superseded_by == world["b"]
    assert od.supersede_reason == "secret cross-project reason STD-9"


def test_ungrant_clears_principal_cache(world):
    """A de-scoped project must stop being readable within the SAME process — ungrant()
    flushes the positive token cache instead of lagging up to the cache TTL."""
    _pid, token = principals.create_principal("cached", kind="token")
    principals.grant("cached", "a", rw=False)
    p1 = principals.resolve_token(token)  # positive resolution → cached WITH grant 'a'
    assert p1 is not None and "a" in p1.read_projects
    with scope.use_principal(p1):
        assert get_item(world["a"]).project == "a"
    principals.ungrant("cached", "a")
    p2 = principals.resolve_token(token)  # cache flushed → reflects the removed grant
    assert p2 is not None and "a" not in p2.read_projects
    with scope.use_principal(p2), pytest.raises(NotFound):
        get_item(world["a"])  # denied now, no TTL wait


def test_grantee_write_is_audited(world):
    """A grantee's writes are attributable: ingest and link stamp an access_log row
    bearing the grantee's principal name (owner/local writes stay unattributed)."""
    g = _grantee("auditor", {"a": True}, role="contributor")
    with scope.use_principal(g):
        new_id = ingest("a", "note", "audited note", "written by grantee auditor", uri="aud")
        link_src = ingest("a", "note", "audited link src", "grantee link source body", uri="ls")
        graph.link(link_src, "mentions", world["a"])
    with db.connect() as conn:
        ingest_rows = conn.execute(
            "SELECT principal, tool FROM access_log WHERE item_id = %s", (new_id,)
        ).fetchall()
        link_rows = conn.execute(
            "SELECT principal, tool FROM access_log WHERE item_id = %s AND tool = 'link'",
            (link_src,),
        ).fetchall()
    assert ("auditor", "ingest") in ingest_rows, "grantee ingest not attributed"
    assert ("auditor", "link") in link_rows, "grantee link not attributed"


def test_find_duplicate_and_kb_save_dup_no_oracle(world):
    # A grantee saving content that duplicates a WITHHELD item must not get its id.
    # Positive control: the OWNER genuinely matches it, so a None for the grantee is
    # the gate withholding — not a content mismatch.
    assert (
        find_duplicate(
            "beta NDA gotcha", "the beta client uses SPI mode 1 too", kind="manual", uri="b"
        )
        == world["b"]
    )
    g = _grantee("writer", {"a": True}, role="contributor")
    with scope.use_principal(g):
        dup = find_duplicate(
            "beta NDA gotcha", "the beta client uses SPI mode 1 too", kind="manual", uri="b"
        )
        assert dup is None, "duplicate lookup leaked a withheld item id"


def test_review_queue_probe_no_project_name_oracle(world):
    """Resolving a review row whose item is WITHHELD must be byte-identical to a
    missing queue id (Phase 1 fix: _queued_item_id gates readability BEFORE
    writability — assert_writable's rejection embeds the project name, so
    probing queue ids would otherwise enumerate withheld NDA project names)."""
    from kbcore import review

    nda = ingest("nda-withheld-client", "note", "secret finding", "secret NDA body")
    # A contradiction row spanning granted + withheld (plan §5 Phase 1 AC).
    qid = review.queue_review(nda, "contradiction", {"a_id": world["a"], "b_id": nda})
    g = _grantee("resolver", {"a": True}, role="contributor")
    for resolve in (
        review.promote,
        review.reject,
        lambda q: review.resolve_via_supersede(q, world["a"]),
    ):
        with scope.use_principal(g), pytest.raises(NotFound) as withheld:
            resolve(qid)
        with scope.use_principal(g), pytest.raises(NotFound) as missing:
            resolve(9_999_999)
        assert str(withheld.value) == str(missing.value).replace("9999999", str(qid))
        assert "nda-withheld-client" not in str(withheld.value), "project name leaked"
    assert review.promote(qid) == nda  # owner still resolves it (no regression)


# --- owner-only surfaces ------------------------------------------------------


def test_require_owner_blocks_grantee(world):
    g = _grantee("reader", {"a": False})
    with scope.use_principal(g):
        with pytest.raises(InvalidOperation):
            scope.require_owner("kb_review_list")
    with scope.use_principal(scope.OWNER):
        scope.require_owner("kb_review_list")  # owner passes, no raise

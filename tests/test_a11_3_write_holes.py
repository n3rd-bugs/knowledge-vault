"""A11-3 write-fork prerequisites — four LIVE kbcore security holes closed at the
kbcore CHOKEPOINT (Invariant 2), so every surface (web/MCP/REST/future) inherits
the trust/authorization guarantees instead of relying on any route layer.

FIX 1 — ingest_in trust chokepoint: a non-owner can never mint sources.trust='verified'.
FIX 2 — the secret scrubber now covers uri / tags / code_ref / meta, not just title/body.
FIX 3 — review.promote is OWNER-ONLY; reject/dismiss require CAP_REVIEW_RESOLVE and
        the readable-then-writable project walls; reject is confined to unverified
        targets for non-owners.
FIX 4 — graph.link requires WRITE on BOTH endpoints.

Principals are bound directly via scope.use_principal (transport-independent, per
test_no_oracle). Real DB in kb_test; Ollama mocked by conftest.
"""

from __future__ import annotations

import pytest

from kbcore import db, graph, principals, review, review_auto, scope
from kbcore.errors import InvalidOperation, NotFound
from kbcore.ingest import ingest
from kbcore.items import get_item, set_project
from kbcore.review import queue_review
from kbcore.supersede import supersede

pytestmark = pytest.mark.usefixtures("kb")

FAKE_GHP = "ghp_" + "0" * 36


def _grantee(
    name: str,
    grants: dict[str, bool],
    role: str = "contributor",
    extra_caps: frozenset[str] = frozenset(),
) -> scope.Principal:
    """A live token grantee. ``extra_caps`` adds capabilities no ROLE_CAPS bundle
    grants (e.g. CAP_REVIEW_RESOLVE) by constructing the Principal directly — the
    resolver never mints such a bundle, which is exactly the point."""
    principals.create_principal(name, kind="token", role=role)
    for project, rw in grants.items():
        principals.grant(name, project, rw=rw)
    p = principals.resolve_name(name)
    assert p is not None
    if extra_caps:
        p = scope.Principal(
            name=p.name,
            is_owner=False,
            read_projects=p.read_projects,
            write_projects=p.write_projects,
            kind=p.kind,
            role=p.role,
            caps=p.caps | extra_caps,
        )
    return p


def _source_trust(item_id: int) -> str:
    with db.connect() as conn:
        return conn.execute(
            "SELECT s.trust FROM sources s JOIN items i ON i.source_id = s.id WHERE i.id = %s",
            (item_id,),
        ).fetchone()[0]


def _source_uri(item_id: int) -> str:
    with db.connect() as conn:
        return conn.execute(
            "SELECT s.uri FROM sources s JOIN items i ON i.source_id = s.id WHERE i.id = %s",
            (item_id,),
        ).fetchone()[0]


def _resolved(qid: int) -> bool:
    with db.connect() as conn:
        return bool(
            conn.execute("SELECT resolved FROM review_queue WHERE id = %s", (qid,)).fetchone()[0]
        )


def _unverified_row(project: str, tag: str, trust: str = "unverified") -> tuple[int, int]:
    iid = ingest(project, "note", f"t-{tag}", f"body {tag}", trust=trust, uri=tag)
    return iid, queue_review(iid, "unverified_extraction", {"rejected_count": 0})


# --- FIX 1: the trust chokepoint ---------------------------------------------


def test_fix1_nonowner_ingest_coerced_to_unverified():
    set_project("a", public=False)
    g = _grantee("w1", {"a": True})
    with scope.use_principal(g):
        iid = ingest("a", "note", "grantee decision", "grantee body", trust="verified", uri="g1")
    assert iid is not None
    assert _source_trust(iid) == "unverified", "a non-owner must never mint verified"


def test_fix1_owner_keeps_explicit_verified():
    set_project("a", public=False)
    iid = ingest("a", "note", "owner decision", "owner body", trust="verified", uri="o1")
    assert _source_trust(iid) == "verified", "owner behavior unchanged"


def test_fix1_nonowner_supersede_new_body_is_unverified():
    """supersede's new_body path composes through ingest_in, so the chokepoint
    downgrades its hardcoded trust='verified' for a non-owner (findings 4/5/8)."""
    set_project("a", public=False)
    old = ingest("a", "note", "seed", "original body", trust="verified", uri="s1")
    g = _grantee("w2", {"a": True})
    with scope.use_principal(g):
        new_id = supersede(old, new_body="corrected grantee body", reason="fix")
    assert _source_trust(new_id) == "unverified"
    # owner's supersede still mints verified
    old2 = ingest("a", "note", "seed2", "original body two", trust="verified", uri="s2")
    new2 = supersede(old2, new_body="owner correction body", reason="fix")
    assert _source_trust(new2) == "verified"


# --- FIX 2: scrub uri / tags / code_ref / meta -------------------------------


def test_fix2_scrubs_tags_uri_coderef_and_meta():
    set_project("a", public=False)
    iid = ingest(
        "a",
        "note",
        "clean title",
        "clean body",
        tags=[f"token:{FAKE_GHP}", "lang:python"],
        uri=f"https://evil.example/x?t={FAKE_GHP}",
        code_ref={"repo": "myrepo", "path": "src/x.py", "symbol": f"fn_{FAKE_GHP}"},
        meta={"note": f"leaked {FAKE_GHP} here"},
    )
    assert iid is not None
    item = get_item(iid)
    # tag redacted; a normal tag is untouched
    assert f"token:{FAKE_GHP}" not in item.tags
    assert any("[REDACTED:github_token]" in t for t in item.tags)
    assert "lang:python" in item.tags
    # code_ref: secret field redacted, clean field unchanged
    assert item.code_ref is not None
    assert FAKE_GHP not in (item.code_ref.symbol or "")
    assert "[REDACTED:github_token]" in (item.code_ref.symbol or "")
    assert item.code_ref.repo == "myrepo"
    # meta string leaf + uri (at the source) redacted
    assert FAKE_GHP not in str(item.meta)
    assert FAKE_GHP not in _source_uri(iid) and "[REDACTED:github_token]" in _source_uri(iid)
    # redaction counts folded into meta.redactions (tag + uri + symbol + meta note)
    assert item.meta.get("redactions", {}).get("github_token", 0) >= 3


def test_fix2_normal_fields_unchanged():
    set_project("a", public=False)
    iid = ingest(
        "a", "note", "t", "b", tags=["lang:python"], uri="https://example.com/doc", code_ref=None
    )
    item = get_item(iid)
    assert item.tags == ["lang:python"]
    assert _source_uri(iid) == "https://example.com/doc"
    assert "redactions" not in item.meta


def test_fix2_reingest_is_idempotent():
    """scrub is deterministic, so re-ingesting a secret-bearing tag/uri dedupes."""
    set_project("a", public=False)
    kw = {"tags": [f"token:{FAKE_GHP}"], "uri": f"https://evil.example/y?t={FAKE_GHP}"}
    first = ingest("a", "note", "dedupe title", "dedupe body", **kw)
    assert first is not None
    second = ingest("a", "note", "dedupe title", "dedupe body", **kw)
    assert second is None, "re-ingest of identical content must dedupe to a no-op"


# --- FIX 3: review-resolve authorization backstops ---------------------------


def test_fix3_promote_is_owner_only_even_with_cap_and_rw():
    set_project("a", public=False)
    iid, q = _unverified_row("a", "p1")
    g = _grantee("rev1", {"a": True}, extra_caps=frozenset({scope.CAP_REVIEW_RESOLVE}))
    with scope.use_principal(g), pytest.raises(InvalidOperation):
        review.promote(q)  # trust escalation is owner-only regardless of cap + rw
    assert _source_trust(iid) == "unverified", "no escalation happened"
    assert review.promote(q) == iid  # owner still promotes
    assert _source_trust(iid) == "verified"


def test_fix3_reject_and_dismiss_require_cap():
    set_project("a", public=False)
    _i1, q1 = _unverified_row("a", "r1")
    _i2, q2 = _unverified_row("a", "d1")
    g = _grantee("noresolve", {"a": True})  # contributor: rw but NO review-resolve cap
    with scope.use_principal(g), pytest.raises(InvalidOperation):
        review.reject(q1)
    with scope.use_principal(g), pytest.raises(InvalidOperation):
        review.dismiss(q2)
    assert _resolved(q1) is False and _resolved(q2) is False, "no row flipped"


def test_fix3_reject_and_dismiss_with_cap_on_unverified_rw_row():
    set_project("a", public=False)
    ir, qr = _unverified_row("a", "ur")
    idd, qd = _unverified_row("a", "ud")
    g = _grantee("resolver", {"a": True}, extra_caps=frozenset({scope.CAP_REVIEW_RESOLVE}))
    with scope.use_principal(g):
        assert review.reject(qr) == ir
        review.dismiss(qd)
    assert get_item(ir).status == "archived"
    assert _resolved(qd) is True
    assert get_item(idd).status == "active", "dismiss leaves the item untouched"


def test_fix3_reject_verified_target_stays_owner_only():
    set_project("a", public=False)
    iid = ingest("a", "note", "verified item", "trusted body", trust="verified", uri="v1")
    q = queue_review(iid, "contradiction", {"a_id": iid, "b_id": iid})
    g = _grantee("resolver2", {"a": True}, extra_caps=frozenset({scope.CAP_REVIEW_RESOLVE}))
    with scope.use_principal(g), pytest.raises(InvalidOperation):
        review.reject(q)  # archiving verified knowledge stays owner-only
    assert get_item(iid).status == "active", "verified item must not be archived by a grantee"
    assert _resolved(q) is False
    assert review.reject(q) == iid  # owner CAN reject a verified target
    assert get_item(iid).status == "archived"


def test_fix3_dismiss_withheld_row_is_byte_identical_notfound():
    """A non-owner with the cap gets byte-identical NotFound for a nonexistent qid,
    an item-less sentinel row, and a withheld-project row — no existence/state
    oracle, no project-name leak."""
    set_project("a", public=False)
    set_project("b", public=False)
    _ia, _qa = _unverified_row("a", "keep")
    _ib, qb = _unverified_row("b", "hidden")  # withheld project for the grantee
    sentinel = queue_review(None, "search_regression", {"note": "hit@5 dropped"})
    g = _grantee("dresolver", {"a": True}, extra_caps=frozenset({scope.CAP_REVIEW_RESOLVE}))
    with scope.use_principal(g):
        with pytest.raises(NotFound) as missing:
            review.dismiss(9_999_999)
        with pytest.raises(NotFound) as withheld:
            review.dismiss(qb)
        with pytest.raises(NotFound) as sent:
            review.dismiss(sentinel)
    # Byte-identical to the missing-row NotFound (only the qid differs) — the row's
    # project name never appears, so open/sentinel/withheld are indistinguishable.
    assert str(withheld.value) == str(missing.value).replace("9999999", str(qb))
    assert str(sent.value) == str(missing.value).replace("9999999", str(sentinel))
    assert _resolved(qb) is False and _resolved(sentinel) is False, "no withheld row flipped"


def test_fix3_reject_item_less_row_is_byte_identical_notfound():
    """FIX 1 (A11-3 reject-path item-less oracle): a non-owner reviewer's reject of an
    item-less sentinel row, a nonexistent qid, and a withheld-project qid all raise the
    BYTE-IDENTICAL NotFound — no existence oracle for the search_regression sentinel
    (the NULL-item branch previously raised InvalidOperation → web 403 for ALL
    principals, while missing/withheld rows 404'd). reject now inherits the byte-
    identical 404 that dismiss already has. The OWNER still gets InvalidOperation on
    the sentinel (behavior unchanged)."""
    set_project("a", public=False)
    set_project("b", public=False)
    _ia, _qa = _unverified_row("a", "rkeep")
    _ib, qb = _unverified_row("b", "rhidden")  # withheld project for the grantee
    sentinel = queue_review(None, "search_regression", {"note": "hit@5 dropped"})
    g = _grantee("rresolver", {"a": True}, extra_caps=frozenset({scope.CAP_REVIEW_RESOLVE}))
    with scope.use_principal(g):
        with pytest.raises(NotFound) as missing:
            review.reject(9_999_999)
        with pytest.raises(NotFound) as withheld:
            review.reject(qb)
        with pytest.raises(NotFound) as sent:
            review.reject(sentinel)
    # Byte-identical to the missing-row NotFound (only the qid differs) — the item-less
    # sentinel and the withheld-project row are indistinguishable from a nonexistent one.
    assert str(withheld.value) == str(missing.value).replace("9999999", str(qb))
    assert str(sent.value) == str(missing.value).replace("9999999", str(sentinel))
    assert _resolved(qb) is False and _resolved(sentinel) is False, "no row flipped"
    # owner still gets InvalidOperation on the item-less sentinel (unchanged)
    with pytest.raises(InvalidOperation):
        review.reject(sentinel)
    assert _resolved(sentinel) is False, "the sentinel is not resolved by the failed reject"


def test_fix3_bulk_dismiss_under_grantee_inherits_walls_as_per_row_errors():
    set_project("a", public=False)
    set_project("b", public=False)
    _ia, qa = _unverified_row("a", "ba")
    _ib, qb = _unverified_row("b", "bb")  # withheld project for the grantee
    g = _grantee("bulker", {"a": True}, extra_caps=frozenset({scope.CAP_REVIEW_RESOLVE}))
    with scope.use_principal(g):
        out = dict(review_auto.bulk_resolve("dismiss", queue_ids=[qa, qb]))
    assert out[qa] == "dismissed"
    assert out[qb] == f"error: review queue row {qb} does not exist"
    assert _resolved(qa) is True and _resolved(qb) is False, "no cross-project flip"


def test_fix3_owner_dismiss_paths_unchanged():
    """Owner keeps today's behavior: dismiss an item-bearing row and an item-less
    sentinel row; double-dismiss and missing qid keep their messages."""
    set_project("a", public=False)
    item, q = _unverified_row("a", "od")
    review.dismiss(q)
    assert _resolved(q) is True and get_item(item).status == "active"
    sentinel = queue_review(None, "search_regression", {"note": "x"})
    review.dismiss(sentinel)
    assert _resolved(sentinel) is True
    with pytest.raises(InvalidOperation):
        review.dismiss(q)  # already resolved
    with pytest.raises(NotFound):
        review.dismiss(9_999_999)


# --- FIX 4: graph.link requires WRITE on the dst -----------------------------


def test_fix4_link_requires_write_on_dst():
    set_project("s", public=False)
    set_project("d", public=False)
    s_item = ingest("s", "note", "src", "src body", uri="si")
    d_item = ingest("d", "note", "dst", "dst body", uri="di")
    g = _grantee("linker", {"s": True, "d": False})  # rw on s, READ-ONLY on d
    with scope.use_principal(g), pytest.raises(InvalidOperation):
        graph.link(s_item, "mentions", d_item)  # dst not writable → refused
    with db.connect() as conn:
        n = conn.execute(
            "SELECT count(*) FROM relations WHERE src_id = %s AND dst_id = %s", (s_item, d_item)
        ).fetchone()[0]
    assert n == 0, "no inbound edge written into a read-only-dst project"
    # rw on BOTH endpoints works
    principals.grant("linker", "d", rw=True)
    principals._clear_cache()
    g2 = principals.resolve_name("linker")
    assert g2 is not None
    with scope.use_principal(g2):
        assert graph.link(s_item, "mentions", d_item) is not None
    # owner links across the boundary (unrestricted)
    assert graph.link(d_item, "alternative_to", s_item) is not None

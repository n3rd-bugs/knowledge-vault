"""A11-3 Branch B (WP-A11-3-B) — the composed kbcore web-write surface.

kbcore.web_write.{save_for_review, supersede_for_review} are the ONLY functions the
web layer imports for knowledge writes; every trust/quarantine/review guarantee is
hardwired in kbcore (Invariant 2), never in a route. This pins the quarantine
lifecycle end to end:

  non-owner web save -> pending + unverified + 'web_contribution' review row
                     -> OWNER promote  -> active + verified (visible in search)
                     -> OWNER reject   -> archived

and the supersede-as-proposal semantics (the OLD item stays active until the
replacement is promoted, so a contributor can never hide verified knowledge behind
an invisible pending item). Principals are bound directly via scope.use_principal
(transport-independent, per test_a11_3_write_holes). Real DB in kb_test; Ollama mocked.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kbcore import db, principals, review, scope, web_write
from kbcore.errors import DuplicateContent, InvalidOperation
from kbcore.ingest import ingest
from kbcore.items import get_item, set_project
from kbcore.models import WebSaveRequest
from kbcore.search import search

pytestmark = pytest.mark.usefixtures("kb")


# --- helpers -----------------------------------------------------------------


def _grantee(
    name: str,
    grants: dict[str, bool],
    role: str = "contributor",
    extra_caps: frozenset[str] = frozenset(),
) -> scope.Principal:
    """A live token grantee (mirror of test_a11_3_write_holes)."""
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


def _rq(qid: int) -> tuple[int | None, str, dict, bool]:
    with db.connect() as conn:
        item_id, reason, detail, resolved = conn.execute(
            "SELECT item_id, reason, detail, resolved FROM review_queue WHERE id = %s", (qid,)
        ).fetchone()
    return item_id, reason, (detail if isinstance(detail, dict) else {}), bool(resolved)


def _search_ids(query: str, **kw) -> list[int]:
    # tool=None skips access logging; hops=0 keeps the result to direct hits only.
    return [h.item_id for h in search(query, hops=0, tool=None, **kw)]


# --- save_for_review: the contributor-save contract --------------------------


def test_save_for_review_quarantines_and_queues():
    set_project("a", public=False)
    g = _grantee("c1", {"a": True})
    with scope.use_principal(g):
        item_id, qid = web_write.save_for_review(
            "a", "gotcha", "deploy flakiness fix zorbit", "body about flakiness zorbit"
        )
    # item is quarantined + unverified
    assert get_item(item_id).status == "pending"
    assert _source_trust(item_id) == "unverified"
    # review row references THIS item, correct reason + web-origin detail
    r_item, reason, detail, resolved = _rq(qid)
    assert r_item == item_id
    assert reason == "web_contribution"
    assert detail.get("origin") == "web"
    assert detail.get("principal") == "c1"
    assert resolved is False
    # ABSENT from default search for the OWNER (finding 6 — no injection channel)
    assert item_id not in _search_ids("zorbit")
    # ABSENT from default search for the GRANTEE too
    with scope.use_principal(g):
        assert item_id not in _search_ids("zorbit")


def test_save_for_review_rejects_consolidation_types():
    set_project("a", public=False)
    g = _grantee("c2", {"a": True})
    with scope.use_principal(g):
        for bad in ("summary", "report"):
            with pytest.raises(InvalidOperation):
                web_write.save_for_review("a", bad, "t", "b")


def test_save_for_review_requires_cap_save():
    set_project("a", public=False)
    # a reader has rw on 'a' but NO CAP_SAVE — the explicit require_cap gate fires
    reader = _grantee("reader1", {"a": True}, role="reader")
    assert scope.CAP_SAVE not in reader.caps
    with scope.use_principal(reader), pytest.raises(InvalidOperation):
        web_write.save_for_review("a", "note", "t", "b")


def test_save_for_review_duplicate_raises_no_orphan_row():
    set_project("a", public=False)
    g = _grantee("c3", {"a": True})
    with scope.use_principal(g):
        _iid, _qid = web_write.save_for_review("a", "note", "dup title", "dup body")
        with pytest.raises(DuplicateContent):
            web_write.save_for_review("a", "note", "dup title", "dup body")
    with db.connect() as conn:
        n = conn.execute("SELECT count(*) FROM review_queue").fetchone()[0]
    assert n == 1, "the duplicate re-submit must not create a second (orphan) review row"


# --- the full quarantine lifecycle -------------------------------------------


def test_lifecycle_promote_unquarantines_and_verifies():
    set_project("a", public=False)
    g = _grantee("c4", {"a": True})
    with scope.use_principal(g):
        item_id, qid = web_write.save_for_review(
            "a", "decision", "keep alive plombus", "body plombus keepalive"
        )
    assert item_id not in _search_ids("plombus")  # quarantined pre-promote
    # OWNER promote: pending -> active, unverified -> verified, row resolved
    assert review.promote(qid) == item_id
    assert get_item(item_id).status == "active"
    assert _source_trust(item_id) == "verified"
    assert _rq(qid)[3] is True
    # NOW visible in default search
    assert item_id in _search_ids("plombus")


def test_lifecycle_reject_archives():
    set_project("a", public=False)
    g = _grantee("c5", {"a": True})
    with scope.use_principal(g):
        item_id, qid = web_write.save_for_review("a", "note", "reject me grumbo", "body grumbo")
    # OWNER reject: archived (Invariant 5 status flip), row resolved
    assert review.reject(qid) == item_id
    assert get_item(item_id).status == "archived"
    assert _rq(qid)[3] is True
    assert item_id not in _search_ids("grumbo")


# --- supersede_for_review: proposal, not act ---------------------------------


def test_supersede_for_review_is_a_proposal_until_promote():
    set_project("a", public=False)
    # an OWNER-authored VERIFIED item is the target
    old = ingest(
        "a",
        "decision",
        "prod db is postgres wobblet",
        "postgres wobblet body",
        trust="verified",
        uri="old-wobblet",
    )
    assert _source_trust(old) == "verified"
    g = _grantee("s1", {"a": True})
    with scope.use_principal(g):
        new_id, qid = web_write.supersede_for_review(
            old, new_body="actually mysql now wobblet", reason="migrated off postgres"
        )
    # replacement is pending + unverified
    assert get_item(new_id).status == "pending"
    assert _source_trust(new_id) == "unverified"
    # review row targets the REPLACEMENT (finding 8), carries the proposal marker
    r_item, reason, detail, _resolved = _rq(qid)
    assert r_item == new_id
    assert reason == "web_contribution"
    assert detail.get("proposed_supersede") == old
    # CRITICAL: the OLD item STAYS active until promote (no hiding verified knowledge)
    assert get_item(old).status == "active"
    assert old in _search_ids("wobblet")  # the verified original still answers
    assert new_id not in _search_ids("wobblet")  # the pending replacement is invisible
    # OWNER promote applies the deferred supersession
    review.promote(qid)
    assert get_item(new_id).status == "active"
    assert _source_trust(new_id) == "verified"
    old_after = get_item(old)
    assert old_after.status == "superseded"
    assert old_after.superseded_by == new_id


def test_supersede_for_review_reject_leaves_original_untouched():
    set_project("a", public=False)
    old = ingest(
        "a",
        "note",
        "keep original snarfle",
        "original snarfle body",
        trust="verified",
        uri="old-snarfle",
    )
    g = _grantee("s2", {"a": True}, extra_caps=frozenset({scope.CAP_REVIEW_RESOLVE}))
    with scope.use_principal(g):
        new_id, qid = web_write.supersede_for_review(
            old, new_body="forged snarfle correction", reason="nope"
        )
        # a contributor with the resolve cap may reject its own unverified replacement
        review.reject(qid)
    assert get_item(new_id).status == "archived"
    # the original is entirely untouched: still active, never superseded
    assert get_item(old).status == "active"
    assert get_item(old).superseded_by is None


# --- include_pending is owner-only -------------------------------------------


def test_include_pending_is_owner_only():
    set_project("a", public=False)
    g = _grantee("ip1", {"a": True})
    with scope.use_principal(g):
        item_id, _qid = web_write.save_for_review(
            "a", "note", "quarantined flubberx", "flubberx body"
        )
    # default: absent for owner AND grantee
    assert item_id not in _search_ids("flubberx")
    with scope.use_principal(g):
        assert item_id not in _search_ids("flubberx")
        # a grantee passing include_pending=True is a NO-OP (owner-gated chokepoint)
        assert item_id not in _search_ids("flubberx", include_pending=True)
    # only the OWNER can surface pending via include_pending=True
    assert item_id in _search_ids("flubberx", include_pending=True)


# --- WebSaveRequest closed shape ---------------------------------------------


def test_websaverequest_valid_payload_parses():
    req = WebSaveRequest(project="a", type="decision", title="t", body="b", tags=["lang:python"])
    assert req.type == "decision"
    assert req.tags == ["lang:python"]
    assert req.code_ref is None


@pytest.mark.parametrize("field", ["trust", "kind", "uri", "raw", "status", "meta", "is_source"])
def test_websaverequest_forbids_forged_fields(field):
    with pytest.raises(ValidationError):
        WebSaveRequest(project="a", type="note", title="t", body="b", **{field: "x"})


@pytest.mark.parametrize("bad_type", ["summary", "report", "bogus"])
def test_websaverequest_rejects_non_web_types(bad_type):
    with pytest.raises(ValidationError):
        WebSaveRequest(project="a", type=bad_type, title="t", body="b")

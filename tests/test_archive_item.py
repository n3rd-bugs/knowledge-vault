"""items.archive_item — owner one-off archive (Invariant 5 status flip).

A reversible flip active→archived, never a delete; idempotent on non-active
items; audit stamp in meta; NotFound on a missing id. This is the primitive
behind `kb archive`, for retiring test fixtures / vendor-scan noise that the
date-based nightly archive_stale never reaches.
"""

from __future__ import annotations

import pytest

from kbcore import db, items
from kbcore.errors import NotFound
from kbcore.ingest import ingest

pytestmark = pytest.mark.usefixtures("kb")


def _status(item_id: int) -> str:
    with db.connect() as conn:
        return conn.execute("SELECT status FROM items WHERE id = %s", (item_id,)).fetchone()[0]


def test_archive_flips_active_to_archived_with_audit():
    iid = ingest("proj-x", "note", "throwaway", "test fixture body", kind="chat", uri="arc:1")
    assert items.archive_item(iid, reason="tier-1 test fixture") is True
    assert _status(iid) == "archived"
    detail = items.get_item(iid)
    assert detail.meta["archived"]["reason"] == "tier-1 test fixture"
    assert detail.meta["archived"]["at"]  # ISO timestamp recorded
    # body preserved — a flip, not a delete
    assert detail.body == "test fixture body"


def test_archive_is_idempotent_on_non_active():
    iid = ingest("proj-x", "note", "t", "b", kind="chat", uri="arc:2")
    assert items.archive_item(iid, reason="first") is True
    assert items.archive_item(iid, reason="second") is False  # already archived → no churn
    detail = items.get_item(iid)
    assert detail.meta["archived"]["reason"] == "first", "the first audit stamp is not overwritten"


def test_archive_missing_id_raises_notfound():
    with pytest.raises(NotFound):
        items.archive_item(999999, reason="nope")


def test_archived_item_leaves_default_search():
    from kbcore.search import search

    iid = ingest(
        "proj-x", "gotcha", "zzarchivetoken gotcha", "body zzarchivetoken", kind="chat", uri="arc:3"
    )
    assert any(h.item_id == iid for h in search("zzarchivetoken")), "active item is searchable"
    items.archive_item(iid, reason="cleanup")
    assert not any(h.item_id == iid for h in search("zzarchivetoken")), (
        "archived item drops from default search"
    )

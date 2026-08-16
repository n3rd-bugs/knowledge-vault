"""Collapse observability on the MCP surface (review #4 P2, KB 21332).

kb_search's rendering appends '(+N folded: ids)' to a hit that folded
cross-project 'alternative_to' twins (SearchHit.collapsed, KB 19663) — and is
BYTE-IDENTICAL to kbcore.render.render_hits whenever nothing folded, so the
provenance contract every consumer parses is untouched. No DB required.
"""

from __future__ import annotations

from datetime import UTC, datetime

from kbcore import render
from kbcore.models import SearchHit
from kbmcp.server import _render_hits_with_folds


def _hit(item_id: int, collapsed: list[int] | None = None) -> SearchHit:
    return SearchHit(
        item_id=item_id,
        title=f"title {item_id}",
        type="note",
        project="p1",
        status="active",
        trust="verified",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        snippet="a snippet",
        score=1.0,
        collapsed=collapsed or [],
    )


def test_a_folding_hit_names_its_folded_twins():
    out = _render_hits_with_folds([_hit(1, [7, 9]), _hit(2)])
    assert "(+2 folded: 7, 9)" in out
    first, second = out.split("\n\n")
    assert "folded" in first
    assert "folded" not in second, "a non-folding hit renders exactly as before"


def test_single_fold_marker():
    out = _render_hits_with_folds([_hit(4, [42])])
    assert "(+1 folded: 42)" in out


def test_no_hits_still_says_no_results():
    assert _render_hits_with_folds([]) == render.render_hits([])


def test_without_folds_the_rendering_is_byte_identical_to_render_hits():
    hits = [_hit(1), _hit(2), _hit(3)]
    assert _render_hits_with_folds(hits) == render.render_hits(hits)

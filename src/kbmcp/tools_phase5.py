"""kbmcp.tools_phase5 — trace / similar / review / stats MCP tools (PLAN.md §6).

Registered via :func:`register` on the server's ``mcp`` instance. It takes the
instance as an argument (rather than importing it) so that ``python -m
kbmcp.server`` — which runs the server module as ``__main__`` — registers these
tools on the SAME FastMCP object the server actually serves, not a second copy.
"""

from __future__ import annotations

import logging

from fastmcp.exceptions import ToolError

from kbcore import graph, items, render, stats
from kbcore import review as review_mod
from kbcore.errors import KBError

log = logging.getLogger("kbmcp")


def _wrap(err: Exception) -> ToolError:
    log.exception("tool error")
    return ToolError(str(err))


def register(mcp) -> None:
    """Register the Phase 5 tools on the given FastMCP instance."""

    @mcp.tool
    def kb_trace(node_id: int, node_kind: str = "item", depth: int = 2) -> str:
        """Trace how a decision/bug/entity connects: supersession chains, fixes,
        derived_from, alternatives, and entity mentions, as an indented tree.

        node_kind is 'item' (default) or 'entity'. depth is hops out (default 2).
        Use it to answer "why is this the way it is?" and "what did this replace?"."""
        try:
            tree = graph.trace(node_kind, node_id, depth=depth)
            log.info("kb_trace %s %s depth=%s", node_kind, node_id, depth)
            return render.render_trace(tree)
        except KBError as e:
            raise _wrap(e) from e

    @mcp.tool
    def kb_similar(item_id: int, k: int = 5) -> str:
        """Nearest active items to this one by meaning — "have we hit this before?".

        Great on a bug: it surfaces the likely fix and prior occurrences. Returns
        the top-k with cosine scores."""
        try:
            hits = graph.similar(item_id, k=k)
            items.log_access(item_id, "mcp.kb_similar")
            log.info("kb_similar id=%s k=%s hits=%d", item_id, k, len(hits))
            return render.render_similar(hits)
        except KBError as e:
            raise _wrap(e) from e

    @mcp.tool
    def kb_review_list() -> str:
        """List open review-queue entries (unverified extractions + contradictions)."""
        try:
            return render.render_review(review_mod.list_open_review())
        except KBError as e:
            raise _wrap(e) from e

    @mcp.tool
    def kb_review_resolve(queue_id: int, action: str, new_item_id: int | None = None) -> str:
        """Resolve one review-queue row. action='promote' (trust→verified),
        'reject' (archive the item), or 'supersede' (retire the flagged item with
        new_item_id — for contradictions)."""
        try:
            if action == "promote":
                return f"promoted item {review_mod.promote(queue_id)} to verified"
            if action == "reject":
                return f"archived item {review_mod.reject(queue_id)}"
            if action == "supersede":
                if new_item_id is None:
                    raise ValueError("action='supersede' requires new_item_id")
                old = review_mod.resolve_via_supersede(queue_id, new_item_id)
                return f"item {old} superseded by {new_item_id}"
            raise ValueError(f"unknown action {action!r}; one of promote|reject|supersede")
        except (KBError, ValueError) as e:
            raise _wrap(e) from e

    @mcp.tool
    def kb_stats() -> str:
        """Corpus health: counts by type/project/trust/status, entities, relations,
        open review queue, embed model, and last consolidation date."""
        try:
            return render.render_stats(stats.stats())
        except KBError as e:
            raise _wrap(e) from e

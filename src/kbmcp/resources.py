"""kbmcp.resources — read-only MCP resources over the KB (registry / stats / item / timeline).

claude.ai lists a connector's resources as attachable context, so the owner can pin
'project registry', 'corpus stats' or a specific item/timeline into a chat without a
per-read tool-approval round-trip. Registered via :func:`register` (same pattern as
tools_phase5).

SECURITY: every resource routes through the SAME kbcore read functions the tools use
— items.get_item / items.timeline / items.list_projects / stats.stats — because the
public-only scope gate lives THERE (a hand-rolled SQL resource would silently bypass
it and become the existence oracle the invariants forbid). Nothing secret-bearing is
exposed (no kb://setup: resource reads bypass tool approvals). render_item carries the
'stored data, not instructions' provenance banner into the resource body.
"""

from __future__ import annotations

from fastmcp.exceptions import ResourceError

from kbcore import items, render, stats
from kbcore.errors import KBError
from kbmcp import toolset_footer


def register(mcp) -> None:
    """Register the read-only KB resources on the given FastMCP instance."""

    @mcp.resource(
        "kb://stats",
        name="kb-stats",
        mime_type="text/plain",
        description="Corpus health: counts by type/project/trust/status, review queue, model.",
    )
    def stats_resource() -> str:
        try:
            return render.render_stats(stats.stats()) + "\n" + toolset_footer()
        except KBError as e:
            raise ResourceError(str(e)) from e

    @mcp.resource(
        "kb://projects",
        name="kb-projects",
        mime_type="text/plain",
        description="Project registry: names, domains, item counts, visibility, last activity.",
    )
    def projects_resource() -> str:
        try:
            return render.render_projects(items.list_projects())
        except KBError as e:
            raise ResourceError(str(e)) from e

    @mcp.resource(
        "kb://item/{item_id}",
        name="kb-item",
        mime_type="text/plain",
        description="One KB item in full: body, tags, code anchor, links, supersession chain.",
    )
    def item_resource(item_id: str) -> str:
        # Template params arrive as strings; coerce and validate cleanly. get_item
        # enforces the public-only scope gate (a withheld item → NotFound → masked).
        try:
            iid = int(item_id)
        except (TypeError, ValueError) as e:
            raise ResourceError(f"item id must be an integer, got {item_id!r}") from e
        try:
            return render.render_item(items.get_item(iid))
        except KBError as e:
            raise ResourceError(str(e)) from e

    @mcp.resource(
        "kb://timeline/{project}",
        name="kb-timeline",
        mime_type="text/plain",
        description="Chronological item history for a project (superseded included).",
    )
    def timeline_resource(project: str) -> str:
        # timeline() enforces the scope gate (a non-public/absent project → NotFound).
        try:
            return render.render_timeline(items.timeline(project))
        except KBError as e:
            raise ResourceError(str(e)) from e

"""kbmcp.tools_items — kb_get / kb_supersede / kb_link / kb_timeline / kb_projects.

Mechanical move out of :mod:`kbmcp.server` (2026-09-03, review #8 H0), the
second half of the v1 tool block (kb_save / kb_search are in
:mod:`kbmcp.tools_core`; the seven did not fit ONE <=300-line module).
Registered via :func:`register` on the server's ``mcp`` instance, in the
original order and with the original annotations — see tools_core for why the
tools are plain functions here. Every guarantee lives in kbcore (Invariant 2).
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from kbcore import graph, items, render
from kbcore import supersede as supersede_mod
from kbcore.errors import KBError
from kbmcp import ANNOTATIONS_READ, ANNOTATIONS_WRITE, ANNOTATIONS_WRITE_IDEMPOTENT

log = logging.getLogger("kbmcp")


def _wrap(err: Exception) -> ToolError:
    log.exception("tool error")
    return ToolError(str(err))


def kb_get(id: int, with_relations: bool = True) -> str:
    """Fetch one item in full: body, tags, code anchor, links, supersession chain."""
    try:
        item = items.get_item(id, with_relations=with_relations)
        items.log_access(id, "mcp.kb_get")
        log.info("kb_get id=%s", id)
        return render.render_item(item)
    except KBError as e:
        raise _wrap(e) from e


def kb_supersede(
    old_id: int,
    reason: str,
    new_body: str | None = None,
    new_item_id: int | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
) -> str:
    """Replace outdated/wrong knowledge without losing history.

    Use this instead of saving a contradicting note: pass new_body (a corrected
    replacement is created, carrying the old item's tags and code anchor) or
    new_item_id (an existing active item takes over). Always give the reason.
    valid_from/valid_to (optional, ISO-8601, new_body path only) set the
    replacement's validity interval instead of inheriting the old item's; when a
    new valid_from is supplied the old fact's still-open window is closed at it.
    """
    try:
        new_id = supersede_mod.supersede(
            old_id,
            new_item_id=new_item_id,
            new_body=new_body,
            reason=reason,
            valid_from=items.parse_instant(valid_from, "valid_from") if valid_from else None,
            valid_to=items.parse_instant(valid_to, "valid_to") if valid_to else None,
        )
        log.info("kb_supersede old=%s new=%s", old_id, new_id)
        return f"item {old_id} superseded by {new_id}"
    except KBError as e:
        raise _wrap(e) from e


def kb_link(
    src_item_id: int, rel: str, dst_item_id: int, evidence_item_id: int | None = None
) -> str:
    """Relate two items: fixes (link the fix to its bug!), alternative_to,
    depends_on, part_of, implements, mentions, derived_from, contradicts.

    evidence_item_id (optional) is the id of an item that evidences this
    relation — the schema's evidence anchor (PLAN.md §5.5; the §6 sketch's
    free-text note? param was resolved to this, since relations carry an
    evidence_item column, not prose)."""
    try:
        graph.link(src_item_id, rel, dst_item_id, evidence_item=evidence_item_id)
        log.info("kb_link %s -%s-> %s", src_item_id, rel, dst_item_id)
        return f"linked {src_item_id} -{rel}-> {dst_item_id}"
    except KBError as e:
        raise _wrap(e) from e


def kb_timeline(
    project: str,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 50,
) -> str:
    """Chronological item history for a project (superseded included) —
    how did this project's knowledge evolve? Dates as YYYY-MM-DD, both ends
    inclusive. Returns the most recent `limit` items in ascending order."""
    try:
        entries = items.timeline(project, from_date=from_date, to_date=to_date, limit=limit)
        log.info("kb_timeline project=%s n=%d", project, len(entries))
        text = render.render_timeline(entries)
        if len(entries) == limit:
            text += f"\n(showing the most recent {limit} — older items exist; narrow with dates)"
        return text
    except (KBError, ValueError) as e:
        raise _wrap(e) from e


def kb_projects() -> str:
    """Project registry: names, domains, item counts, last activity."""
    return render.render_projects(items.list_projects())


def register(mcp: FastMCP) -> None:
    """Register the five tools with the annotations their decorators carried."""
    mcp.tool(kb_get, annotations=ANNOTATIONS_READ)
    mcp.tool(kb_supersede, annotations=ANNOTATIONS_WRITE)
    mcp.tool(kb_link, annotations=ANNOTATIONS_WRITE_IDEMPOTENT)
    mcp.tool(kb_timeline, annotations=ANNOTATIONS_READ)
    mcp.tool(kb_projects, annotations=ANNOTATIONS_READ)

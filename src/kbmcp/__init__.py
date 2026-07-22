"""kbmcp — FastMCP server exposing kbcore over authenticated HTTP (Phase 2, PLAN.md §6)."""

from __future__ import annotations

from typing import Any

# --- Toolset schema version (schema-version signalling) -----------------------
# claude.ai caches tools/list per connector SESSION, so a param added mid-session
# (e.g. the expand tri-state, valid_from/valid_to) is invisible until the client
# reconnects. We can't force a refetch in json_response mode, but we CAN make the
# drift observable: this string is surfaced in the FastMCP serverInfo.version, the
# server instructions, and kb_stats output, so a long-lived session can notice its
# tools predate the server and reconnect. BUMP whenever a tool SCHEMA changes
# (tool added/removed, or a param added/removed/retyped). Date form stays sortable.
KB_TOOLSET_VERSION = "2026-07-22"


def toolset_footer() -> str:
    """One-line schema-version marker appended to kb_stats / kb://stats."""
    return (
        f"toolset-version: {KB_TOOLSET_VERSION} "
        "(reconnect your connector if this is newer than the tools your client shows)"
    )


# --- Tool annotations (MCP readOnlyHint/destructiveHint/…) --------------------
# Hints only — they NEVER relax a server-side check; their sole value is client UX
# (claude.ai gives read-only tools lighter approval friction). destructiveHint is
# false everywhere: automation never deletes, it flips status (CLAUDE.md inv. 5).
# openWorldHint is false everywhere: the KB is a closed corpus, not an open-world
# fetch. Shared presets so all four tool modules annotate consistently.
ANNOTATIONS_READ: dict[str, Any] = {
    "readOnlyHint": True,
    "idempotentHint": True,
    "destructiveHint": False,
    "openWorldHint": False,
}
# Writes that CONVERGE on re-call (dedupe / upsert): kb_save, kb_ingest_session,
# kb_link, kb_consolidate — re-submitting identical args is a no-op.
ANNOTATIONS_WRITE_IDEMPOTENT: dict[str, Any] = {
    "readOnlyHint": False,
    "idempotentHint": True,
    "destructiveHint": False,
    "openWorldHint": False,
}
# Writes whose repeat is NOT a clean no-op: kb_supersede / kb_review_resolve (a
# second identical call errors on the already-changed state), kb_deep_search
# (each run mints a new report), kb_setup (deliberately NOT read-only — it can
# return the master bearer, so it keeps full approval friction).
ANNOTATIONS_WRITE: dict[str, Any] = {
    "readOnlyHint": False,
    "idempotentHint": False,
    "destructiveHint": False,
    "openWorldHint": False,
}

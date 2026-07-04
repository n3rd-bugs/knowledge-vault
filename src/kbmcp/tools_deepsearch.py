"""kbmcp.tools_deepsearch — the kb_deep_search MCP tool (explicit GPU run).

Same registration pattern as tools_phase5: read tool, rendered-string result,
domain errors wrapped into clean ToolErrors. The underlying run is SLOW (the
local 14B, typically 1-5 minutes) — FastMCP executes sync tools in a worker
thread, so the server keeps serving other requests meanwhile.
"""

from __future__ import annotations

import logging

from fastmcp.exceptions import ToolError

from kbcore import render
from kbcore.deepsearch import deep_search
from kbcore.errors import KBError

log = logging.getLogger("kbmcp.deepsearch")


def _wrap(err: Exception) -> ToolError:
    log.exception("tool error")
    return ToolError(str(err))


def register(mcp) -> None:
    """Register the deep-search tool on the given FastMCP instance."""

    @mcp.tool
    def kb_deep_search(question: str, project: str | None = None, save: bool = True) -> str:
        """Deep search: plans 3-6 sub-queries, retrieves across the whole KB,
        and synthesizes a cited answer on the LOCAL model. SLOW — typically
        1-5 minutes; use it to dig out architectural details that one
        kb_search cannot assemble. The report is saved to the KB
        (type='report', unverified) BEFORE this returns, so if your client
        times out you can still retrieve it: kb_search(question,
        type='report') or kb_timeline(project='kb-system').
        """
        try:
            result = deep_search(question, project=project, save=save)
            log.info(
                "kb_deep_search q=%r sources=%d saved=%s",
                question[:60],
                len(result.sources),
                result.item_id,
            )
            return render.render_deep_search(result)
        except KBError as e:
            raise _wrap(e) from e

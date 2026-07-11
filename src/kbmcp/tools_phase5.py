"""kbmcp.tools_phase5 — trace / similar / review / stats MCP tools (PLAN.md §6).

Registered via :func:`register` on the server's ``mcp`` instance. It takes the
instance as an argument (rather than importing it) so that ``python -m
kbmcp.server`` — which runs the server module as ``__main__`` — registers these
tools on the SAME FastMCP object the server actually serves, not a second copy.
"""

from __future__ import annotations

import logging

from fastmcp.exceptions import ToolError

from kbcore import compress, graph, ingest_queue, items, render, stats
from kbcore import review as review_mod
from kbcore.errors import KBError
from kbmcp import (
    ANNOTATIONS_READ,
    ANNOTATIONS_WRITE,
    ANNOTATIONS_WRITE_IDEMPOTENT,
    toolset_footer,
)

log = logging.getLogger("kbmcp")

_REVIEW_LIST_MAX = 100
_REVIEW_LIST_DEFAULT = 25


def _wrap(err: Exception) -> ToolError:
    log.exception("tool error")
    return ToolError(str(err))


def register(mcp) -> None:
    """Register the Phase 5 tools on the given FastMCP instance."""

    @mcp.tool(annotations=ANNOTATIONS_READ)
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

    @mcp.tool(annotations=ANNOTATIONS_READ)
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

    @mcp.tool(annotations=ANNOTATIONS_READ)
    def kb_review_list(limit: int = _REVIEW_LIST_DEFAULT) -> str:
        """List open review-queue entries (unverified extractions + contradictions).

        limit caps how many rows are returned (default 25, max 100), oldest first,
        so a big nightly extraction can't blow the connector's context budget. When
        the queue is longer than the limit, a '(showing N of M open)' footer is
        appended so you know to page with a larger limit or resolve some first."""
        try:
            lim = max(1, min(int(limit), _REVIEW_LIST_MAX))
            entries = review_mod.list_open_review(limit=lim)
            text = render.render_review(entries)
            if len(entries) == lim:
                total = review_mod.count_open_review()
                if total > lim:
                    text += f"\n(showing {lim} of {total} open — raise limit or resolve some)"
            return text
        except (KBError, ValueError) as e:
            raise _wrap(e) from e

    @mcp.tool(annotations=ANNOTATIONS_WRITE)
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

    @mcp.tool(annotations=ANNOTATIONS_READ)
    def kb_stats() -> str:
        """Corpus health: counts by type/project/trust/status, entities, relations,
        open review queue, embed model, last consolidation date, and the toolset
        schema-version (reconnect the connector if yours is older)."""
        try:
            # Append the toolset schema-version so a long-lived connector session can
            # notice its cached tools/list predates the server and reconnect.
            return render.render_stats(stats.stats()) + "\n" + toolset_footer()
        except KBError as e:
            raise _wrap(e) from e

    @mcp.tool(annotations=ANNOTATIONS_WRITE_IDEMPOTENT)
    def kb_ingest_session(
        project: str,
        transcript: str | None = None,
        transcript_gz: str | None = None,
        kind: str = "session",
        repo: str | None = None,
        branch: str | None = None,
        commit: str | None = None,
    ) -> str:
        """Submit a full conversation transcript OR a memory dump for the KB to
        learn from. It is scrubbed and stored NOW (no GPU); the grounded 14B
        extraction runs in the nightly window and results land as unverified items
        you can review. kind is 'session' (a .jsonl or plain transcript) or
        'memory' (a free-text memory dump). For LARGE transcripts pass
        transcript_gz = base64(gzip(text)) instead of transcript (~10x smaller
        on the wire). Pass repo/branch/commit to anchor the knowledge to code.
        Re-submitting identical content is a no-op."""
        try:
            text = compress.resolve_transcript(transcript, transcript_gz)
            res = ingest_queue.enqueue(
                project, text, kind=kind, repo=repo, branch=branch, commit=commit
            )
            if res.id is None:
                return "duplicate — already queued"
            if res.permanently_failed:
                # The row exhausted both its retry budget AND its re-arm budget, so a
                # re-submit will NOT re-queue it — a human must inspect the queue row.
                # Say so plainly rather than implying a resubmit might help.
                return (
                    f"could not be extracted after repeated retries and re-submissions — "
                    f"this content needs manual review (queue id {res.id}); re-submitting the "
                    f"same content will not re-queue it. Ask a KB maintainer to inspect the row."
                )
            if res.requeued:
                return f"re-queued after prior failure for nightly extraction (id {res.id})"
            return f"queued for nightly extraction (id {res.id})"
        except (KBError, ValueError) as e:
            raise _wrap(e) from e

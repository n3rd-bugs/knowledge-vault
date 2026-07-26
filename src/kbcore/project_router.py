"""kbcore.project_router — file KB-tooling knowledge under kb-system (audit 2026-07-26).

``flush-session-to-kb.sh`` derives a session's project from ``basename $CWD``, so
knowledge ABOUT the Knowledge Vault's own tooling gets filed under whatever repo
the session happened to run in. The code-grounded audit found ONE fact about the
flush hook scattered across NINE projects, which defeats every project-scoped
search and lets the same tooling gotcha be re-learned indefinitely.

:func:`route_project` re-files a SINGLE ITEM (never the whole session — a session
in ``sungrow`` legitimately yields both hook knowledge and domain knowledge) when
its code anchor, title, or body references the KB's own tooling surface. The
caller stamps ``meta.rerouted_from`` with the original project so the reroute is
auditable and reversible. Pure string matching — no LLM, no DB.
"""

from __future__ import annotations

from typing import Any

# Reserved home for KB-system knowledge (kbcore.ingest_queue.RESERVED_PROJECTS).
# The LOCAL ingest path (kbcore.ingest.ingest_in) imposes no reserved-name check,
# so a locally-extracted item may target it directly; only the REMOTE submission
# path (ingest_queue.enqueue) gates the name, and the trusted local flush already
# passes allow_reserved=True.
KB_TOOLING_PROJECT = "kb-system"

# Surfaces that make an item KB-tooling knowledge. Session-hook assets first
# (the audited scatter), then the KB repo's own package paths.
TOOLING_MARKERS: tuple[str, ...] = (
    "~/.claude/hooks",
    "kb-session-flush",
    "flush-session-to-kb.sh",
    "kb-session-brief.sh",
    "kb-mcp-call.sh",
    "kb-flush.env",
    "kb_setup",
    "src/kbcore",
    "src/kbmcp",
    "src/kbweb",
    "src/kbworker",
)


def tooling_marker(*parts: str | None) -> str | None:
    """The first tooling marker present in ``parts`` (case-insensitive), or None."""
    haystack = " ".join(p for p in parts if p).lower()
    if not haystack:
        return None
    for marker in TOOLING_MARKERS:
        if marker in haystack:
            return marker
    return None


def route_project(
    project: str,
    *,
    title: str,
    body: str,
    code_ref: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    """``(project, rerouted_from)`` for one extracted item.

    Returns the ORIGINAL project and ``None`` unless the item is about the KB
    tooling, in which case it returns ``(KB_TOOLING_PROJECT, original)``. An item
    already filed under ``kb-system`` is never touched (nothing to reroute).
    """
    if project == KB_TOOLING_PROJECT:
        return project, None
    ref_path = str((code_ref or {}).get("path") or "")
    if tooling_marker(ref_path, title, body) is None:
        return project, None
    return KB_TOOLING_PROJECT, project

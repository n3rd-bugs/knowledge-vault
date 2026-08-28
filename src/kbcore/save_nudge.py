"""kbcore.save_nudge — typed-edge capture nudge on kb_save (Wave G, review #6).

The graph's typed edges are starved: graph-expansion retrieval shipped but has
almost nothing to expand (fixes-coverage of post-ship bug items ~1.1%). The
cheapest intervention is at the moment of CAPTURE: when an agent saves a bug or
gotcha — or a zero-link save whose title reads like a fix — the save RESPONSE
gains up to :data:`NUDGE_CANDIDATES` similar ACTIVE bug items from the SAME
project plus a one-line ``kb_link rel=fixes`` hint. The agent (or owner)
decides; nothing here ever writes a relation (Invariant 4 untouched — this is
not even a review-queue proposal, only response text).

Rules of the road:

* NEVER on a public-only instance (:func:`kbcore.scope.public_only`): the
  suffix would be a similarity oracle on a surface that deliberately masks
  existence. On the full instance the probe is additionally walled by
  ``scope.project_predicate_named`` and pinned to the project the caller just
  WROTE (writable implies readable), so a grantee learns nothing new.
* ONE CHEAP PROBE: lead-chunk cosine against the item's OWN stored embedding
  (the :func:`kbcore.graph.similar` mechanism narrowed to same-project active
  bugs) — no Ollama call, no extra embed (Invariant 8 untouched).
* BEST-EFFORT: any failure returns ``""`` — a nudge must never fail a save.
* Weekly typed-edge delta (the review #6 target: >=20 new
  fixes/depends_on/implements edges per week; kill rule at <5 after 2 weeks):
  :func:`typed_edge_counts` is the CHEAP QUERY the weekly's checkpoint-diff
  pattern adopts — a query, deliberately not a nightly counts key (the nightly
  never touches this module).
"""

from __future__ import annotations

import logging
import re

from kbcore import scope
from kbcore.config import settings
from kbcore.db import connect

logger = logging.getLogger("kbcore.save_nudge")

#: Item types whose save always probes (a gotcha is often the fix knowledge).
NUDGE_TYPES = ("bug", "gotcha")
#: The typed edges the weekly delta tracks (the starved, retrieval-bearing rels).
TYPED_EDGE_RELS = ("fixes", "depends_on", "implements")
#: Response-suffix candidate cap — a nudge, not a search result page.
NUDGE_CANDIDATES = 3
#: "Fix-shaped" title heuristic for zero-link saves of any other type.
FIX_TITLE_RE = re.compile(r"\b(fix(es|ed|ing)?|resolve[sd]?|workaround|patch(es|ed)?)\b", re.I)


def suffix(project: str, item_type: str, title: str, item_id: int, has_links: bool) -> str:
    """The nudge text to append to a kb_save response, or ``""``.

    Fires for ``item_type`` in :data:`NUDGE_TYPES`, and for any ZERO-link save
    whose title matches :data:`FIX_TITLE_RE` (``has_links`` is whether the save
    carried explicit link specs). Skips entirely under public-only scope (no
    oracle). Best-effort: a failed probe logs and returns ``""`` — the save
    already succeeded and its response must say so.
    """
    if scope.public_only():
        return ""
    if item_type not in NUDGE_TYPES and (has_links or not FIX_TITLE_RE.search(title or "")):
        return ""
    try:
        candidates = _similar_active_bugs(project, int(item_id))
    except Exception:  # noqa: BLE001 — a nudge must never fail (or fail after) a save
        logger.warning("save-nudge probe failed for item %s", item_id, exc_info=True)
        return ""
    if not candidates:
        return ""
    lines = [f"\nsimilar active bug items in {project}:"]
    lines += [f"\n  [{cid}] {ctitle}" for cid, ctitle in candidates]
    lines.append(f"\nif this fixes one: kb_link src_item_id={item_id} rel=fixes dst_item_id=<id>")
    return "".join(lines)


def _similar_active_bugs(
    project: str, item_id: int, k: int = NUDGE_CANDIDATES
) -> list[tuple[int, str]]:
    """Nearest same-project ACTIVE ``bug`` items to ``item_id`` by lead-chunk cosine.

    Reuses the embedding ingest just stored — zero model calls. An item with no
    lead embedding (or a stale embed_model) legitimately nudges nothing.
    """
    with connect() as conn:
        base = conn.execute(
            "SELECT embedding::text FROM chunks WHERE item_id = %s AND ord = 0 "
            "AND embed_model = %s AND embedding IS NOT NULL",
            (item_id, settings.kb_embed_model),
        ).fetchone()
        if base is None or base[0] is None:
            return []
        vis_frag, vis_named = scope.project_predicate_named("i.project")
        vis_where = f"AND {vis_frag}" if vis_frag else ""
        rows = conn.execute(
            f"""
            SELECT i.id, i.title
            FROM chunks c JOIN items i ON i.id = c.item_id
            WHERE c.ord = 0 AND c.embed_model = %(model)s AND c.embedding IS NOT NULL
              AND i.status = 'active' AND i.type = 'bug'
              AND i.project = %(project)s AND i.id <> %(self)s {vis_where}
            ORDER BY c.embedding <=> %(v)s::vector
            LIMIT %(k)s
            """,  # noqa: S608 — vis_where is a constant fragment; values ride params
            {
                "v": base[0],
                "model": settings.kb_embed_model,
                "project": project,
                "self": item_id,
                "k": max(1, int(k)),
                **vis_named,
            },
        ).fetchall()
    return [(int(r[0]), r[1]) for r in rows]


def typed_edge_counts() -> dict[str, int]:
    """Current item-to-item totals per typed edge — the weekly delta's read side.

    One grouped scan of ``relations``, zero-filled so the shape is stable
    (``{fixes: N, depends_on: N, implements: N}``). RECORDED CONTRACT (Wave G):
    the weekly typed-edge delta is computed by snapshotting this QUERY through
    the weekly's existing checkpoint-diff pattern — there is no nightly counts
    key for it, so nothing here runs unattended.
    """
    out = dict.fromkeys(TYPED_EDGE_RELS, 0)
    with connect() as conn:
        rows = conn.execute(
            "SELECT rel, count(*) FROM relations WHERE rel = ANY(%s) "
            "AND src_kind = 'item' AND dst_kind = 'item' GROUP BY rel",
            (list(TYPED_EDGE_RELS),),
        ).fetchall()
    for rel, n in rows:
        out[rel] = int(n)
    return out

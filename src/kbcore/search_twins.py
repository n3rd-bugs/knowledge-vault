"""kbcore.search_twins — the collapse fold's LINKER-TWIN discriminator (E1, KB 31855).

Why (KB 31855, review #8 KB 31857): ``alternative_to`` carries two meanings in
the graph. The cross-project twin linker (kbcore.twin_probe, 2026-08-14) wrote
32 edges between near-verbatim duplicates — ord-0 cosine 0.935-0.999 — and an
owner writes MANUAL edges between genuine ADR alternatives (297->294 at ord-0
cosine 0.374; 302->294 0.667; 374->373 0.677; 37->38 0.829). The rank-time fold
(search_rank.collapse_alternatives) folded EVERY cross-project pair, so the
297/294 golden case rendered one ADR alternative and HID the other — a correct
manual edge, wrongly read as "one piece of knowledge captured twice". Relations
has no meta column and E1 allows no migration, so the discriminator is a
rank-time PREDICATE rather than a stored ``twin_of`` label.

Basis — the linker's own definition, no new knob: a pair is a twin iff both
items' ord=0 chunks exist, are embedded on the CURRENT ``settings.kb_embed_model``
with a non-NULL embedding, and ``1 - (a <=> b)`` is STRICTLY ABOVE
``settings.kb_xtwin_threshold`` (mirrors ``twin_probe._KNN_SQL`` and its
``sim <= thr: continue``). ``kb_xtwin_threshold`` stays the single source of
truth shared with the linker: the 32 linker edges keep folding, the four manual
ADR edges do not.

Render consequence when embeddings lag a model swap: a pair whose lead chunks
are missing, still on the previous model, or NULL is NOT provably a twin and is
NOT folded — both items render (honest default: a duplicate shown twice is
harmless, a hidden ADR alternative is not). The fold returns on its own once the
re-embed catches up; nothing is written, nothing is flagged (Invariant 5).
"""

from __future__ import annotations

from kbcore.config import settings

_TWIN_SQL = """
SELECT p.a, p.b
FROM unnest(%(a)s::bigint[], %(b)s::bigint[]) AS p(a, b)
JOIN chunks ca ON ca.item_id = p.a AND ca.ord = 0
              AND ca.embed_model = %(model)s AND ca.embedding IS NOT NULL
JOIN chunks cb ON cb.item_id = p.b AND cb.ord = 0
              AND cb.embed_model = %(model)s AND cb.embedding IS NOT NULL
WHERE (1 - (ca.embedding <=> cb.embedding)) > %(thr)s
"""


def linker_twin_pairs(conn, pairs: list[tuple[int, int]]) -> set[tuple[int, int]]:
    """Subset of ``pairs`` that are linker twins by kbcore.twin_probe's definition.

    One parameterised query for the whole set (the pair arrays are unnested
    side by side). A pair drops out when either lead chunk is absent, on
    another ``embed_model``, NULL, or at/below ``kb_xtwin_threshold`` — see the
    module docstring for why an unprovable fold renders both items. Empty input
    returns an empty set without touching the DB.
    """
    if not pairs:
        return set()
    rows = conn.execute(
        _TWIN_SQL,
        {
            "a": [a for a, _b in pairs],
            "b": [b for _a, b in pairs],
            "model": settings.kb_embed_model,
            "thr": settings.kb_xtwin_threshold,
        },
    ).fetchall()
    return {(int(row[0]), int(row[1])) for row in rows}

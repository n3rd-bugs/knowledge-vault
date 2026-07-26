"""kbcore.tag_hygiene — strip bled-through domain tags (audit 2026-07-26).

Extracted tags are passed through VERBATIM from the model, so a session's
dominant domain vocabulary sticks to unrelated items. Audited instances:
``framework:pymodbus`` + ``hw:sungrow`` on a pure-bash hook runbook, and
``hw:winet`` on a hook item that consolidation then welded into a HARDWARE
summary — the tag alone is enough to drag an item into the wrong topic
summary, because kbcore.consolidation summarizes BY TAG.

:func:`filter_bled_tags` drops a tag in the ``hw:`` / ``framework:`` namespace
whose value token does not literally appear in the item's own title+body.
Those two namespaces name a CONCRETE artifact (a part number, a library), so a
legitimate one is essentially always mentioned. ``lang:`` / ``tool:`` /
``protocol:`` / ``domain:`` / ``os:`` are left untouched — they are frequently
correct without a literal mention ("lang:bash" on a script that never says
"bash"). Dropped tags are returned for ``meta.dropped_tags`` (audit, not
deletion — the KB never silently loses information).
"""

from __future__ import annotations

import re

# Namespaces whose value names a concrete artifact and must therefore be
# mentioned by the item itself.
BLEED_NAMESPACES: frozenset[str] = frozenset({"hw", "framework"})

_STRIP = re.compile(r"[-_]+")


def _norm(text: str) -> str:
    """Case-fold and drop hyphens/underscores (``hw:sun-grow`` ≈ "sungrow")."""
    return _STRIP.sub("", text).lower()


def filter_bled_tags(tags: list[str], title: str, body: str) -> tuple[list[str], list[str]]:
    """``(kept, dropped)`` for one extracted item's tags. Order is preserved.

    A ``hw:``/``framework:`` tag survives only when its value token appears
    (case-insensitively, ignoring hyphens/underscores) in ``title + body``.
    """
    haystack = _norm(f"{title}\n{body}")
    kept: list[str] = []
    dropped: list[str] = []
    for tag in tags:
        ns, sep, value = tag.partition(":")
        if sep and ns.strip().lower() in BLEED_NAMESPACES:
            token = _norm(value)
            if token and token not in haystack:
                dropped.append(tag)
                continue
        kept.append(tag)
    return kept, dropped

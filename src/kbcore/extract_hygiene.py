"""kbcore.extract_hygiene — the ONE post-extraction hygiene chokepoint (audit 2026-07-26).

The code-grounded audit of 30 machine-extracted items found four defects that all
live between "the model returned an item" and "the item is written": the project
is wrong (kbcore.project_router), the tags bled from the session's dominant domain
(kbcore.tag_hygiene), the body cites identifiers that do not exist
(kbcore.ref_check), the item is a session receipt rather than knowledge
(kbcore.session_noise), and the code anchor points at a repo root that verifies
nothing (:func:`sanitize_code_ref`).

Composing them HERE, as one pure function, is what makes them a single
chokepoint: kbworker.sessions runs pipeline v1 and the WP-B2 ensemble through the
same :func:`evaluate` call, so the two paths can never drift — the same property
``gate_quotes`` gives the SEC-3 quote gate.

WHY NOT ``kbcore.ingest.ingest_in``: ingest_in is the write chokepoint for EVERY
surface, including the owner's manual ``kb_save``, the repo doc scanner and the
web contribution form. All five rules above are corrections for MODEL-generated
items — an owner who tags ``hw:tps55288`` on a note that never spells the part
number out, or anchors a decision at a repo root, means it. Applying them at
ingest_in would silently rewrite deliberate human input; applying them here
confines them to the automatic pipeline, which is exactly where the audit found
the damage. Pure functions only: no DB, no LLM, no writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kbcore import project_router, ref_check, session_noise, tag_hygiene

# code_ref.path values that anchor nothing (FIX 6): a session spans a working
# tree, not a file, and "." renders as a verifiable citation while pointing at
# the repo root. 8+ audited items carried exactly this phantom anchor.
PHANTOM_PATHS = frozenset({"", ".", "./", "/"})


@dataclass(frozen=True)
class Hygiene:
    """Verdicts for ONE extracted item (all advisory except ``noise_reason``)."""

    project: str
    tags: list[str] = field(default_factory=list)
    code_ref: dict[str, Any] | None = None
    rerouted_from: str | None = None
    dropped_tags: list[str] = field(default_factory=list)
    unresolved_refs: list[str] = field(default_factory=list)
    noise_reason: str | None = None

    def meta(self) -> dict[str, Any]:
        """Audit keys to stamp on the item's meta (only the non-empty ones)."""
        out: dict[str, Any] = {}
        if self.rerouted_from:
            out["rerouted_from"] = self.rerouted_from
        if self.dropped_tags:
            out["dropped_tags"] = list(self.dropped_tags)
        if self.unresolved_refs:
            out["unresolved_refs"] = list(self.unresolved_refs)
        return out


def sanitize_code_ref(code_ref: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop a code anchor whose path points at nothing (FIX 6), else pass it through.

    Returns None for a repo-root/empty path so no phantom ``↳ repo@commit:.``
    citation is ever stored; a real file anchor round-trips untouched.
    """
    if not code_ref:
        return None
    path = str(code_ref.get("path") or "").strip()
    if path in PHANTOM_PATHS:
        return None
    return code_ref


def evaluate(
    *,
    project: str,
    title: str,
    body: str,
    tags: list[str] | None = None,
    code_ref: dict[str, Any] | None = None,
) -> Hygiene:
    """Run every post-extraction rule over one candidate item. Never writes.

    The caller skips minting when ``noise_reason`` is set (counting the skip in
    the review detail), and otherwise ingests with the returned project / tags /
    code_ref plus :meth:`Hygiene.meta` stamped onto the item meta.
    """
    clean_ref = sanitize_code_ref(code_ref)
    reason = session_noise.noise_reason(title, body)
    routed, rerouted_from = project_router.route_project(
        project, title=title, body=body, code_ref=clean_ref
    )
    kept_tags, dropped = tag_hygiene.filter_bled_tags(list(tags or []), title, body)
    refs = ref_check.unresolved_refs(f"{title}\n{body}", project=routed)
    return Hygiene(
        project=routed,
        tags=kept_tags,
        code_ref=clean_ref,
        rerouted_from=rerouted_from,
        dropped_tags=dropped,
        unresolved_refs=refs,
        noise_reason=reason,
    )

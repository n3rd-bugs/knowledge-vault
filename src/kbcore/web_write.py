"""kbcore.web_write — the composed write surface the web layer imports (A11-3 / A11-3-B).

The ONLY functions kbweb may import for knowledge writes. Every trust / quarantine /
review guarantee is HARDWIRED here at the kbcore chokepoint (Invariant 2), so no
route can reach raw ``ingest()`` / ``supersede()`` and smuggle the review gate off.

A non-owner web save always lands ``trust='unverified'`` + ``status='pending'``
(quarantined from retrieval — A11-3 finding 6) + a ``'web_contribution'``
review_queue row, ATOMICALLY in one transaction; only an OWNER promote
un-quarantines it (kbcore.review.promote flips pending -> active + unverified ->
verified). A rejected contribution is archived (Invariant 5 status flip). The
web NEVER reaches trust='verified' for a non-owner (the WP-1 ingest_in chokepoint
also coerces it), and NEVER reaches promote (owner-only, stays in review.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from kbcore import scope
from kbcore.db import transaction
from kbcore.errors import DuplicateContent, InvalidOperation, NotFound
from kbcore.ingest import ingest_in
from kbcore.models import WEB_ITEM_TYPES, CodeRef
from kbcore.review import queue_review_in

# The web contract, hardwired here (never accepted from a request):
_WEB_KIND = "manual"  # web saves share the (manual, '') dedupe namespace (finding 7)
_WEB_TRUST = "unverified"  # non-owner can never mint 'verified' (also WP-1 chokepoint)
_WEB_STATUS = "pending"  # quarantined from retrieval until owner promote (finding 6)
_WEB_REASON = "web_contribution"  # reviewer-triage label (migration 014 / REVIEW_REASONS)


def _web_detail(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The review-row detail stamped on every web contribution.

    ``origin='web'`` + the resolved principal name so reviewer triage can attribute
    the contribution; ``extra`` carries the proposed-supersession marker when present.
    """
    detail: dict[str, Any] = {"origin": "web", "principal": scope.current().name}
    if extra:
        detail.update(extra)
    return detail


def save_for_review(
    project: str,
    type_: str,
    title: str,
    body: str,
    *,
    tags: list[str] | None = None,
    code_ref: CodeRef | dict | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> tuple[int, int]:
    """Save a WEB contributor's item as quarantined + queued, in ONE transaction.

    kbcore chokepoint (Invariant 2) — A11-3 findings 5/6/7/16. Hardwires the
    contributor-save contract: ``ingest_in(kind='manual', trust='unverified'
    [also enforced by the WP-1 chokepoint], status='pending')`` + a
    ``'web_contribution'`` review row referencing the NEW item, so a crash between
    the two inserts never leaves an unverified item invisible to review. The item is
    ABSENT from default search (finding 6) until the owner promotes it.

    ``type_`` MUST be one of WEB_ITEM_TYPES (ITEM_TYPES minus 'summary'/'report') so
    a web save can never masquerade as consolidation output (finding 7). require_cap
    (CAP_SAVE) + assert_writable(project) are inherited via ingest_in; CAP_SAVE is
    ALSO asserted here as the explicit gate (defense-in-depth — per-principal-constant,
    oracle-safe). Returns ``(item_id, queue_id)``; raises DuplicateContent on a
    content-hash no-op (no orphan queue row is ever created).
    """
    scope.require_cap(scope.CAP_SAVE)
    if type_ not in WEB_ITEM_TYPES:
        raise InvalidOperation(
            f"item type {type_!r} may not be authored from the web; "
            f"one of {', '.join(WEB_ITEM_TYPES)}"
        )
    with transaction() as conn:
        item_id = ingest_in(
            conn,
            project,
            type_,
            title,
            body,
            kind=_WEB_KIND,
            uri="",
            trust=_WEB_TRUST,
            status=_WEB_STATUS,
            tags=tags,
            code_ref=code_ref,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        if item_id is None:
            # Duplicate content re-submitted — ingest_in was a no-op; do NOT enqueue
            # a review row pointing at nothing (finding 16). Rolls back cleanly.
            raise DuplicateContent("duplicate content — nothing saved for review")
        queue_id = queue_review_in(conn, item_id, _WEB_REASON, _web_detail())
    return item_id, queue_id


def supersede_for_review(
    old_id: int,
    *,
    new_body: str,
    reason: str,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> tuple[int, int]:
    """Propose a supersession from the WEB as a quarantined replacement.

    kbcore chokepoint (Invariant 2) — A11-3 findings 5/8/10. The REPLACEMENT item
    lands ``trust='unverified'`` ``status='pending'`` + a ``'web_contribution'``
    review row referencing the REPLACEMENT id (NOT ``old_id`` — review.reject
    archives ``rq.item_id``, so queuing ``old_id`` would leave a rejected forgery
    active; finding 8).

    CHOSEN QUARANTINE SEMANTICS (documented + tested): the OLD item is NOT retired
    here — it STAYS ``active`` until the owner promotes the replacement. Retiring it
    now would let a contributor HIDE a verified item behind an invisible pending one
    (the old row leaves active search while the replacement is still quarantined) —
    a retrieval-suppression primitive this MUST prevent. Instead the proposal is
    recorded in the review row's ``detail.proposed_supersede`` (+ the free-text
    ``supersede_reason``); kbcore.review.promote applies the real supersession only
    once the owner promotes the replacement to active+verified. A rejected proposal
    archives just the replacement, leaving the original untouched and active.

    require_cap(CAP_SAVE) + assert_writable(old item's project) (writability inherited
    via ingest_in; CAP_SAVE asserted explicitly). The old item's type/title/tags/meta
    and valid-time interval are inherited by the replacement (an explicit valid_from/
    valid_to OVERRIDES the inherited interval), mirroring supersede()'s new_body path.
    Returns ``(new_item_id, queue_id)``; raises DuplicateContent on a no-op dup.
    """
    scope.require_cap(scope.CAP_SAVE)
    with transaction() as conn:
        row = conn.execute(
            "SELECT project, type, title, tags, status, meta, valid_from, valid_to "
            "FROM items WHERE id = %s",
            (old_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"item {old_id} does not exist")
        project, type_, title, tags, status, old_meta, old_vf, old_vt = row
        # A withheld old item must be byte-identical to a missing one (no existence /
        # project-name oracle): readability BEFORE writability, mirroring supersede().
        if not scope.project_readable(conn, project):
            raise NotFound(f"item {old_id} does not exist")
        # Readable now, so a write-rejection may safely name the project.
        scope.assert_writable(project)
        if status != "active":
            raise InvalidOperation(
                f"item {old_id} is {status}; only an active item may be superseded"
            )
        # Carry knowledge anchors (code_ref etc.) onto the replacement; redaction
        # counts belong to the OLD body, not the new one (mirrors supersede()).
        carried = {k: v for k, v in (old_meta or {}).items() if k != "redactions"}
        repl_vf = valid_from if valid_from is not None else old_vf
        repl_vt = valid_to if valid_to is not None else old_vt
        new_id = ingest_in(
            conn,
            project,
            type_,
            title,
            new_body,
            kind=_WEB_KIND,
            uri="",
            trust=_WEB_TRUST,
            status=_WEB_STATUS,
            tags=tags,
            meta=carried or None,
            valid_from=repl_vf,
            valid_to=repl_vt,
        )
        if new_id is None:
            raise DuplicateContent(
                "replacement body duplicates existing content; nothing saved for review"
            )
        queue_id = queue_review_in(
            conn,
            new_id,
            _WEB_REASON,
            _web_detail({"proposed_supersede": int(old_id), "supersede_reason": reason}),
        )
    return new_id, queue_id

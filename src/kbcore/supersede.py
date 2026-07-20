"""kbcore.supersede — supersession without loss (DATA-2, PLAN.md §5.5).

Old items are never edited or deleted: the old row flips to
``status='superseded'``, points at its replacement, and a ``supersedes``
relation records the edge (Invariant 1/5).
"""

from __future__ import annotations

import logging
from datetime import datetime

from kbcore import scope
from kbcore.db import connect, transaction
from kbcore.errors import DuplicateContent, InvalidOperation, NotFound
from kbcore.ingest import _as_utc, ingest_in

logger = logging.getLogger("kbcore.supersede")


def supersede(
    old_id: int,
    *,
    new_item_id: int | None = None,
    new_body: str | None = None,
    reason: str,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> int:
    """Supersede *old_id* with an existing item or a new body. Returns the new item id.

    Valid-time (WP1): the MANDATED correction path must not erase a fact's validity
    window. On the ``new_body`` path the replacement INHERITS the old item's
    valid_from/valid_to by default; an explicit ``valid_from``/``valid_to`` OVERRIDES
    that (world-changed correction). On the ``new_item_id`` path the existing item's
    own interval governs, untouched. Whenever the replacement's start is NEWLY set
    (explicit override, or the new item's own valid_from) and the old row is still
    open (valid_to IS NULL), the old row's validity window is CLOSED at that start
    (Zep/SQL:2011 validity-window close, NOT a body edit — items.body stays
    immutable).
    """
    if (new_item_id is None) == (new_body is None):
        raise InvalidOperation("pass exactly one of new_item_id or new_body")
    if new_item_id is not None and (valid_from is not None or valid_to is not None):
        # An existing item already carries its own interval; overriding it here
        # would edit that item's window from a supersede call — reject instead of
        # silently ignoring the arguments.
        raise InvalidOperation("valid_from/valid_to apply only to the new_body path")

    with transaction() as conn:
        row = conn.execute(
            "SELECT project, type, title, tags, status, meta, valid_from, valid_to "
            "FROM items WHERE id = %s FOR UPDATE",  # lock the old row: serialize
            # concurrent supersedes so two callers can't both see 'active', both
            # mint a replacement, and split superseded_by/valid_to (review finding).
            (old_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"item {old_id} does not exist")
        project, type_, title, tags, status, old_meta, old_valid_from, old_valid_to = row
        # A withheld old item must be byte-identical to a missing one — gate readability
        # BEFORE writability, else assert_writable would leak the withheld project's NAME
        # (existence + project-name oracle over arbitrary old_ids). Mirrors the
        # new_item_id wall below and get_item's assert_readable.
        if not scope.project_readable(conn, project):
            raise NotFound(f"item {old_id} does not exist")
        # Readable now, so a write-rejection may safely name it (public-only instance:
        # cannot supersede an item in a non-public project). The SAVE capability gates
        # the FEATURE before the rw grant gates the ROW (tiered access, plan §2) —
        # per-principal-constant, so no target signal rides the ordering.
        scope.require_cap(scope.CAP_SAVE)
        scope.assert_writable(project)
        if status == "superseded":
            raise InvalidOperation(f"item {old_id} is already superseded")

        # The instant (if any) at which the OLD fact stopped being true — set only
        # when the replacement introduces a NEW start (explicit override or the new
        # item's own valid_from), never for a plain interval-inheriting correction.
        close_from: datetime | None = None
        if new_body is not None:
            # Carry knowledge anchors (code_ref etc.) onto the replacement;
            # redaction counts belong to the old body, not the new one.
            carried = {k: v for k, v in (old_meta or {}).items() if k != "redactions"}
            repl_from = valid_from if valid_from is not None else old_valid_from
            repl_to = valid_to if valid_to is not None else old_valid_to
            new_item_id = ingest_in(
                conn,
                project,
                type_,
                title,
                new_body,
                kind="manual",
                trust="verified",
                tags=tags,
                meta=carried or None,
                valid_from=repl_from,
                valid_to=repl_to,
            )
            if new_item_id is None:
                raise DuplicateContent(
                    "replacement body duplicates existing content; pass new_item_id instead"
                )
            if valid_from is not None:
                close_from = _as_utc(valid_from)  # explicit new start → world changed
            elif valid_to is not None:
                # valid_to-only override records that the OLD fact stopped being
                # true at T. Close the old open window there too — otherwise the
                # old row stays open while the correction is closed, and an as_of>T
                # query returns the STALE superseded text and drops the correction
                # (inverted visibility; review finding). ingest_in already rejected
                # a valid_to <= old_valid_from, so this close is > old_valid_from.
                close_from = _as_utc(valid_to)
        else:
            repl = conn.execute(
                "SELECT status, project, valid_from FROM items WHERE id = %s", (new_item_id,)
            ).fetchone()
            # A replacement the caller cannot read is indistinguishable from a
            # missing one — no existence/status oracle, no withheld id written onto
            # the visible item's superseded_by (review finding). Walls off both
            # public-only and grantee scope.
            if repl is None or not scope.project_readable(conn, repl[1]):
                raise NotFound(f"replacement item {new_item_id} does not exist")
            if repl[0] != "active":
                # Guards against supersession cycles and chains that end in a
                # non-active successor (review finding).
                raise InvalidOperation(
                    f"replacement item {new_item_id} is {repl[0]}; must be active"
                )
            close_from = repl[2]  # the new item's own start closes the old window
        if new_item_id == old_id:
            raise InvalidOperation("an item cannot supersede itself")

        conn.execute(
            """
            UPDATE items
            SET status = 'superseded', superseded_by = %s, supersede_reason = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (new_item_id, reason, old_id),
        )
        # Auto-close the old validity window (world-changed semantics). Only when
        # the old fact was still open; a closed window is left as recorded.
        if close_from is not None and old_valid_to is None:
            close_from = _as_utc(close_from)
            if old_valid_from is not None and close_from < old_valid_from:
                # Strictly earlier would invert the old interval — the replacement
                # cannot start BEFORE the fact it supersedes began.
                raise InvalidOperation(
                    "replacement valid_from precedes the superseded item's valid_from"
                )
            if old_valid_from is None or close_from > old_valid_from:
                # A strictly-later start SHRINKS the window: close the old row there.
                # EQUAL start is a same-window takeover — the window is unchanged, so
                # skip the close (closing AT the start would collapse it to an empty
                # interval, which ingest forbids). This is the mandated same-window
                # correction path (review finding); only strict inequality acts.
                conn.execute(
                    "UPDATE items SET valid_to = %s, updated_at = now() WHERE id = %s",
                    (close_from, old_id),
                )
        conn.execute(
            """
            INSERT INTO relations (project, src_kind, src_id, rel, dst_kind, dst_id)
            VALUES (%s, 'item', %s, 'supersedes', 'item', %s)
            ON CONFLICT DO NOTHING
            """,
            (project, new_item_id, old_id),
        )
        scope.log_write(conn, old_id, "supersede")  # audit: attribute grantee writes
    return new_item_id


def supersede_doc_refresh(new_item_id: int) -> list[int]:
    """Supersede ALL prior ACTIVE items sharing the new item's source ``(kind, uri)``.

    A re-ingested changed document (same source uri, new content hash) is a
    NEWER REVISION, not competing knowledge — the repo owner already "disposed"
    by committing the change, so mirroring git history here is V4-compatible
    (owner decision, 2026-07). Returns the superseded ids ([] when the doc is
    new or the predecessors are already non-active). Per-predecessor isolation:
    one failure logs and the rest proceed.
    """
    with connect() as conn:
        src = conn.execute(
            """
            SELECT i.project, s.kind, s.uri FROM items i
            JOIN sources s ON s.id = i.source_id
            WHERE i.id = %s
            """,
            (new_item_id,),
        ).fetchone()
        if src is None or not src[2]:
            return []  # no source uri to key on (manual/chat items)
        project, kind, uri = src
        olds = conn.execute(
            """
            SELECT i.id FROM items i
            JOIN sources s ON s.id = i.source_id
            WHERE s.kind = %s AND s.uri = %s AND i.project = %s
              AND i.status = 'active' AND i.id <> %s
            ORDER BY i.id
            """,
            (kind, uri, project, new_item_id),
        ).fetchall()

    superseded: list[int] = []
    for (old_id,) in olds:
        try:
            supersede(old_id, new_item_id=new_item_id, reason=f"auto: newer revision of {uri}")
            superseded.append(old_id)
        except (InvalidOperation, NotFound):  # raced/already superseded — fine
            logger.info("doc-refresh: item %s already resolved; skipping", old_id)
    return superseded


def same_uri_active_pairs(project: str) -> list[tuple[int, int]]:
    """(stale_id, newest_id) pairs of ACTIVE git-doc items sharing a source uri.

    Backlog finder for the nightly doc-refresh sweep: any documents whose
    revisions still coexist (scanned before the scan-time hook existed, or with
    the flag off) — the newest item per (kind, uri) survives, every other
    active revision pairs with it.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.uri, array_agg(i.id ORDER BY i.created_at, i.id) AS ids
            FROM items i JOIN sources s ON s.id = i.source_id
            WHERE i.project = %s AND i.status = 'active' AND s.kind = 'git'
              AND s.uri <> ''
            GROUP BY s.kind, s.uri
            HAVING count(*) > 1
            ORDER BY s.uri
            """,
            (project,),
        ).fetchall()
    pairs: list[tuple[int, int]] = []
    for _uri, ids in rows:
        newest = ids[-1]
        pairs.extend((stale, newest) for stale in ids[:-1])
    return pairs

"""kbcore.resend_repair_replay_rows — the QUEUE-ROW side of the tier-3 replay (wave C.2).

Split from :mod:`kbcore.resend_repair_replay` (ratchet): everything here reads
``ingest_queue`` for ONE session uri and turns it into the byte-range facts the
plan is built on — the uri a ``--session`` prefix names, the pre-``since`` status
GATE, the covered end C and the holes the stamped ranges leave below it, R (the
re-sent rows inside ``[0, C)``), W0, W1 and NEW. Read-only; the writes live in the
replay module and :mod:`kbcore.resend_repair_items`. Stamps are read OFFLINE for an
owner-fired repair, never at ingest (proposal_v2 amendment 3).

Two fail-safes on what the stamps cannot prove (review 2026-10-11):

* a re-sent row touching a STAMP GAP (bytes below C no stamped range covers) is not
  in R — it moves to NEW, so the items it minted are kept as new content;
* the unstamped seed-era prefix is assumed to hold ``[0, first stamp)``; when it
  holds less than ``1 - PREFIX_TOLERANCE`` of that (stored bytes are the raw
  transcript bytes, scrub-adjusted), the plan carries a CHECK and ``--apply``
  refuses. The CHECK is ONE-SIDED: a pre-wave-C re-send only ever INFLATES the
  prefix, and an inflated prefix proves nothing about ``[0, first stamp)`` — the
  same bytes may simply have been stored twice. So a prefix over ``1 +
  PREFIX_TOLERANCE`` of the first stamp is a NOTE (:func:`prefix_note`, logged with
  its ratio), never a refusal: the owner accepts it as covered from that history.
  rm1's bd162e9f is that case: 570 unstamped rows / 484.5 MB under a first stamp
  at 221,030,428 (2.19x — the earlier dup-chunk incident re-sent the prefix).

The pre-``since`` verdicts — the GATE and the prefix NOTE — live in
:mod:`kbcore.resend_repair_replay_gate` (re-exported here).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, NamedTuple

from kbcore.db import connect
from kbcore.errors import InvalidOperation
from kbcore.ingest_queue import MAX_ATTEMPTS
from kbcore.resend_repair import SESSION_ROWS_SQL
from kbcore.resend_repair_models import ReplayPlan
from kbcore.resend_repair_replay_gate import (  # noqa: F401 — re-exports
    GATE_OK,
    GATE_SHOWN,
    PREFIX_TOLERANCE,
    gate,
    gate_options,
    prefix_note,
)
from kbcore.resend_repair_rows import PARKED_LIKE
from kbcore.resend_repair_undo import REPLAY_MODE

logger = logging.getLogger("kbcore.resend_repair")

MATCHES_SHOWN = 10  # ambiguous --session: uris listed in the refusal


class Row(NamedTuple):
    id: int
    project: str
    status: str
    created_at: datetime
    processed_at: datetime | None
    offset: int | None  # meta.flush.offset (None: unstamped)
    nbytes: int  # meta.flush.bytes (0 when unstamped)
    size: int  # stored content length
    repair: Any  # meta.repair (None when unmarked)
    attempts: int = 0
    parked: bool = False  # failed with error LIKE PARKED_LIKE: owner-parked resend residue

    @property
    def end(self) -> int:
        return (self.offset or 0) + self.nbytes

    @property
    def undrained(self) -> bool:
        """Still in the drain's reach: pending, or failed under the retry budget."""
        return self.status == "pending" or (
            self.status == "failed" and self.attempts < MAX_ATTEMPTS
        )


def resolve_uri(prefix: str) -> str:
    """The ONE session uri ``prefix`` names; refuses none or several (listing them)."""
    p = prefix.strip()
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT btrim(meta->>'source_uri') FROM ingest_queue
             WHERE {SESSION_ROWS_SQL} AND starts_with(btrim(meta->>'source_uri'), %(p)s)
             ORDER BY 1 LIMIT %(n)s
            """,  # noqa: S608 — SESSION_ROWS_SQL is a constant fragment
            {"p": "session:" + p, "n": MATCHES_SHOWN + 1},
        ).fetchall()
    uris = [str(r[0]) for r in rows]
    if not uris:
        raise InvalidOperation(f"--session {p!r} matches no session in the queue")
    if len(uris) > 1:
        shown = ", ".join(uris[:MATCHES_SHOWN]) + (" +more" if len(uris) > MATCHES_SHOWN else "")
        raise InvalidOperation(
            f"--session {p!r} is ambiguous — it matches {shown}; give a longer prefix"
        )
    return uris[0]


def load_rows(conn, uri: str) -> list[Row]:
    """Every row of ``uri`` (any project) — metadata only, never the content."""
    rows = conn.execute(
        f"""
        SELECT id, project, status, created_at, processed_at,
               (meta->'flush'->>'offset')::bigint, (meta->'flush'->>'bytes')::bigint,
               octet_length(content), meta->'repair', attempts,
               (status = 'failed' AND coalesce(error, '') LIKE %(parked)s)
          FROM ingest_queue
         WHERE {SESSION_ROWS_SQL} AND btrim(meta->>'source_uri') = %(uri)s
         ORDER BY id
        """,  # noqa: S608 — SESSION_ROWS_SQL is a constant fragment
        {"uri": uri, "parked": PARKED_LIKE},
    ).fetchall()
    return [
        Row(
            int(i),
            pj,
            st,
            at,
            done,
            off,
            int(nb or 0),
            int(size or 0),
            rep,
            int(att or 0),
            bool(pk),
        )  # fmt: skip
        for i, pj, st, at, done, off, nb, size, rep, att, pk in rows
    ]


def holes(stamped: list[Row], has_prefix: bool) -> list[tuple[int, int]]:
    """Byte ranges below C that no stamped range covers; a re-sent row touching one
    moves to NEW (:func:`fill_resent`). ``[0, first stamp)`` counts as covered when an
    unstamped seed-era prefix exists — the stamps cannot tell which bytes it held, so
    :func:`coverage` checks its SIZE instead (the CHECK)."""
    merged: list[list[int]] = []
    for a, b in sorted((r.offset or 0, r.end) for r in stamped):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out = [(merged[i][1], merged[i + 1][0]) for i in range(len(merged) - 1)]
    if merged and merged[0][0] > 0 and not has_prefix:
        out.insert(0, (0, merged[0][0]))
    return out


def coverage(uri: str, since: datetime, rows: list[Row]) -> tuple[ReplayPlan, list[tuple]]:
    """The plan's pre-``since`` half: gate, C, the unstamped prefix (and its CHECK)
    and the holes."""
    pre = [r for r in rows if r.created_at < since]
    status = gate(uri, pre)
    stamped = [r for r in pre if r.offset is not None]
    if not stamped:
        raise InvalidOperation(
            f"replay refused: no row of {uri} created before --since carries a flush stamp "
            "— the covered end is unknown, so no byte range can be called a re-send"
        )
    unstamped = [r for r in pre if r.offset is None]
    p = ReplayPlan(
        uri=uri,
        since=since,
        covered_end=max(r.end for r in stamped),
        pre_rows=len(pre),
        pre_status=status,
        pre_stamped=len(stamped),
        pre_unstamped_rows=len(unstamped),
        pre_unstamped_bytes=sum(r.size for r in unstamped),
        first_stamped_offset=min(r.offset or 0 for r in stamped),
    )
    p.prefix_short = bool(unstamped) and p.pre_unstamped_bytes < p.first_stamped_offset * (
        1.0 - PREFIX_TOLERANCE
    )
    if note := prefix_note(p):
        logger.warning("%s", note)
    hs = holes(stamped, has_prefix=bool(unstamped))
    p.gaps, p.gap_bytes = len(hs), sum(b - a for a, b in hs)
    return p, hs


def fill_resent(p: ReplayPlan, rows: list[Row], hs: list[tuple[int, int]]) -> None:
    """R (stamped rows at/after ``since`` ending at or below C and touching no stamp
    gap), its marker split, W0 = ``min(created_at)`` and W1 = ``max(processed_at)``
    over R, and NEW: rows outside R processed at/after W0 or still undrained, plus
    every gap row (``gap -> NEW``: what a gap row minted is kept). Leaves W0/W1/NEW
    empty when R is."""
    below = [
        r
        for r in rows
        if r.created_at >= p.since and r.offset is not None and r.end <= p.covered_end
    ]
    gap_ids = {r.id for r in below if any((r.offset or 0) < b and r.end > a for a, b in hs)}
    resent = [r for r in below if r.id not in gap_ids]
    p.r_in_gap = len(gap_ids)
    for r in resent:
        p.r_ids.append(r.id)
        p.r_bytes += r.size
        p.r_status[r.status] = p.r_status.get(r.status, 0) + 1
        p.r_undrained += int(r.undrained)
        if r.repair is None:
            p.mark_ids.append(r.id)
        elif isinstance(r.repair, dict) and r.repair.get("mode") == REPLAY_MODE:
            p.marked_before += 1
        else:
            p.other_marker += 1
    if not resent:
        return
    w0 = min(r.created_at for r in resent)
    done = [r.processed_at for r in resent if r.processed_at is not None]
    p.w0, p.first_done, p.w1 = w0, min(done, default=None), max(done, default=None)
    in_r = set(p.r_ids)
    new = [
        r
        for r in rows
        if r.id not in in_r
        and (
            r.id in gap_ids or r.undrained or (r.processed_at is not None and r.processed_at >= w0)
        )
    ]
    p.new_ids, p.new_bytes = [r.id for r in new], sum(r.size for r in new)


def new_texts(conn, ids: list[int]) -> list[str]:
    """The ASSEMBLED text of every NEW row (never raw JSONL), plus, per PROJECT, that
    project's NEW rows concatenated in id order — the drain groups by (project, uri)
    and joins a group the same way, so a line split across two rows is whole there
    even when another project's row sits between them by id."""
    from kbworker.sessions import assemble_text

    if not ids:
        return []
    by_project: dict[str, list[str]] = {}
    texts: list[str] = []
    for project, content in conn.execute(
        "SELECT project, content FROM ingest_queue WHERE id = ANY(%(ids)s::bigint[]) ORDER BY id",
        {"ids": list(ids)},
    ).fetchall():
        by_project.setdefault(str(project), []).append(str(content))
        texts.append(assemble_text(str(content)))
    for contents in by_project.values():
        if len(contents) > 1:
            texts.append(assemble_text("".join(contents)))
    return texts

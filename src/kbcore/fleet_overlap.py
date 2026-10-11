"""kbcore.fleet_overlap — the RESEND ALERT query behind ``resend_summary.overlap_alerts``.

Split from :mod:`kbcore.fleet_flush` (the 300-line cap; wave C.2 review fixes,
incident KB 49064): a stale transcript copy re-sent 282 MB of already-covered bytes
from byte 0 and the digest said only ``overlap rows N``. READ-ONLY telemetry —
proposal_v2 amendment 3: nothing here refuses, trims or range-checks on a stamp.
``kbcore.fleet_flush`` re-exports both names (the established seam).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from kbcore.config import settings
from kbcore.fleet_rows import mb, sid8
from kbcore.scrub import scrub

# Sessions over the RESEND ALERT threshold kept in the report (the digest renders
# fewer — kbworker.digest_resend_alert.RESEND_ALERT_LINES). Bounds the report row.
OVERLAP_ALERTS_KEPT = 10


def overlap_alerts(conn, start: datetime) -> list[dict[str, Any]]:
    """Per ``(principal, session uri)``: the stamped rows created after ``start`` that
    carry ``meta.overlap`` (bytes this principal had ALREADY covered — written by
    :func:`kbcore.queue_guard.apply_stamp`) and the sum of ``meta.overlap.bytes``.
    Only sessions at/over ``KB_RESEND_ALERT_MB`` OR ``KB_RESEND_ALERT_ROWS`` are
    returned, largest first, at most :data:`OVERLAP_ALERTS_KEPT`. A knob ``<= 0``
    disables THAT criterion; both ``<= 0`` disables the alert and skips the query.
    ``first_at`` is the first such row in the window; ``since`` the first
    UNREPAIRED FULL-overlap row of that (principal, uri) EVER (:func:`_first_overlap`;
    ``first_at`` when there is none) — what the digest offers as the repair's
    ``--since``, so every night's alert for one incident names the same instant (one
    extra query, bounded to the uris alerted tonight). Telemetry only —
    nothing refuses on it (proposal_v2 amendment 3). The principal is scrubbed
    (rule 12: a token-shaped name never reaches a push).
    """
    mb_floor = float(settings.kb_resend_alert_mb)
    rows_floor = int(settings.kb_resend_alert_rows)
    if mb_floor <= 0 and rows_floor <= 0:
        return []
    rows = conn.execute(
        """
        SELECT coalesce(nullif(btrim(meta->>'submitted_by'), ''), '(unknown)'),
               btrim(meta->>'source_uri'), count(*),
               coalesce(sum(CASE WHEN jsonb_typeof(meta->'overlap'->'bytes') = 'number'
                                 THEN (meta->'overlap'->>'bytes')::numeric END), 0) AS b,
               min(created_at)
          FROM ingest_queue
         WHERE created_at > %(start)s AND meta ? 'overlap' AND meta ? 'flush'
           AND starts_with(btrim(meta->>'source_uri'), 'session:')
         GROUP BY 1, 2
        HAVING (%(rows)s::bigint IS NOT NULL AND count(*) >= %(rows)s::bigint)
            OR (%(bytes)s::numeric IS NOT NULL
                AND coalesce(sum(CASE WHEN jsonb_typeof(meta->'overlap'->'bytes') = 'number'
                                      THEN (meta->'overlap'->>'bytes')::numeric END), 0)
                    >= %(bytes)s::numeric)
         ORDER BY 4 DESC, 1, 2
         LIMIT %(cap)s
        """,
        {
            "start": start,
            "rows": rows_floor if rows_floor > 0 else None,
            "bytes": max(1, int(mb_floor * 1_000_000)) if mb_floor > 0 else None,
            "cap": OVERLAP_ALERTS_KEPT,
        },
    ).fetchall()
    first_ever = _first_overlap(conn, [str(r[1]) for r in rows])
    return [
        {
            "principal": scrub(str(principal))[0],
            "sid8": sid8(uri),
            "rows": int(n),
            "mb": mb(b),
            "first_at": first.isoformat(),
            "since": first_ever.get((str(principal), str(uri)), first).isoformat(),
        }
        for principal, uri, n, b, first in rows
    ]


def _first_overlap(conn, uris: list[str]) -> dict[tuple[str, str], datetime]:
    """``(principal, uri) -> min(created_at)`` over the given session uris' rows (all
    time) that the replay could place in R and no repair has touched: a FULL overlap
    (``overlap.bytes >= flush.bytes`` — every byte already covered) carrying no
    ``meta.repair``. A benign partial overlap (a timeout retry with a grown chunk) or
    an already-repaired earlier incident would otherwise give a too-early ``--since``:
    C shrinks and the new incident's duplicates fall into NEW (review 2, MUST 3).
    The comparison is CASE-guarded, not a bare AND — Postgres does not guarantee AND
    evaluation order, so a guard written as an AND can still reach the cast. A
    session with no such row falls back to ``first_at`` in the caller."""
    if not uris:
        return {}
    rows = conn.execute(
        """
        SELECT coalesce(nullif(btrim(meta->>'submitted_by'), ''), '(unknown)'),
               btrim(meta->>'source_uri'), min(created_at)
          FROM ingest_queue
         WHERE kind = 'session' AND btrim(meta->>'source_uri') = ANY(%(uris)s::text[])
           AND meta ? 'overlap' AND meta ? 'flush' AND NOT meta ? 'repair'
           AND CASE WHEN jsonb_typeof(meta->'overlap'->'bytes') = 'number'
                     AND jsonb_typeof(meta->'flush'->'bytes') = 'number'
                    THEN (meta->'overlap'->>'bytes')::numeric
                         >= (meta->'flush'->>'bytes')::numeric
                    ELSE false END
         GROUP BY 1, 2
        """,
        {"uris": sorted(set(uris))},
    ).fetchall()
    return {(str(p), str(u)): at for p, u, at in rows}

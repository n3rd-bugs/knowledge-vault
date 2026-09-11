"""kbcore.fleet_flush — READ-ONLY flush telemetry per submitting box (wave S, S4).

Root cause KB 33783: a box resent whole transcripts from byte 0 for weeks and
no surface named it. These aggregates ride the nightly report beside
:func:`kbcore.fleet_health.fleet_by_box` (ADDED keys, never a rename) and feed
the digest / weekly lines in :mod:`kbworker.digest_fleet_lines` and
:mod:`kbworker.weekly_fleet`.

Identity is the AUTHENTICATED submitter (``meta->>'submitted_by'``, rule 12);
the ``flush.box`` stamp is a LABEL only — one principal with two labels is one
credential on two machines. Stamps (``meta.flush``, written by the REST path)
are telemetry: nothing here refuses, trims or range-checks (proposal §7).
Positive-only: a quiet fleet yields empty containers and by-box dicts carry
optional keys only when they have something to say (the fleet_health posture).
Nothing here writes. Every query is parameterised; the window is the tiled
one (:func:`kbcore.fleet_health.window_start`) so every fleet aggregate
describes the same rows.

Module map (the 300-line cap): the shared row CTE lives in
:mod:`kbcore.fleet_rows`; silence and hook staleness — keyed on ``(principal,
label)`` — in :mod:`kbcore.fleet_stale`; ``queue_coverage`` in
:mod:`kbcore.fleet_coverage`. All are re-exported here under the brief's names.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from kbcore.config import settings
from kbcore.db import connect
from kbcore.fleet_coverage import queue_coverage  # noqa: F401 — the brief's import path
from kbcore.fleet_health import window_start
from kbcore.fleet_rows import (  # noqa: F401 — FLEET_FLOOR_DAYS is part of the surface
    FLEET_FLOOR_DAYS,
    ROWS_SQL,
    floor_at,
    mb,
    sid8,
)
from kbcore.fleet_stale import fleet_silent, stale_hook_boxes  # noqa: F401 — re-exports

log = logging.getLogger("kbcore.fleet_flush")


def fleet_flush_by_box(hours: int | None = None) -> dict[str, dict[str, Any]]:
    """Per submitting box: when it last flushed, what it sent in the window, and
    what it left behind.

    The population is the 30-day floor (:data:`FLEET_FLOOR_DAYS`): a box whose
    last row is older has LEFT the fleet and is not listed — the same
    population :func:`fleet_silent` reads, so "silent" and "gone" never
    overlap. Always: ``last_seen`` (ISO UTC; for a box in the population the
    floor's max(created_at) is its all-time max), ``age_hours``,
    ``rows``/``mb``/``sessions`` in the tiled window, ``sessions_30d``. Only
    when non-zero/non-empty: ``unstamped`` (window rows with no ``meta.flush``),
    ``labels`` (distinct ``flush.box``), ``last_event``, ``hook_version`` (latest
    stamped), ``tails_open`` + ``largest_tail`` (see :func:`open_tails`),
    ``sweep_sessions`` + ``sweep_recovered_mb`` (``flush.event = 'Backfill'``).
    """
    now = datetime.now(UTC)
    start = window_start(hours, now)
    params = {"floor": floor_at(now), "start": start}
    with connect() as conn:
        rows = conn.execute(
            ROWS_SQL
            + """
            SELECT box,
                   max(created_at) AS last_seen,
                   count(*) FILTER (WHERE created_at > %(start)s) AS rows_w,
                   coalesce(sum(bytes) FILTER (WHERE created_at > %(start)s), 0) AS bytes_w,
                   count(DISTINCT uri) FILTER (WHERE created_at > %(start)s) AS sessions_w,
                   count(DISTINCT uri) AS sessions_30d,
                   count(*) FILTER (WHERE created_at > %(start)s AND flush IS NULL) AS unstamped,
                   array_remove(array_agg(DISTINCT flush->>'box'), NULL) AS labels,
                   (array_agg(flush->>'event' ORDER BY created_at DESC, id DESC)
                      FILTER (WHERE flush->>'event' IS NOT NULL))[1] AS last_event,
                   (array_agg(hook_version ORDER BY created_at DESC, id DESC)
                      FILTER (WHERE hook_version IS NOT NULL))[1] AS hook_version,
                   count(DISTINCT uri) FILTER (WHERE created_at > %(start)s
                                                 AND flush->>'event' = 'Backfill') AS sweep_s,
                   coalesce(sum(bytes) FILTER (WHERE created_at > %(start)s
                                                 AND flush->>'event' = 'Backfill'), 0) AS sweep_b
              FROM rows30
             GROUP BY box
             ORDER BY box
            """,
            params,
        ).fetchall()
    tails = open_tails(now=now)
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        box, last_seen = str(r[0]), r[1]
        d: dict[str, Any] = {
            "last_seen": last_seen.isoformat(),
            "age_hours": round((now - last_seen).total_seconds() / 3600.0, 1),
            "rows": int(r[2]),
            "mb": mb(r[3]),
            "sessions": int(r[4]),
            "sessions_30d": int(r[5]),
        }
        if int(r[6]):
            d["unstamped"] = int(r[6])
        if r[7]:
            d["labels"] = sorted(str(x) for x in r[7])
        if r[8]:
            d["last_event"] = str(r[8])
        if r[9]:
            d["hook_version"] = str(r[9])
        if int(r[10]):
            d["sweep_sessions"] = int(r[10])
            d["sweep_recovered_mb"] = mb(r[11])
        mine = [t for t in tails if t["box"] == box]
        if mine:
            top = max(mine, key=lambda t: t["mb"])
            d["tails_open"] = len(mine)
            d["largest_tail"] = {"mb": top["mb"], "project": top["project"], "sid8": top["sid8"]}
        out[box] = d
    return out


def open_tails(hours: int | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Stamped sessions whose flushed reach (``max(offset+bytes)``) stops short of
    the transcript size the LATEST stamp reported, and whose latest stamped
    firing is older than ``hours`` (default ``KB_FLEET_TAIL_HOURS``): the flush
    stopped short and nothing resumed it. One entry per (box, session).
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=hours if hours is not None else settings.kb_fleet_tail_hours)
    with connect() as conn:
        rows = conn.execute(
            ROWS_SQL
            + """
            SELECT box, uri,
                   (array_agg(project ORDER BY created_at DESC, id DESC))[1] AS project,
                   max((flush->>'offset')::bigint + (flush->>'bytes')::bigint) AS reach,
                   (array_agg((flush->>'size')::bigint ORDER BY created_at DESC, id DESC))[1]
                       AS size,
                   max(created_at) AS last_at
              FROM rows30
             WHERE flush IS NOT NULL AND uri IS NOT NULL
             GROUP BY box, uri
            HAVING max((flush->>'offset')::bigint + (flush->>'bytes')::bigint)
                   < (array_agg((flush->>'size')::bigint ORDER BY created_at DESC, id DESC))[1]
               AND max(created_at) < %(cutoff)s
             ORDER BY box, uri
            """,
            {"floor": floor_at(now), "cutoff": cutoff},
        ).fetchall()
    return [
        {
            "box": str(box),
            "uri": str(uri),
            "sid8": sid8(uri),
            "project": str(project),
            "mb": mb(int(size) - int(reach)),
            "last_at": last_at.isoformat(),
        }
        for box, uri, project, reach, size, last_at in rows
    ]


def resend_summary(hours: int | None = None) -> dict[str, Any]:
    """What the server-side resend guard refused, from the BOUNDED ledger
    ``ingest_resend_events`` (one row per (principal, session uri), UPSERTed on
    every refusal — a LIFETIME tally per session, never a per-night delta).

    Ledger rows touched in the window (``last_at``) sum to ``refused_rows`` /
    ``refused_mb`` — "refused TO DATE by the sessions still resending" — plus
    ``sessions`` (DISTINCT uris: one uri refused under two principals is one
    session), ``boxes`` and the ``per_box`` top three ``[principal, rows,
    mb]``. From the queue's own meta: ``overlap_rows`` (a stamped chunk whose
    range the same principal already covered) in the window;
    ``duplicate_rows``/``duplicate_mb`` (status 'duplicate', all time — the
    repair's trail). An inconsistent stamp leaves NO row marker (the guard
    only counts it in-process), so there is no ``stamp_inconsistent`` key.
    Positive-only: zero keys are omitted, ``{}`` when nothing happened.
    """
    start = window_start(hours)
    with connect() as conn:
        led = conn.execute(
            """
            SELECT principal, sum(refused_rows), sum(refused_bytes)
              FROM ingest_resend_events
             WHERE last_at > %(start)s
             GROUP BY principal
             ORDER BY sum(refused_rows) DESC, principal
            """,
            {"start": start},
        ).fetchall()
        (sessions,) = conn.execute(
            "SELECT count(DISTINCT uri) FROM ingest_resend_events WHERE last_at > %(start)s",
            {"start": start},
        ).fetchone()
        (overlap,) = conn.execute(
            "SELECT count(*) FROM ingest_queue WHERE created_at > %(start)s AND meta ? 'overlap'",
            {"start": start},
        ).fetchone()
        dup_rows, dup_bytes = conn.execute(
            "SELECT count(*), coalesce(sum(octet_length(content)), 0) "
            "FROM ingest_queue WHERE status = 'duplicate'"
        ).fetchone()
    out: dict[str, Any] = {}
    if led:
        out["refused_rows"] = int(sum(r[1] for r in led))
        out["refused_mb"] = mb(sum(r[2] for r in led))
        out["sessions"] = int(sessions)
        out["boxes"] = [str(r[0]) for r in led]
        out["per_box"] = [[str(r[0]), int(r[1]), mb(r[2])] for r in led[:3]]
    if int(overlap):
        out["overlap_rows"] = int(overlap)
    if int(dup_rows):
        out["duplicate_rows"] = int(dup_rows)
        out["duplicate_mb"] = mb(dup_bytes)
    return out

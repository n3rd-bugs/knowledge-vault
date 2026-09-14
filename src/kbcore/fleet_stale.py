"""kbcore.fleet_stale — hook staleness per ``(principal, label)``, silence per HOST.

Design of record V4-M-6: staleness is keyed on ``(principal, flush.box)`` when
a label is present. One credential on two machines is the exact case the label
exists for — machine A runs ``update.sh`` and stamps the served version;
machine B, still on the old hooks, must NOT hide behind A's latest row. Keying
on the principal alone did exactly that (review of the first cut).

Silence (first real week of KB 34161, weekly 2026-09-13) is keyed on the
HOST instead: the per-project tokens (``project@host``) meant a finished
project read as a silent "box" seven times over, while the one thing worth an
alert — a MACHINE with no principal submitting — was buried in the noise. That
host fold SUPERSEDES V4-M-6 for the SILENCE surface only: a labelled group
(``principal/label``) folds into ``host_of(principal)`` with the rest of its
credential, so one credential's quiet second desk is not named while its
first desk still flushes (staleness stays label-keyed — an old-hooks desk is
still named). The label is ``hostname`` by default but ``KB_BOX`` may say
anything, so keying silence on it would split one machine into two "hosts"
and re-create the noise. Both surfaces still read the SAME grouping query.

Read-only, parameterised, positive-only (``[]`` / ``{}`` when all is well).
``fleet_flush`` re-exports both public functions under the names the brief
uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from kbcore.config import settings
from kbcore.db import connect
from kbcore.fleet_health import window_start
from kbcore.fleet_rows import ROWS_SQL, floor_at, host_of

_GROUPS_SQL = (
    ROWS_SQL
    + """
    SELECT box, flush->>'box' AS label,
           max(created_at) AS last_seen,
           count(DISTINCT uri) AS sessions_30d,
           count(*) FILTER (WHERE created_at > %(start)s) AS rows_w,
           count(*) FILTER (WHERE created_at > %(start)s AND flush IS NULL) AS unstamped_w,
           count(*) FILTER (WHERE created_at > %(start)s AND hook_version IS NOT NULL)
               AS versioned_w,
           count(*) FILTER (WHERE created_at > %(start)s AND kind = 'session'
                              AND uri LIKE 'session:%%') AS hook_rows_w,
           (array_agg(hook_version ORDER BY created_at DESC, id DESC)
              FILTER (WHERE hook_version IS NOT NULL))[1] AS hook_version
      FROM rows30
     GROUP BY box, label
     ORDER BY box, label
    """
)


@dataclass(frozen=True)
class FleetGroup:
    """One ``(principal, label)`` group of the 30-day population."""

    box: str
    label: str | None
    last_seen: datetime
    sessions_30d: int
    rows_window: int  # rows in the tiled window
    unstamped_window: int  # window rows with no ``meta.flush``
    versioned_window: int  # window rows that stamp a ``hook_version``
    hook_rows_window: int  # window rows the FLUSH HOOK sent (kind session, uri session:)
    hook_version: str | None  # the group's LATEST stamped hook version
    prestamp: bool  # the UNLABELLED rows of a principal that also has labelled rows

    @property
    def name(self) -> str:
        return f"{self.box}/{self.label}" if self.label else self.box


def fleet_groups(hours: int | None = None, now: datetime | None = None) -> list[FleetGroup]:
    """Every ``(principal, label)`` group in the population, ordered by name.

    A principal whose rows carry no label at all is one group named by the
    principal. A principal with labelled rows is one group PER label, and its
    unlabelled rows form a ``prestamp`` group — the history from before its
    hooks stamped, or a transport that never stamps (MCP): callers decide
    whether that group can speak.
    """
    now = now or datetime.now(UTC)
    with connect() as conn:
        rows = conn.execute(
            _GROUPS_SQL, {"floor": floor_at(now), "start": window_start(hours, now)}
        ).fetchall()
    labelled = {str(box) for box, label, *_rest in rows if label}
    return [
        FleetGroup(
            box=str(box),
            label=str(label) if label else None,
            last_seen=last_seen,
            sessions_30d=int(sessions),
            rows_window=int(rows_w),
            unstamped_window=int(unstamped_w),
            versioned_window=int(versioned_w),
            hook_rows_window=int(hook_rows_w),
            hook_version=str(hv) if hv else None,
            prestamp=(not label) and str(box) in labelled,
        )
        for (
            box,
            label,
            last_seen,
            sessions,
            rows_w,
            unstamped_w,
            versioned_w,
            hook_rows_w,
            hv,
        ) in rows
    ]


def fleet_silent(hours: int | None = None) -> list[dict[str, Any]]:
    """HOSTS none of whose principals flushed within ``hours`` (default
    ``KB_FLEET_SILENT_HOURS``). Evidence-of-work — there is no heartbeat
    endpoint. The 30-day population is grouped by :func:`host_of` the
    principal; a host's ``last_seen`` is the max over its principals and
    labels, so one live principal keeps its host off the list however many
    finished projects went quiet beside it. A labelled principal's pre-stamp
    rows never count. Each entry: ``host``, ``age_hours``, ``principals``
    (sorted group names), ``sessions_30d`` (summed). Positive-only: ``[]``.
    """
    now = datetime.now(UTC)
    limit = timedelta(hours=hours if hours is not None else settings.kb_fleet_silent_hours)
    hosts: dict[str, dict[str, Any]] = {}
    for g in fleet_groups(now=now):
        if g.prestamp:
            continue
        h = hosts.setdefault(
            host_of(g.box), {"last_seen": g.last_seen, "principals": set(), "sessions_30d": 0}
        )
        h["last_seen"] = max(h["last_seen"], g.last_seen)
        h["principals"].add(g.name)
        h["sessions_30d"] += g.sessions_30d
    out: list[dict[str, Any]] = []
    for host, h in sorted(hosts.items()):
        age = now - h["last_seen"]
        if age <= limit:
            continue
        out.append(
            {
                "host": host,
                "age_hours": round(age.total_seconds() / 3600.0, 1),
                "principals": sorted(h["principals"]),
                "sessions_30d": h["sessions_30d"],
            }
        )
    return out


def stale_hook_boxes(manifest_version: str | None = None) -> dict[str, Any]:
    """Groups whose LATEST stamped ``hook_version`` is not the served manifest
    (``manifest_version``; default :func:`kbcore.setup_manifest.version`),
    keyed ``principal/label`` when a label is present.

    A group whose rows in the window carry NO ``hook_version`` at all is
    ``unversioned`` (weekly 2026-09-13): every served hook since review #8 H15 stamps the
    version on the REST path, so only pre-H15 hooks and MCP-transport boxes
    cannot — which is exactly the box the owner wants named, and the one
    ``KB_FLEET_REQUIRE_STAMPS`` off used to hide. With the knob on, a group
    that stamps a version but still submits rows with no ``meta.flush`` is
    ``unstamped`` (that arm and the version check are unchanged). Only a group
    with rows in the window can be ``unversioned``; a labelled principal's
    pre-stamp group speaks only while it still submits rows in the window (an
    old-hooks machine sharing the credential), never for its history.
    Returns ``{}`` when every group is current, else ``{"server": <version>,
    "boxes": {name: <its version> | "unversioned" | "unstamped"}}``.
    """
    if manifest_version is None:
        from kbcore import setup_manifest

        manifest_version = setup_manifest.version()
    require = bool(settings.kb_fleet_require_stamps)
    boxes: dict[str, str] = {}
    for g in fleet_groups():
        if g.prestamp and not g.rows_window:
            continue
        if g.hook_version and g.hook_version != manifest_version:
            boxes[g.name] = g.hook_version
        # Only the FLUSH HOOK's own traffic can be unversioned: a memory or document
        # submission over MCP (2026-09-14: one owner-token kb_ingest_session row named
        # the OWNER 'unversioned') carries no hook at all.
        elif g.hook_rows_window and not g.versioned_window:
            boxes[g.name] = "unversioned"
        elif require and (not g.hook_version or g.unstamped_window):
            boxes[g.name] = "unstamped"
    return {"server": manifest_version, "boxes": boxes} if boxes else {}

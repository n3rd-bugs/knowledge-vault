"""kbcore.fleet_stale — silence and hook staleness per ``(principal, label)`` (wave S).

Design of record V4-M-6: silence AND staleness are keyed on ``(principal,
flush.box)`` when a label is present. One credential on two machines is the
exact case the label exists for — machine A runs ``update.sh`` and stamps the
served version; machine B, still on the old hooks, must NOT hide behind A's
latest row. Keying on the principal alone did exactly that (review of the
first cut), so both surfaces read the SAME grouping query here.

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
from kbcore.fleet_rows import ROWS_SQL, floor_at

_GROUPS_SQL = (
    ROWS_SQL
    + """
    SELECT box, flush->>'box' AS label,
           max(created_at) AS last_seen,
           count(DISTINCT uri) AS sessions_30d,
           count(*) FILTER (WHERE created_at > %(start)s) AS rows_w,
           count(*) FILTER (WHERE created_at > %(start)s AND flush IS NULL) AS unstamped_w,
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
            hook_version=str(hv) if hv else None,
            prestamp=(not label) and str(box) in labelled,
        )
        for box, label, last_seen, sessions, rows_w, unstamped_w, hv in rows
    ]


def fleet_silent(hours: int | None = None) -> list[dict[str, Any]]:
    """Groups whose last flush is older than ``hours`` (default
    ``KB_FLEET_SILENT_HOURS``). Evidence-of-work — there is no heartbeat
    endpoint. The quiet machine of a shared credential is named
    ``principal/label``; a labelled principal's pre-stamp rows never count.
    Positive-only: ``[]`` when all is well.
    """
    now = datetime.now(UTC)
    limit = timedelta(hours=hours if hours is not None else settings.kb_fleet_silent_hours)
    out: list[dict[str, Any]] = []
    for g in fleet_groups(now=now):
        if g.prestamp:
            continue
        age = now - g.last_seen
        if age <= limit:
            continue
        out.append(
            {
                "box": g.box,
                "label": g.label,
                "name": g.name,
                "age_hours": round(age.total_seconds() / 3600.0, 1),
                "sessions_30d": g.sessions_30d,
            }
        )
    return out


def stale_hook_boxes(manifest_version: str | None = None) -> dict[str, Any]:
    """Groups whose LATEST stamped ``hook_version`` is not the served manifest
    (``manifest_version``; default :func:`kbcore.setup_manifest.version`),
    keyed ``principal/label`` when a label is present.

    A group that never stamps a version (MCP transport, pre-H15 hooks) cannot
    be told apart from a current one and is NOT flagged — unless
    ``KB_FLEET_REQUIRE_STAMPS`` is on, when a missing version or any unstamped
    (no ``meta.flush``) row in the window marks it ``unstamped``. A labelled
    principal's pre-stamp group speaks only while it still submits rows in the
    window (an old-hooks machine sharing the credential), never for its history.
    Returns ``{}`` when every group is current, else ``{"server": <version>,
    "boxes": {name: <its version> | "unstamped"}}``.
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
        elif require and (not g.hook_version or g.unstamped_window):
            boxes[g.name] = "unstamped"
    return {"server": manifest_version, "boxes": boxes} if boxes else {}

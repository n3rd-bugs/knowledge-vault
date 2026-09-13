"""kbcore.fleet_rows — the ONE queue-row CTE every fleet-flush aggregate reads.

Split out of :mod:`kbcore.fleet_flush` so :mod:`kbcore.fleet_stale` (silence
and hook staleness per ``(principal, label)`` group) can share the population
without a circular import. Read-only; every consumer parameterises ``floor``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# The fleet POPULATION: boxes with any row in this many days. A box that stopped
# before the floor has left the fleet; one inside it that went quiet is SILENT.
FLEET_FLOOR_DAYS = 30

# The last FLEET_FLOOR_DAYS of queue rows with the box identity (the
# AUTHENTICATED submitter, rule 12), the session uri and the optional flush
# stamp pulled out. ``flush->>'box'`` is a LABEL only: one principal with two
# labels is one credential on two machines.
ROWS_SQL = """
    WITH rows30 AS (
        SELECT id, project, status, created_at,
               octet_length(content) AS bytes,
               coalesce(nullif(btrim(meta->>'submitted_by'), ''), '(unknown)') AS box,
               nullif(btrim(meta->>'source_uri'), '') AS uri,
               meta->'flush' AS flush,
               nullif(btrim(meta->>'hook_version'), '') AS hook_version
          FROM ingest_queue
         WHERE created_at > %(floor)s
    )
"""


def floor_at(now: datetime) -> datetime:
    """The population's lower bound: ``now`` minus :data:`FLEET_FLOOR_DAYS`."""
    return now - timedelta(days=FLEET_FLOOR_DAYS)


def mb(n: Any) -> float:
    """Bytes to one-decimal megabytes (``None`` reads as 0)."""
    return round(float(n or 0) / 1_000_000, 1)


def sid8(uri: str | None) -> str:
    """The first eight characters of a ``session:<sid>`` uri's sid."""
    tail = (uri or "").partition(":")[2]
    return tail[:8]


def host_of(box: str) -> str:
    """The MACHINE a principal names: the part after the last ``@`` of a
    per-project token's ``project@host`` (rule 12 identity). A principal with
    no ``@`` is its own host; ``(unknown)`` stays ``(unknown)``. A finished
    project going quiet is normal — a host going quiet is the alert (first
    real week of KB 34161, weekly 2026-09-13)."""
    if box == "(unknown)" or "@" not in box:
        return box
    return box.rpartition("@")[2] or box

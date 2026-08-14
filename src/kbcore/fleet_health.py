"""kbcore.fleet_health — SERVER-SIDE staleness signal for the capture fleet.

WHY THIS IS NOT A CLIENT BEACON. The obvious design is "every machine reports
its hook version on submit". It was tried and it is not trustworthy for this
question, for two independent reasons:

* the beacon itself shipped broken once, and a broken self-report is
  indistinguishable from a healthy one — the failure mode is silence;
* the boxes that MATTER are by definition the ones running PRE-BEACON hooks.
  A machine that predates the beacon cannot send it, so the exact population
  this signal exists to find is the population the beacon cannot see.

So the evidence used here is the WORK ITSELF, which no client can fake or
forget: a submission whose ``source_uri`` is missing or degenerate. Every
enqueue path in this repo supplies one (``kbrest`` /api/ingest-session,
``kb_ingest_session``, the local flush in ``kbworker.sessions``,
``kbcore.documents``), so a degenerate row does not mean "an odd submission" —
it means the SUBMITTER did not send a session id, i.e. a machine still on
pre-fix hooks. rm1 is producing exactly these rows today.

Cost of getting it wrong is asymmetric and cheap: a false positive is one extra
digest line telling an owner to check a box; a false negative is a machine
silently capturing sessions that can never be coalesced (every row becomes its
own ``queue:<kind>:<id>`` group, i.e. its own 14B extraction).

NO NEW TABLE AND NO MIGRATION. ``ingest_queue`` rows already ARE the evidence;
this module is a read-only aggregate over them. Nothing here writes.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from kbcore.db import connect

# The SQL definition of "degenerate", imported rather than restated. This module
# must agree with the grouping pass EXACTLY — a second copy of the predicate is a
# second thing to keep in step, and a signal that disagrees with the behaviour it
# is reporting on is worse than no signal.
from kbcore.ingest_queue import _DEGENERATE_URI_SQL

log = logging.getLogger("kbcore.fleet_health")

# One day, because the nightly runs daily: a shorter window would miss a machine
# that only submits in the evening, a longer one would keep alarming about a box
# that was fixed yesterday.
FLEET_WINDOW_HOURS = 24


def degenerate_by_project(hours: int = FLEET_WINDOW_HOURS) -> dict[str, dict[str, int]]:
    """Per project, how many of the last ``hours`` of queue rows are degenerate.

    Returns ``{project: {"degenerate": n, "total": m}}`` for every project that
    submitted anything in the window — the denominator rides along deliberately,
    because "3 degenerate" reads very differently against a total of 3 (a box
    wholly on old hooks) than against 300 (one stale client among many).

    Read-only, parameterized, and bounded by the window; safe to call from the
    nightly tail. A project absent from the result submitted nothing.
    """
    window = max(1, int(hours))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT project,
                   count(*) FILTER (WHERE {_DEGENERATE_URI_SQL}) AS degenerate,
                   count(*) AS total
              FROM ingest_queue
             WHERE created_at > now() - make_interval(hours => %s)
             GROUP BY project
             ORDER BY project
            """,  # noqa: S608 — _DEGENERATE_URI_SQL is a constant fragment
            (window,),
        ).fetchall()
    out = {str(p): {"degenerate": int(d), "total": int(t)} for p, d, t in rows}
    stale = stale_hook_projects(out)
    if stale:
        log.warning(
            "fleet: %d project(s) received submissions with no session_uri in the last %dh "
            "— a machine is on pre-fix hooks: %s",
            len(stale),
            window,
            ", ".join(stale),
        )
    return out


def fleet_by_box(hours: int = FLEET_WINDOW_HOURS) -> dict[str, dict[str, int]]:
    """Per SUBMITTING BOX: the degenerate/total split, keyed by the authenticated
    principal the enqueue path stamps into ``meta->>'submitted_by'`` (KB 19663).

    Review #3 P2 (box identity): the server always AUTHENTICATED a per-machine
    principal at ingest and then discarded it, so the project view could say
    "someone on pre-fix hooks submitted into project X" but never WHICH BOX to go
    fix. This is the same read-only aggregate keyed by the submitter instead —
    deliberately the SAME shape as :func:`degenerate_by_project`, so the digest's
    tolerant reader (:func:`stale_hook_projects`) reads either view unchanged.

    Rows enqueued before the stamp existed group under ``(unknown)``, which is
    itself signal: an unknown box is a box on old code — exactly the population
    this module exists to find. Rides the nightly report as the ADDED key
    ``fleet_by_box`` beside the project view; nothing is renamed.
    """
    window = max(1, int(hours))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT coalesce(nullif(btrim(meta->>'submitted_by'), ''), '(unknown)') AS box,
                   count(*) FILTER (WHERE {_DEGENERATE_URI_SQL}) AS degenerate,
                   count(*) AS total
              FROM ingest_queue
             WHERE created_at > now() - make_interval(hours => %s)
             GROUP BY box
             ORDER BY box
            """,  # noqa: S608 — _DEGENERATE_URI_SQL is a constant fragment
            (window,),
        ).fetchall()
    return {str(b): {"degenerate": int(d), "total": int(t)} for b, d, t in rows}


def stale_hook_projects(report: Any) -> list[str]:
    """The projects in a :func:`degenerate_by_project` report with a live problem.

    Split out so the log line, the digest line and any future surface all read
    "nonzero" the same way, and so a caller holding a report loaded from an OLD
    ``consolidation_requests.result`` can re-derive the list without the DB.

    Deliberately TOLERANT of its argument: the nightly digest calls this on a
    report key that may be absent (a failed step contributes none) or written by
    an older run under a different shape. A shape it does not recognise yields no
    projects, never an exception — the digest is the one thing an owner sees
    without going looking, so it must not be the thing that breaks.
    """
    if not isinstance(report, Mapping):
        return []

    def _flagged(counts: Any) -> bool:
        if not isinstance(counts, Mapping):
            return False
        n = counts.get("degenerate")
        # Type-checked rather than coerced: int("oops") is the exception this
        # function promises never to raise.
        return isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0

    return sorted(str(project) for project, counts in report.items() if _flagged(counts))

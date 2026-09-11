"""kbcore.fleet_coverage — what the queue holds for a session (``kb queue coverage``).

The number the owner feeds ``kb-flush-backfill.sh --seed`` on a box whose local
log cannot be reconstructed by arithmetic (rm1, Windows). Two rules from the
refuters (proposal v2 V1-M1) are enforced HERE, not in the CLI:

* a seed candidate comes ONLY from flush stamps — ``max(flush.offset +
  flush.bytes)`` over stamped rows — and only when the session's rows sit under
  ONE project with ZERO redactions. Otherwise the answer is a refusal WITH the
  reason, never a number;
* ``sum(octet_length(content))`` is post-scrub (redactions shorten it) and is
  labelled "post-scrub, NOT a seed" everywhere it appears.

Owner-only (``scope.require_owner``), read-only, parameterised. Split from
:mod:`kbcore.fleet_flush` for the 300-line cap; that module re-exports
:func:`queue_coverage`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from kbcore import scope
from kbcore.db import connect
from kbcore.errors import InvalidOperation

POST_SCRUB_LABEL = "post-scrub, NOT a seed"
MAX_SESSIONS = 50


class ProjectSplit(BaseModel):
    """One session's rows under one project, split by status."""

    project: str
    rows: int
    mb: float
    pending: int = 0
    extracted: int = 0
    failed: int = 0
    duplicate: int = 0


class SessionCoverage(BaseModel):
    """Everything the queue knows about one ``session:<sid>`` uri."""

    uri: str
    rows: int
    post_scrub_bytes: int = Field(description=POST_SCRUB_LABEL)
    post_scrub_mb: float = Field(description=POST_SCRUB_LABEL)
    first_at: datetime
    last_at: datetime
    boxes: list[str]
    projects: list[ProjectSplit]
    stamped_rows: int
    redacted_rows: int
    seed_candidate: int | None = None  # max(flush.offset+flush.bytes), stamped rows only
    refusal: str | None = None  # why no seed is recommended


class CoverageReport(BaseModel):
    selector: str
    sessions: list[SessionCoverage]


def _like_prefix(prefix: str) -> str:
    esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"session:{esc}%"


def queue_coverage(
    session_prefix: str | None = None, box: str | None = None, limit: int = MAX_SESSIONS
) -> CoverageReport:
    """Per-session queue coverage for ``--session <sid-prefix>`` or ``--box <principal>``.

    Exactly one selector. Sessions are the newest ``limit`` by last submission.
    """
    scope.require_owner("kb queue coverage")
    session_prefix = (session_prefix or "").strip() or None  # "  " selects nothing, not everything
    box = (box or "").strip() or None
    if bool(session_prefix) == bool(box):
        raise InvalidOperation("pass exactly one of --session <sid-prefix> or --box <principal>")
    if session_prefix:
        where, params, selector = (
            "btrim(meta->>'source_uri') LIKE %(pat)s",
            {"pat": _like_prefix(session_prefix)},
            f"session {session_prefix}",
        )
    else:
        where, params, selector = (
            "meta->>'submitted_by' = %(box)s AND btrim(meta->>'source_uri') LIKE 'session:%%'",
            {"box": box},
            f"box {box}",
        )
    params["limit"] = max(1, int(limit))
    with connect() as conn:
        rows = conn.execute(
            f"""
            WITH sel AS (
                SELECT btrim(meta->>'source_uri') AS uri, project, status,
                       octet_length(content) AS bytes, created_at,
                       (meta ? 'flush') AS stamped,
                       (meta ? 'redactions') AS redacted,
                       coalesce(nullif(btrim(meta->>'submitted_by'), ''), '(unknown)') AS box,
                       (meta->'flush'->>'offset')::bigint
                         + (meta->'flush'->>'bytes')::bigint AS reach
                  FROM ingest_queue
                 WHERE kind = 'session' AND {where}
            ), top AS (
                SELECT uri FROM sel GROUP BY uri ORDER BY max(created_at) DESC LIMIT %(limit)s
            )
            SELECT s.uri, s.project, s.status, count(*), sum(s.bytes),
                   min(s.created_at), max(s.created_at),
                   count(*) FILTER (WHERE s.stamped), count(*) FILTER (WHERE s.redacted),
                   max(s.reach) FILTER (WHERE s.stamped),
                   array_agg(DISTINCT s.box)
              FROM sel s JOIN top USING (uri)
             GROUP BY s.uri, s.project, s.status
             ORDER BY max(s.created_at) DESC, s.uri, s.project, s.status
            """,  # noqa: S608 — `where` is one of two constant fragments above
            params,
        ).fetchall()
    return CoverageReport(selector=selector, sessions=_assemble(rows))


def _assemble(rows: list[tuple]) -> list[SessionCoverage]:
    by_uri: dict[str, dict[str, Any]] = {}
    for uri, project, status, n, bytes_, first, last, stamped, redacted, reach, boxes in rows:
        s = by_uri.setdefault(
            uri,
            {
                "rows": 0,
                "bytes": 0,
                "first": first,
                "last": last,
                "boxes": set(),
                "projects": {},
                "stamped": 0,
                "redacted": 0,
                "reach": None,
            },
        )
        s["rows"] += int(n)
        s["bytes"] += int(bytes_)
        s["first"], s["last"] = min(s["first"], first), max(s["last"], last)
        s["boxes"].update(str(b) for b in boxes)
        s["stamped"] += int(stamped)
        s["redacted"] += int(redacted)
        if reach is not None:
            s["reach"] = max(int(reach), s["reach"] or 0)
        p = s["projects"].setdefault(project, {"rows": 0, "bytes": 0})
        p["rows"] += int(n)
        p["bytes"] += int(bytes_)
        p[status] = p.get(status, 0) + int(n)
    out: list[SessionCoverage] = []
    for uri, s in by_uri.items():
        seed, refusal = _seed_verdict(s)
        out.append(
            SessionCoverage(
                uri=uri,
                rows=s["rows"],
                post_scrub_bytes=s["bytes"],
                post_scrub_mb=round(s["bytes"] / 1_000_000, 1),
                first_at=s["first"],
                last_at=s["last"],
                boxes=sorted(s["boxes"]),
                projects=[
                    ProjectSplit(
                        project=proj,
                        rows=p["rows"],
                        mb=round(p["bytes"] / 1_000_000, 1),
                        pending=p.get("pending", 0),
                        extracted=p.get("extracted", 0),
                        failed=p.get("failed", 0),
                        duplicate=p.get("duplicate", 0),
                    )
                    for proj, p in sorted(s["projects"].items())
                ],
                stamped_rows=s["stamped"],
                redacted_rows=s["redacted"],
                seed_candidate=seed,
                refusal=refusal,
            )
        )
    return out


def _seed_verdict(s: dict[str, Any]) -> tuple[int | None, str | None]:
    """The seed candidate, or the reason there is none (V1-M1)."""
    if len(s["projects"]) > 1:
        names = ", ".join(sorted(s["projects"]))
        return None, f"rows under {len(s['projects'])} projects ({names}); repair first"
    if s["redacted"]:
        return None, (
            f"{s['redacted']} row(s) carry redactions; post-scrub bytes are shorter than "
            "the transcript"
        )
    if not s["stamped"] or s["reach"] is None:
        return None, "no stamped rows; a seed comes only from flush stamps (offset+bytes)"
    return int(s["reach"]), None
